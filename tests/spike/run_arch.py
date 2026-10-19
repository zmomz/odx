"""Effective-arch scoreboard — the graduated engine EffectiveArchBuilder on the
real corpus (RFC §22). Mirrors run_resolver.py for the view side.

Phase 0.6 (`run_xml.py`) measured the *spike* parser against each view's own
ancestor chain. This guards the *graduated* engine applicator (`odx.engine`): it
runs the real `parse_xml` + `ModuleGraph` + `EffectiveArchBuilder` over the
corpus and scores the aggregate behavior unit tests can't see —

  - effective views built (primary roots),
  - record-view locator resolution % (the load-bearing view number),
  - dangling-after-apply (genuine UNRESOLVED record locators — Phase-4 candidates),
  - the honest deferred buckets, each first-class: QWeb `qweb-deferred`,
    `%(xmlid)d` `interpolation-deferred` (DYNAMIC), dialect-unsupported (DYNAMIC),
    base-outside-corpus (DYNAMIC).

The point (RFC §22): the aggregate told us the applicator is trustworthy, not the
unit tests. A regression that stops respecting the primary boundary, or the load
order, drops record resolution and grows dangling — invisible to a unit test,
caught here. Permanent.

Usage:
    python -m tests.spike.run_arch [CORPUS_DIR]      (default: tests/corpus)
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

from odx.engine.contracts import Resolution
from odx.engine.loadorder import ModuleGraph
from odx.engine.model.arch import EffectiveArchBuilder
from odx.engine.parser.xml import parse_xml

from .run import DEFAULT_CORPUS, find_modules, module_of


def _root_in_corpus(v, reg) -> bool:
    """Mirror EffectiveArchBuilder._root_of: does this extension's base resolve
    inside the corpus? (Used only to scope the denominator to base-resolved
    locators, matching the Phase 0.6 spike's framing.)"""
    cur, seen = v, set()
    while cur.inherit_ref and not cur.is_primary and (cur.xmlid or "") not in seen:
        seen.add(cur.xmlid or "")
        nxt = reg.get(cur.inherit_ref)
        if nxt is None:
            return False
        cur = nxt
    return True


def run_arch(corpus: Path) -> dict:
    module_dirs = find_modules(corpus)
    graph = ModuleGraph.discover(corpus)

    views = []
    xml_files = parsed_ok = 0
    for mdir in module_dirs:
        mod = module_of(mdir, mdir)
        for xml in mdir.rglob("*.xml"):
            fr = parse_xml(xml, mod)
            xml_files += 1
            if fr.parse_error is None:
                parsed_ok += 1
            views.extend(fr.views)

    reg = {}
    for v in views:
        if v.xmlid:
            reg.setdefault(v.xmlid, v)

    effective = EffectiveArchBuilder(graph).build(views)

    # denominators: base-resolved extension-view locators, split record / template
    rec_locators = tpl_locators = 0
    for v in reg.values():
        if not v.is_inheriting or v.is_primary or v.resolution is Resolution.DYNAMIC:
            continue
        if not _root_in_corpus(v, reg):
            continue
        n = sum(1 for loc in v.locators if loc.expr)
        if v.is_template:
            tpl_locators += n
        else:
            rec_locators += n

    # failures, classified by reason head and by declaring-view kind
    reasons: Counter = Counter()
    rec_dangling = tpl_dangling = 0
    for ev in effective.values():
        for u in ev.unresolved_locators:
            head = u.reason.split(":", 1)[0]
            reasons[head] += 1
            decl = reg.get(u.view_xmlid or "")
            if head == "target-not-found" and decl is not None:
                if decl.is_template:
                    tpl_dangling += 1
                else:
                    rec_dangling += 1

    rec_resolved = rec_locators - rec_dangling
    built = len(effective)
    dynamic_views = sum(1 for ev in effective.values()
                        if ev.resolution is Resolution.DYNAMIC)

    return {
        "corpus": str(corpus),
        "modules": len(module_dirs),
        "xml_files": xml_files,
        "parsed_ok": parsed_ok,
        "parse_rate_pct": round(100.0 * parsed_ok / xml_files, 2) if xml_files else 100.0,
        "registry_views": len(reg),
        "effective_views_built": built,
        "dynamic_views": dynamic_views,
        "record_locators": rec_locators,
        "record_resolved": rec_resolved,
        "record_resolved_pct": round(100.0 * rec_resolved / rec_locators, 2) if rec_locators else 100.0,
        "record_dangling_after_apply": rec_dangling,   # genuine UNRESOLVED (Phase-4)
        "template_locators": tpl_locators,
        "template_resolved": tpl_locators - tpl_dangling,
        "template_dangling_after_apply": tpl_dangling,
        "inactive_view_misses": reasons.get("inactive-view", 0),
        "qweb_deferred": reasons.get("qweb-deferred", 0),
        "interpolation_deferred": reasons.get("interpolation-deferred", 0),
        "dialect_unsupported": reasons.get("dialect-unsupported", 0),
        "base_outside_corpus": reasons.get("base-outside-corpus", 0),
        "dynamic_inherit_id": reasons.get("dynamic-inherit-id", 0),
        "reasons": dict(reasons),
    }


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    corpus = Path(argv[1]).resolve() if len(argv) > 1 else DEFAULT_CORPUS
    if not corpus.is_dir():
        print(f"corpus not found: {corpus}", file=sys.stderr)
        return 2

    r = run_arch(corpus)
    print("\n=== EFFECTIVE-ARCH SCOREBOARD (graduated engine) ===")
    print(f"  modules:                 {r['modules']}")
    print(f"  .xml files:              {r['xml_files']}  ({r['parse_rate_pct']}% parsed)")
    print(f"  registry views:          {r['registry_views']}")
    print(f"  effective views built:   {r['effective_views_built']}  ({r['dynamic_views']} dynamic)")
    print(f"  record locators:         {r['record_locators']}")
    print(f"  record resolved:         {r['record_resolved']}  ({r['record_resolved_pct']}%)")
    print(f"  dangling after apply:    {r['record_dangling_after_apply']}  (genuine UNRESOLVED -> Phase 4)")
    print(f"  template locators:       {r['template_locators']}")
    print(f"  template dangling:       {r['template_dangling_after_apply']}  (genuine UNRESOLVED)")
    print(f"  inactive-view misses:    {r['inactive_view_misses']}  (PARTIAL: optional views, not applied by default)")
    print(f"  qweb-deferred:           {r['qweb_deferred']}  (view backlog item 4 — closed)")
    print(f"  interpolation-deferred:  {r['interpolation_deferred']}  (view backlog item 3 — closed)")
    print(f"  dialect-unsupported:     {r['dialect_unsupported']}  (DYNAMIC, announced)")
    print(f"  base-outside-corpus:     {r['base_outside_corpus']}  (DYNAMIC, boundary)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
