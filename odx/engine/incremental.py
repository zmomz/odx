"""Incremental recompute (RFC §8) — the invalidation graph.

    file changed
        │  re-parse that file only (content-hash keyed; unchanged hashes skip)
        ▼  replace the raw facts it contributed in its module's sources
   dirty models: models the file contributed (before AND after) + every model
        │  inheriting/delegating to them, transitively
        ▼  re-resolve -> new ResolvedModels; drop index entries from the file
   affected scope: the modules owning dirty models/files -> analyzers re-run there

Parsing dominates a cold build (~60% on the live corpus), so re-parsing only the
changed files is the bulk of the win. Model resolution, effective arch and the
index are recomputed from the (mostly cached) raw facts; `invalidation` records
exactly what a change dirtied, which `test_incremental/` asserts is minimal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from odx.engine.contracts import Scope
from odx.engine.loadorder import MANIFEST_NAMES, parse_manifest
from odx.engine.parser.csv import parse_csv
from odx.engine.parser.xml import parse_xml
from odx.engine.pipeline import BuildResult, build
from odx.engine.workspace import (
    Workspace,
    declared_data_files,
    file_hash,
    parse_module,
    parse_python_cached,
)


@dataclass
class Invalidation:
    files: list[Path] = field(default_factory=list)          # re-parsed
    skipped: list[Path] = field(default_factory=list)        # hash unchanged
    modules: set[str] = field(default_factory=set)           # owning modules
    models: set[str] = field(default_factory=set)            # dirty models (closure)
    full_module_reload: set[str] = field(default_factory=set)

    def scope(self) -> Scope:
        return Scope(label="changed", modules=tuple(sorted(self.modules)))


def models_of(ws: Workspace, files: set[str]) -> set[str]:
    out: set[str] = set()
    for rmod in ws.raw_python():
        if str(rmod.file) in files:
            for rm in rmod.models:
                if rm.effective_name:
                    out.add(rm.effective_name)
    return out


def dependents_closure(result: BuildResult, models: set[str]) -> set[str]:
    """Models whose effective fields depend on `models` (inherit/delegate)."""
    children: dict[str, set[str]] = {}
    for name, m in result.models.items():
        for p in (*m.inherit_chain, *m.delegates):
            children.setdefault(p, set()).add(name)
    out, stack = set(), list(models)
    while stack:
        m = stack.pop()
        if m in out:
            continue
        out.add(m)
        stack.extend(children.get(m, ()))
    return out


class IncrementalPipeline:
    def __init__(self, paths: list[Path], project_paths: list[Path] | None = None,
                 cache=None) -> None:
        self.paths = [Path(p) for p in paths]
        self.project_paths = [Path(p) for p in (project_paths or paths)]
        self.cache = cache
        self.ws = Workspace.load(self.paths, project_paths=self.project_paths, cache=cache)
        self.result: BuildResult = build(self.ws)
        self.invalidation = Invalidation()

    def update(self, changed: list[Path]) -> BuildResult:
        inv = Invalidation()
        before_files: set[str] = set()
        # what the changed files contributed BEFORE the change (a removed class
        # still dirties the model it used to extend)
        old_models = models_of(self.ws, {str(Path(f).resolve()) for f in changed})
        for f in changed:
            f = Path(f).resolve()
            key = str(f)
            h = file_hash(f) if f.exists() else ""
            if f.exists() and self.ws.hashes.get(key) == h:
                inv.skipped.append(f)
                continue
            module = self.ws.module_of_file(f)
            if f.name in MANIFEST_NAMES or module is None:
                # manifest edit / new module: rediscover the module (cheap enough)
                mdir = f.parent if f.name in MANIFEST_NAMES else None
                if mdir is None:
                    continue
                mf = parse_manifest(mdir)
                if mf is None:
                    self.ws.modules.pop(mdir.name, None)
                    self.ws.graph.manifests.pop(mdir.name, None)
                    inv.modules.add(mdir.name)
                    continue
                self.ws.graph.manifests[mf.name] = mf
                old = self.ws.modules.get(mf.name)
                if old is not None:
                    before_files |= {str(x) for x in self.ws.files_of(mf.name)}
                self.ws.modules[mf.name] = parse_module(mf, self.cache)
                if any(Path(mf.path).resolve() == p.resolve() or p.resolve() in Path(mf.path).resolve().parents
                       for p in self.project_paths):
                    self.ws.project_modules.add(mf.name)
                for x in self.ws.files_of(mf.name):
                    self.ws.hashes[str(x)] = file_hash(x)
                inv.full_module_reload.add(mf.name)
                inv.modules.add(mf.name)
                inv.files.append(f)
                continue
            ms = self.ws.modules[module]
            before_files.add(key)
            inv.files.append(f)
            inv.modules.add(module)
            self.ws.hashes[key] = h
            if f.suffix == ".py":
                ms.python = [m for m in ms.python if Path(m.file).resolve() != f]
                if f.exists():
                    ms.python.append(parse_python_cached(f, module, self.cache))
            elif f.suffix in (".xml", ".csv"):
                declared = {str((ms.path / rel).resolve()) for rel, _ in
                            declared_data_files(ms.manifest)}
                if f.suffix == ".xml":
                    ms.xml = [x for x in ms.xml if Path(x.file).resolve() != f]
                    if f.exists() and key in declared:
                        ms.xml.append(parse_xml(f, module))
                else:
                    ms.csv = [c for c in ms.csv if Path(c.file).resolve() != f]
                    if f.exists() and key in declared:
                        ms.csv.append(parse_csv(f, module))
        dirty_before = dependents_closure(self.result, old_models)
        if inv.files:
            self.result = build(self.ws)
            touched = models_of(self.ws, {str(Path(p).resolve()) for p in inv.files})
            inv.models = dependents_closure(self.result, touched | dirty_before)
            for m in inv.models:
                rm = self.result.models.get(m)
                if rm is not None:
                    inv.modules |= {p.module for p in rm.contributors}
        self.invalidation = inv
        return self.result
