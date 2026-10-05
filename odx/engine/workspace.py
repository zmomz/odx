"""Workspace — discover modules and parse every source file the pipeline reads.

The parser stage (RFC §6) fed by the load graph: for each module of the
`ModuleGraph`, parse

  - Python: every `*.py` under the module except `tests/`, `migrations/`,
    `upgrades/`, `static/` and the manifest (models are only loaded if imported,
    but an unimported model class is rare enough that "all python" is the right
    approximation — and it is what the Phase 0.5 spike validated);
  - data files the manifest declares (`data`, `demo`, legacy `init_xml`/
    `update_xml`/`demo_xml`, plus `qweb`) — XML through the XML parser, CSV
    through the CSV parser. Undeclared files are not loaded by Odoo, so they are
    not part of the semantics.

Every file is keyed by path and carries a content hash, which is what the
incremental path (RFC §8) diffs against: a changed file maps to the module it
belongs to and the raw facts it contributed. Never raises on a file.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from odx.engine.loadorder import Manifest, ModuleGraph
from odx.engine.model.raw import RawCsvFile, RawModule, RawXmlFile
from odx.engine.parser.csv import parse_csv
from odx.engine.parser.python import parse_python
from odx.engine.parser.xml import parse_xml

# Odoo's server-wide modules: always installed, every module may rely on them.
SERVER_WIDE_MODULES = ("base", "web")

SKIP_DIRS = {"tests", "migrations", "upgrades", "static", "__pycache__", "node_modules"}
DATA_KEYS = ("data", "demo", "init_xml", "update_xml", "demo_xml", "qweb")


@dataclass
class ModuleSources:
    name: str
    manifest: Manifest
    python: list[RawModule] = field(default_factory=list)
    xml: list[RawXmlFile] = field(default_factory=list)
    csv: list[RawCsvFile] = field(default_factory=list)
    missing_data_files: list[str] = field(default_factory=list)   # declared, absent
    demo_files: set[str] = field(default_factory=set)

    @property
    def path(self) -> Path:
        return self.manifest.path


def file_hash(path: Path) -> str:
    try:
        return hashlib.sha1(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


def python_files(module_dir: Path) -> list[Path]:
    out: list[Path] = []
    for p in sorted(module_dir.rglob("*.py")):
        rel = p.relative_to(module_dir).parts
        if any(part in SKIP_DIRS for part in rel[:-1]):
            continue
        if p.name in ("__manifest__.py", "__openerp__.py"):
            continue
        # a nested module (another manifest below us) belongs to that module
        if any((module_dir.joinpath(*rel[:i]) / "__manifest__.py").is_file()
               for i in range(1, len(rel))):
            continue
        out.append(p)
    return out


def declared_data_files(manifest: Manifest) -> list[tuple[str, bool]]:
    """(relative path, is_demo) in manifest order, deduplicated."""
    seen: set[str] = set()
    out: list[tuple[str, bool]] = []
    for key in DATA_KEYS:
        vals = manifest.raw.get(key) or []
        if not isinstance(vals, (list, tuple)):
            continue
        for v in vals:
            if isinstance(v, str) and v not in seen:
                seen.add(v)
                out.append((v, key in ("demo", "demo_xml")))
    return out


def parse_module(manifest: Manifest) -> ModuleSources:
    ms = ModuleSources(name=manifest.name, manifest=manifest)
    for py in python_files(manifest.path):
        ms.python.append(parse_python(py, manifest.name))
    for rel, is_demo in declared_data_files(manifest):
        p = manifest.path / rel
        if not p.is_file():
            ms.missing_data_files.append(rel)
            continue
        if is_demo:
            ms.demo_files.add(str(p))
        suffix = p.suffix.lower()
        if suffix == ".xml":
            ms.xml.append(parse_xml(p, manifest.name))
        elif suffix == ".csv":
            ms.csv.append(parse_csv(p, manifest.name))
    return ms


@dataclass
class Workspace:
    graph: ModuleGraph
    modules: dict[str, ModuleSources] = field(default_factory=dict)
    # modules the user owns (diagnostics are scoped to these by default);
    # everything else (odoo source, OCA deps) is context only.
    project_modules: set[str] = field(default_factory=set)
    hashes: dict[str, str] = field(default_factory=dict)          # file -> sha1

    @classmethod
    def load(cls, addons_paths: list[Path], *, project_paths: list[Path] | None = None,
             modules: set[str] | None = None) -> "Workspace":
        graph = ModuleGraph.discover(*addons_paths)
        ws = cls(graph=graph)
        roots = [Path(p).resolve() for p in (project_paths if project_paths is not None
                                             else addons_paths)]
        for name, mf in graph.manifests.items():
            if modules is not None and name not in modules:
                continue
            ms = parse_module(mf)
            ws.modules[name] = ms
            mpath = Path(mf.path).resolve()
            if any(mpath == r or r in mpath.parents for r in roots):
                ws.project_modules.add(name)
            for f in ws.files_of(name):
                ws.hashes[str(f)] = file_hash(f)
        return ws

    # ------------------------------------------------------------------ #
    def files_of(self, module: str) -> list[Path]:
        ms = self.modules.get(module)
        if ms is None:
            return []
        return ([m.file for m in ms.python] + [x.file for x in ms.xml]
                + [c.file for c in ms.csv]
                + [ms.path / "__manifest__.py"])

    def module_of_file(self, path: Path) -> str | None:
        p = Path(path).resolve()
        best, depth = None, -1
        for name, ms in self.modules.items():
            mp = Path(ms.path).resolve()
            if mp == p or mp in p.parents:
                d = len(mp.parts)
                if d > depth:
                    best, depth = name, d
        return best

    def raw_python(self) -> list[RawModule]:
        return [m for ms in self.modules.values() for m in ms.python]

    def raw_xml(self) -> list[RawXmlFile]:
        return [x for ms in self.modules.values() for x in ms.xml]

    def raw_csv(self) -> list[RawCsvFile]:
        return [c for ms in self.modules.values() for c in ms.csv]

    def dependency_closure(self, module: str) -> set[str]:
        """`module` plus everything it transitively depends on (incl. modules
        missing from the graph — callers check `graph.manifests` for those)."""
        seen: set[str] = set()
        stack = [module] + [m for m in SERVER_WIDE_MODULES if m in self.graph.manifests]
        while stack:
            m = stack.pop()
            if m in seen:
                continue
            seen.add(m)
            stack.extend(self.graph.depends_of(m))
        return seen

    def closure_is_complete(self, module: str) -> bool:
        """Whether every module `module` depends on (transitively) is visible."""
        return all(m in self.graph.manifests for m in self.dependency_closure(module))
