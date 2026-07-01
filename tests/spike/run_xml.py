"""Phase 0.6 spike harness: XML view-inheritance resolution (RFC §7).

The Phase 0.5 spike validated Python-side model/field/_inherit resolution — the
*cleaner* half. This measures the messier half the RFC §7 index depends on:
resolving `inherit_id` xml-ids across modules and evaluating `xpath`/`position`
locators against the parent arch.

Emits a scoreboard + a tagged failure catalog to out/, same shape as run.py.

Usage:
    python -m tests.spike.run_xml [CORPUS_DIR]
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

from .catalog import Catalog, Tag
from .run import DEFAULT_CORPUS, OUT_DIR, find_modules, module_of, repo_of
from .xml_parser import ViewRecord, parse_xml_file


# Odoo registers custom XPath extension functions that appear in real view
# locators; lxml doesn't know them. The engine must register the same set. We
# register the common ones here so evaluation reflects reality, not a stdlib gap.
def _register_odoo_xpath_functions() -> None:
    ns = etree.FunctionNamespace(None)

    def hasclass(context, *classes):
        node = context.context_node
        node_classes = frozenset((node.get("class") or "").split())
        return all(c in node_classes for c in classes)

    ns["hasclass"] = hasclass


_register_odoo_xpath_functions()


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
def build_view_registry(file_results) -> dict[str, ViewRecord]:
    reg: dict[str, ViewRecord] = {}
    for fr in file_results:
        for v in fr.views:
            if v.xmlid:
                reg.setdefault(v.xmlid, v)
    return reg


def resolve_chain(view: ViewRecord, reg: dict[str, ViewRecord]):
    """Walk inherit_id from the view up to its base.

    Returns (ancestors, missing_ref). `ancestors` is [parent, ..., base] (the
    view itself excluded). `missing_ref` is set when a ref along the chain isn't
    in the registry. A locator on `view` should find its target in the *effective*
    parent arch = base + all intermediate inheritances applied; as a static proxy
    we test the locator against every ancestor's arch fragment (a node added by an
    intermediate inheritance lives in that intermediate's arch).
    """
    ancestors: list[ViewRecord] = []
    cur = view
    seen: set[str] = set()
    while cur.inherit_ref:
        if cur.xmlid in seen:      # cycle guard
            break
        seen.add(cur.xmlid or "")
        parent = reg.get(cur.inherit_ref)
        if parent is None:
            return ancestors, cur.inherit_ref
        ancestors.append(parent)
        cur = parent
    return ancestors, None


def eval_locator(expr: str | None, base_arch) -> tuple[bool, str | None]:
    """Evaluate a locator xpath against the base arch subtree.

    Returns (target_found, error). Rewrites a leading '//' to './/' so the
    search is scoped to the arch subtree, mirroring Odoo's "within this view".
    """
    if not expr:
        return False, "no-expr"
    if base_arch is None:
        return False, "no-base-arch"
    scoped = ("." + expr) if expr.startswith("/") else expr
    try:
        found = base_arch.xpath(scoped)
    except (etree.XPathError, ValueError) as e:
        return False, f"xpath-error: {type(e).__name__}"
    n = len(found) if isinstance(found, list) else (1 if found else 0)
    return n > 0, None


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #
@dataclass
class XmlMetrics:
    xml_files: int = 0
    parsed_ok: int = 0
    views: int = 0
    templates: int = 0
    inheriting: int = 0
    inherit_resolved: int = 0        # parent ref present in registry
    locators: int = 0
    locators_evaluable: int = 0      # base resolved + expr present
    locators_target_found: int = 0   # xpath matched >=1 node in base arch

    def pct(self, n, d):
        return round(100.0 * n / d, 2) if d else 100.0

    def summary(self):
        return {
            "xml_files": self.xml_files,
            "parse_rate_pct": self.pct(self.parsed_ok, self.xml_files),
            "views": self.views,
            "templates": self.templates,
            "inheriting_views": self.inheriting,
            "inherit_id_resolved_pct": self.pct(self.inherit_resolved, self.inheriting),
            "locators": self.locators,
            "locator_target_found_pct": self.pct(
                self.locators_target_found, self.locators),
            "locator_target_found_of_evaluable_pct": self.pct(
                self.locators_target_found, self.locators_evaluable),
        }


def run_xml(corpus: Path) -> dict:
    module_dirs = find_modules(corpus)
    file_results = []
    file_repo: dict[Path, str] = {}
    for mdir in module_dirs:
        mod = module_of(mdir, mdir)
        repo = repo_of(mdir, corpus)
        for xml in mdir.rglob("*.xml"):
            fr = parse_xml_file(xml, mod)
            file_results.append(fr)
            file_repo[xml] = repo

    reg = build_view_registry(file_results)

    overall = XmlMetrics()
    per_repo: dict[str, XmlMetrics] = defaultdict(XmlMetrics)
    catalog = Catalog()
    missing_refs: Counter = Counter()

    for fr in file_results:
        repo = file_repo.get(fr.file, "?")
        for m in (overall, per_repo[repo]):
            m.xml_files += 1
        if fr.failure:
            catalog.add(Tag.FIXABLE, "xml-parse-error", fr.failure, str(fr.file))
            continue
        for m in (overall, per_repo[repo]):
            m.parsed_ok += 1

        for v in fr.views:
            for m in (overall, per_repo[repo]):
                m.views += 1
                if v.is_template:
                    m.templates += 1
            where = f"{fr.file}:{v.line}"

            if v.inherit_dynamic:
                catalog.add(Tag.OUT_OF_SCOPE, "dynamic-inherit-id",
                            "inherit_id via eval= (runtime-derived)", where)
            if not v.is_inheriting:
                continue

            for m in (overall, per_repo[repo]):
                m.inheriting += 1

            ancestors, missing = resolve_chain(v, reg)
            if v.inherit_ref and v.inherit_ref in reg:
                for m in (overall, per_repo[repo]):
                    m.inherit_resolved += 1
            elif missing:
                missing_refs[missing] += 1
                catalog.add(Tag.BOUNDARY, "inherit-ref-unresolved",
                            f"inherit_id '{missing}' not in corpus", where)

            chain_archs = [a.arch for a in ancestors if a.arch is not None]
            for loc in v.locators:
                for m in (overall, per_repo[repo]):
                    m.locators += 1
                if loc.unresolvable_expr:
                    catalog.add(Tag.HARD, "locator-no-expr",
                                f"{loc.kind} locator without a usable expr",
                                f"{fr.file}:{loc.line}")
                    continue
                if not chain_archs:
                    # can't evaluate — parent unresolved (usually boundary)
                    continue
                for m in (overall, per_repo[repo]):
                    m.locators_evaluable += 1
                # found if the target exists anywhere in the applied chain
                found = False
                eval_err = None
                for arch in chain_archs:
                    ok, err = eval_locator(loc.expr, arch)
                    if ok:
                        found = True
                        break
                    if err and err.startswith("xpath-error"):
                        eval_err = err
                if found:
                    for m in (overall, per_repo[repo]):
                        m.locators_target_found += 1
                elif eval_err:
                    catalog.add(Tag.FIXABLE, "xpath-eval-error",
                                f"{eval_err}: {loc.expr}", f"{fr.file}:{loc.line}")
                else:
                    # target not found anywhere in the chain. Either a node added
                    # by a *sibling* inheritance not on this view's ancestor path
                    # (needs full registry-wide application) or a genuine dangling
                    # locator the Phase 4 analyzer should flag.
                    catalog.add(Tag.HARD, "xpath-target-not-found",
                                f"locator '{loc.expr}' not found in inheritance chain",
                                f"{fr.file}:{loc.line}")

    return {
        "corpus": str(corpus),
        "modules": len(module_dirs),
        "registry_views": len(reg),
        "overall": overall.summary(),
        "per_repo": {r: m.summary() for r, m in sorted(per_repo.items())},
        "catalog_by_tag": catalog.by_tag(),
        "catalog_by_pattern": catalog.by_pattern(),
        "pattern_tags": catalog.pattern_tags(),
        "missing_refs_top": missing_refs.most_common(25),
        "_catalog": catalog,
    }


def write_reports(result: dict) -> tuple[Path, Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    catalog: Catalog = result.pop("_catalog")
    json_path = OUT_DIR / "xml_scoreboard.json"
    json_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    with (OUT_DIR / "xml_failures.jsonl").open("w", encoding="utf-8") as fh:
        for f in catalog.failures:
            fh.write(json.dumps({"tag": f.tag.value, "pattern": f.pattern,
                                 "detail": f.detail, "where": f.where}) + "\n")

    o = result["overall"]
    lines = [
        "# Phase 0.6 — XML View-Inheritance Spike Results\n",
        f"Corpus: `{result['corpus']}`  ·  {result['modules']} modules  ·  "
        f"{result['registry_views']} views in registry\n",
        "## Scoreboard\n",
        "| scope | .xml | parse | views | inheriting | inherit_id resolved | "
        "locators | target found | target found (of evaluable) |",
        "|---|--:|--:|--:|--:|--:|--:|--:|--:|",
    ]

    def row(name, s):
        return (f"| {name} | {s['xml_files']} | {s['parse_rate_pct']}% | "
                f"{s['views']} | {s['inheriting_views']} | "
                f"{s['inherit_id_resolved_pct']}% | {s['locators']} | "
                f"{s['locator_target_found_pct']}% | "
                f"{s['locator_target_found_of_evaluable_pct']}% |")

    lines.append(row("**ALL**", o))
    for r, s in result["per_repo"].items():
        lines.append(row(r, s))
    lines += [
        "",
        "## Failure catalog by tag\n",
        *[f"- **{t}**: {n}" for t, n in
          sorted(result["catalog_by_tag"].items(), key=lambda x: -x[1])],
        "",
        "## Failure catalog by pattern\n",
        "| pattern | tag | count |", "|---|---|--:|",
        *[f"| {p} | {result['pattern_tags'].get(p, '?')} | {n} |" for p, n in
          sorted(result["catalog_by_pattern"].items(), key=lambda x: -x[1])],
        "",
        "## Top unresolved inherit_id refs (corpus boundary)\n",
        "| ref | count |", "|---|--:|",
        *[f"| `{r}` | {n} |" for r, n in result["missing_refs_top"]],
        "",
    ]
    md_path = OUT_DIR / "xml_catalog.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    result["_catalog"] = catalog
    return json_path, md_path


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    corpus = Path(argv[1]).resolve() if len(argv) > 1 else DEFAULT_CORPUS
    if not corpus.is_dir():
        print(f"corpus not found: {corpus}", file=sys.stderr)
        return 2
    print(f"scanning XML in corpus: {corpus}")
    result = run_xml(corpus)
    json_path, md_path = write_reports(result)
    o = result["overall"]
    print("\n=== XML SCOREBOARD (all repos) ===")
    print(f"  .xml files:              {o['xml_files']}")
    print(f"  parsed ok:               {o['parse_rate_pct']}%")
    print(f"  view records:            {o['views']} ({o['templates']} templates)")
    print(f"  inheriting views:        {o['inheriting_views']}")
    print(f"  inherit_id resolved:     {o['inherit_id_resolved_pct']}%")
    print(f"  locators:                {o['locators']}")
    print(f"  target found (all):      {o['locator_target_found_pct']}%")
    print(f"  target found (evaluable):{o['locator_target_found_of_evaluable_pct']}%")
    print("\n=== FAILURE CATALOG BY TAG ===")
    for t, n in sorted(result["catalog_by_tag"].items(), key=lambda x: -x[1]):
        print(f"  {t:14} {n}")
    print(f"\nwrote {json_path}\nwrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
