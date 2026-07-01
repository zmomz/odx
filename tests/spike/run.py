"""Phase 0.5 spike harness (RFC §3, §19, §22).

Walks a corpus of real Odoo modules, runs the spike parser + resolver, and emits:
  - a SCOREBOARD (orientation): parse / model / field / inherit resolution rates
  - a FAILURE CATALOG (the deliverable): every failure tagged
        fixable | hard | out-of-scope | boundary
  - the static/runtime BOUNDARY summary

Re-runnable as a permanent correctness harness. Writes JSON + Markdown to
tests/spike/out/ and prints a summary.

Usage:
    python -m tests.spike.run [CORPUS_DIR]      (default: tests/corpus)
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field as dc_field
from pathlib import Path

from .catalog import Catalog, Tag
from .parser import FileResult, parse_file
from .resolver import build_registry, resolve_effective_fields

SPIKE_DIR = Path(__file__).resolve().parent
DEFAULT_CORPUS = SPIKE_DIR.parent / "corpus"
OUT_DIR = SPIKE_DIR / "out"

MANIFESTS = ("__manifest__.py", "__openerp__.py")


# --------------------------------------------------------------------------- #
# corpus discovery
# --------------------------------------------------------------------------- #
def find_modules(corpus: Path) -> list[Path]:
    """Directories containing an Odoo manifest."""
    mods = []
    for manifest_name in MANIFESTS:
        mods.extend(p.parent for p in corpus.rglob(manifest_name))
    return sorted(set(mods))


def repo_of(path: Path, corpus: Path) -> str:
    try:
        return path.relative_to(corpus).parts[0]
    except (ValueError, IndexError):
        return "?"


def module_of(path: Path, module_dir: Path) -> str:
    return module_dir.name


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #
@dataclass
class Metrics:
    py_files: int = 0
    parsed_ok: int = 0
    model_classes: int = 0
    models_named: int = 0                 # effective_name determinable
    field_defs: int = 0
    relational: int = 0
    relational_literal: int = 0           # comodel is a literal string
    inherit_refs: int = 0
    inherit_resolved: int = 0             # target present in registry

    def pct(self, num: int, den: int) -> float:
        return 100.0 * num / den if den else 100.0

    def summary(self) -> dict:
        return {
            "py_files": self.py_files,
            "parsed_ok": self.parsed_ok,
            "parse_rate_pct": round(self.pct(self.parsed_ok, self.py_files), 2),
            "model_classes": self.model_classes,
            "models_named_pct": round(self.pct(self.models_named, self.model_classes), 2),
            "field_defs": self.field_defs,
            "relational": self.relational,
            "relational_literal_comodel_pct": round(
                self.pct(self.relational_literal, self.relational), 2),
            "inherit_refs": self.inherit_refs,
            "inherit_resolved_pct": round(
                self.pct(self.inherit_resolved, self.inherit_refs), 2),
        }


def run(corpus: Path) -> dict:
    module_dirs = find_modules(corpus)
    file_results: list[FileResult] = []
    file_repo: dict[Path, str] = {}

    # 1. parse every .py in every module
    for mdir in module_dirs:
        mod_name = module_of(mdir, mdir)
        repo = repo_of(mdir, corpus)
        for py in mdir.rglob("*.py"):
            fr = parse_file(py, mod_name)
            file_results.append(fr)
            file_repo[py] = repo

    # 2. build registry across the WHOLE corpus (so core resolves OCA inherits)
    reg = build_registry(file_results)
    resolve_effective_fields(reg)

    # 3. metrics — global and per repo
    overall = Metrics()
    per_repo: dict[str, Metrics] = defaultdict(Metrics)
    catalog = Catalog()

    for fr in file_results:
        repo = file_repo.get(fr.file, "?")
        for m in (overall, per_repo[repo]):
            m.py_files += 1
        if fr.failure is not None:
            catalog.add(Tag.FIXABLE, "parse-error", fr.failure.error, str(fr.file))
            continue
        for m in (overall, per_repo[repo]):
            m.parsed_ok += 1

        for rm in fr.models:
            for m in (overall, per_repo[repo]):
                m.model_classes += 1
                m.field_defs += len(rm.fields)
            where = f"{fr.file}:{rm.line}"

            if rm.effective_name:
                for m in (overall, per_repo[repo]):
                    m.models_named += 1
            elif rm.name_dynamic:
                catalog.add(Tag.OUT_OF_SCOPE, "dynamic-name",
                            "_name is not a literal (runtime-derived)", where)
            elif rm.inherit_dynamic:
                catalog.add(Tag.OUT_OF_SCOPE, "dynamic-inherit",
                            "_inherit is not a literal", where)
            else:
                catalog.add(Tag.FIXABLE, "nameless-model",
                            "model class with no _name/_inherit found statically", where)

            # dynamic-pattern flags
            if rm.name_dynamic and rm.effective_name:
                catalog.add(Tag.OUT_OF_SCOPE, "dynamic-name",
                            "_name is not a literal", where)
            if rm.inherit_dynamic:
                catalog.add(Tag.OUT_OF_SCOPE, "dynamic-inherit",
                            "_inherit is not a literal", where)
            if rm.has_fields_in_loop:
                catalog.add(Tag.OUT_OF_SCOPE, "fields-in-loop",
                            "fields constructed in a loop", where)
            if rm.has_setattr_fields:
                catalog.add(Tag.OUT_OF_SCOPE, "setattr-field",
                            "field added via setattr()", where)

            # field-level
            for f in rm.fields:
                if f.ftype in {"Many2one", "One2many", "Many2many", "Many2oneReference"}:
                    for m in (overall, per_repo[repo]):
                        m.relational += 1
                    if f.dynamic_comodel:
                        catalog.add(Tag.OUT_OF_SCOPE, "dynamic-comodel",
                                    f"{f.ftype} '{f.name}' comodel is not a literal",
                                    f"{fr.file}:{f.line}")
                    elif f.comodel:
                        for m in (overall, per_repo[repo]):
                            m.relational_literal += 1
                    else:
                        # no literal comodel arg. Usually a `related` field whose
                        # comodel is inferred from the related chain (resolvable
                        # with more work) or a Reference-style field. HARD, not
                        # out-of-scope.
                        if "related" in f.attrs:
                            catalog.add(Tag.HARD, "comodel-via-related",
                                        f"{f.ftype} '{f.name}' comodel inferred from related=",
                                        f"{fr.file}:{f.line}")
                        else:
                            catalog.add(Tag.HARD, "comodel-absent",
                                        f"{f.ftype} '{f.name}' has no explicit comodel",
                                        f"{fr.file}:{f.line}")
                    # one2many inverse resolution. A *computed* one2many needs no
                    # inverse (fully resolvable). A non-literal inverse is dynamic.
                    # A plain omitted inverse on a stored o2m is rare and worth a
                    # note (the Phase 4 analyzer flags it; the resolver still copes).
                    if f.ftype == "One2many":
                        if f.dynamic_inverse:
                            catalog.add(Tag.OUT_OF_SCOPE, "dynamic-inverse",
                                        f"one2many '{f.name}' inverse is not a literal",
                                        f"{fr.file}:{f.line}")
                        elif not f.inverse_given and not f.is_computed:
                            catalog.add(Tag.HARD, "one2many-inverse-absent",
                                        f"stored one2many '{f.name}' has no inverse_name",
                                        f"{fr.file}:{f.line}")

        if fr.monkeypatch_fields:
            catalog.add(Tag.HARD, "monkeypatch-field",
                        f"{fr.monkeypatch_fields} module-level field injection(s)",
                        str(fr.file))

    # 4. inherit-chain resolution (needs the full registry)
    missing_parents: Counter = Counter()
    for name, model in reg.models.items():
        repo = "?"
        if model.contributions:
            repo = file_repo.get(model.contributions[0].file, "?")
        parents = set(model.inherit) | set(model.inherits.keys())
        for parent in parents:
            overall.inherit_refs += 1
            per_repo[repo].inherit_refs += 1
            if parent in reg.models:
                overall.inherit_resolved += 1
                per_repo[repo].inherit_resolved += 1
            else:
                missing_parents[parent] += 1

    # unresolved parents: with core present these are corpus-boundary (addons we
    # didn't clone) unless they look like typos. Tag as boundary; list them.
    for parent, count in missing_parents.items():
        catalog.add(Tag.BOUNDARY, "external-inherit-target",
                    f"_inherit target '{parent}' not in corpus (x{count})", parent)

    return {
        "corpus": str(corpus),
        "modules": len(module_dirs),
        "registry_models": len(reg.models),
        "overall": overall.summary(),
        "per_repo": {r: m.summary() for r, m in sorted(per_repo.items())},
        "catalog_by_tag": catalog.by_tag(),
        "catalog_by_pattern": catalog.by_pattern(),
        "pattern_tags": catalog.pattern_tags(),
        "missing_parents_top": missing_parents.most_common(25),
        "_catalog": catalog,
    }


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def _fmt_metrics_row(name: str, s: dict) -> str:
    return (f"| {name} | {s['py_files']} | {s['parse_rate_pct']}% | "
            f"{s['model_classes']} | {s['models_named_pct']}% | "
            f"{s['field_defs']} | {s['relational_literal_comodel_pct']}% | "
            f"{s['inherit_refs']} | {s['inherit_resolved_pct']}% |")


def write_reports(result: dict) -> tuple[Path, Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    catalog: Catalog = result.pop("_catalog")

    # JSON scoreboard
    json_path = OUT_DIR / "scoreboard.json"
    json_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    # full catalog dump (every failure, for inspection / regression tests)
    failures_path = OUT_DIR / "failures.jsonl"
    with failures_path.open("w", encoding="utf-8") as fh:
        for f in catalog.failures:
            fh.write(json.dumps({
                "tag": f.tag.value, "pattern": f.pattern,
                "detail": f.detail, "where": f.where,
            }) + "\n")

    # Markdown catalog
    lines: list[str] = []
    lines.append("# Phase 0.5 — Resolver Spike Results\n")
    lines.append(f"Corpus: `{result['corpus']}`  ·  "
                 f"{result['modules']} modules  ·  "
                 f"{result['registry_models']} distinct models resolved into registry\n")

    lines.append("## Scoreboard (orientation, not the gate)\n")
    lines.append("| scope | .py | parse | model classes | named | field defs | "
                 "rel. literal comodel | inherit refs | inherit resolved |")
    lines.append("|---|--:|--:|--:|--:|--:|--:|--:|--:|")
    lines.append(_fmt_metrics_row("**ALL**", result["overall"]))
    for repo, s in result["per_repo"].items():
        lines.append(_fmt_metrics_row(repo, s))
    lines.append("")
    lines.append("RFC §3 targets: parse 100% · models ≥95% · fields ≥95% · "
                 "inherit chains ≥90%.\n")

    lines.append("## Failure catalog by tag (the deliverable)\n")
    for tag, n in sorted(result["catalog_by_tag"].items(), key=lambda x: -x[1]):
        lines.append(f"- **{tag}**: {n}")
    lines.append("")

    lines.append("## Failure catalog by pattern\n")
    lines.append("| pattern | tag | count |")
    lines.append("|---|---|--:|")
    ptags = result["pattern_tags"]
    for pat, n in sorted(result["catalog_by_pattern"].items(), key=lambda x: -x[1]):
        lines.append(f"| {pat} | {ptags.get(pat, '?')} | {n} |")
    lines.append("")

    lines.append("## Static/runtime boundary — top external inherit targets\n")
    lines.append("These are models referenced via `_inherit`/`_inherits` but not "
                 "defined in the cloned corpus (addons/repos we didn't clone). "
                 "Corpus boundary, *not* a resolver failure.\n")
    lines.append("| model | refs |")
    lines.append("|---|--:|")
    for parent, count in result["missing_parents_top"]:
        lines.append(f"| `{parent}` | {count} |")
    lines.append("")

    lines.append("## Sample failures (first 40)\n")
    lines.append("| tag | pattern | where | detail |")
    lines.append("|---|---|---|---|")
    for f in catalog.failures[:40]:
        where = f.where.replace(str(result['corpus']), "…")
        lines.append(f"| {f.tag.value} | {f.pattern} | `{where}` | {f.detail} |")
    lines.append("")

    md_path = OUT_DIR / "catalog.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    result["_catalog"] = catalog
    return json_path, md_path


def main(argv: list[str]) -> int:
    try:  # Windows consoles default to cp1252; the report uses unicode.
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    corpus = Path(argv[1]).resolve() if len(argv) > 1 else DEFAULT_CORPUS
    if not corpus.is_dir():
        print(f"corpus not found: {corpus}", file=sys.stderr)
        print("clone some modules into tests/corpus/ first (see tests/spike/README.md)",
              file=sys.stderr)
        return 2

    print(f"scanning corpus: {corpus}")
    result = run(corpus)
    json_path, md_path = write_reports(result)

    o = result["overall"]
    print("\n=== SCOREBOARD (all repos) ===")
    print(f"  modules:              {result['modules']}")
    print(f"  distinct models:      {result['registry_models']}")
    print(f"  .py files:            {o['py_files']}")
    print(f"  parsed ok:            {o['parse_rate_pct']}%   (target 100%)")
    print(f"  models named:         {o['models_named_pct']}%   (target >=95%)")
    print(f"  rel. literal comodel: {o['relational_literal_comodel_pct']}%")
    print(f"  inherit resolved:     {o['inherit_resolved_pct']}%   (target >=90%)")
    print("\n=== FAILURE CATALOG BY TAG ===")
    for tag, n in sorted(result["catalog_by_tag"].items(), key=lambda x: -x[1]):
        print(f"  {tag:14} {n}")
    print(f"\nwrote {json_path}")
    print(f"wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
