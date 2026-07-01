"""Resolver scoreboard — the graduated engine ModelResolver on the real corpus.

Phase 0.5/0.6 (`run.py`, `run_xml.py`) validated the *spike* parser/resolver.
This harness guards the *graduated* engine pipeline (`odx.engine`): it runs the
real `parse_python` + `ModuleGraph` + `ModelResolver` over the corpus and scores
the field-resolution behavior that unit tests can't see in aggregate —

  - the `related=` worklist lift: `comodel-via-related` fields that became STATIC,
  - the honest tail: fields left DYNAMIC (runtime-derived) or UNRESOLVED (path
    broken / hops through a module outside the corpus),
  - **false cycles**: `related-cycle` count, which must stay 0 on clean code,
  - `comodel-absent` PARTIALs (the backlog-2 pile), tracked so a regression that
    grows it is visible.

The point (RFC §22): the aggregate is what told us the resolver is trustworthy,
not the unit tests. A regression that drops the lift (271 → 260) or introduces a
handful of false cycles won't trip a unit test but will trip this. Permanent.

Usage:
    python -m tests.spike.run_resolver [CORPUS_DIR]      (default: tests/corpus)
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

from odx.engine.loadorder import ModuleGraph
from odx.engine.model.resolver import ModelResolver
from odx.engine.parser.python import parse_python

from .run import DEFAULT_CORPUS, find_modules


def run_resolver(corpus: Path) -> dict:
    module_dirs = find_modules(corpus)
    graph = ModuleGraph.discover(corpus)

    raw_modules = []
    py_files = parsed_ok = 0
    for mdir in module_dirs:
        for py in mdir.rglob("*.py"):
            rm = parse_python(py, mdir.name)
            raw_modules.append(rm)
            py_files += 1
            if rm.parse_error is None:
                parsed_ok += 1

    models = ModelResolver(graph).resolve(raw_modules)

    # Tally OWN fields only (via == "own"), so a field inherited into N children
    # is counted once at its definition site — not N times.
    res_counts: Counter = Counter()
    note_counts: Counter = Counter()
    own_fields = 0
    for model in models.values():
        for rf in model.fields.values():
            if rf.provenance.via != "own":
                continue
            own_fields += 1
            res_counts[rf.resolution.value] += 1
            for n in rf.notes:
                note_counts[n] += 1

    related_resolved = note_counts.get("comodel-resolved-via-related", 0)
    related_cycles = note_counts.get("related-cycle", 0)
    comodel_absent = note_counts.get("comodel-absent", 0)
    comodel_merged = note_counts.get("comodel-via-override-merge", 0)
    comodel_runtime_ref = note_counts.get("comodel-runtime-ref", 0)
    inverse_resolved = note_counts.get("inverse-resolved", 0)
    inverse_nonrelational = note_counts.get("inverse-nonrelational", 0)
    inverse_dangling = note_counts.get("inverse-dangling", 0)
    inverse_outside = note_counts.get("inverse-comodel-outside-corpus", 0)

    return {
        "corpus": str(corpus),
        "modules": len(module_dirs),
        "models_resolved": len(models),
        "py_files": py_files,
        "parsed_ok": parsed_ok,
        "parse_rate_pct": round(100.0 * parsed_ok / py_files, 2) if py_files else 100.0,
        "own_fields": own_fields,
        "field_resolution": dict(res_counts),
        "related_resolved_static": related_resolved,   # worklist lift
        "related_cycles": related_cycles,              # must be 0 on clean corpus
        "comodel_absent_partial": comodel_absent,      # backlog-2 pile (now drained)
        "comodel_merged": comodel_merged,              # field-override attr merge lift
        "comodel_runtime_ref": comodel_runtime_ref,    # genuine Many2oneReference DYNAMICs
        "inverse_resolved": inverse_resolved,          # one2many inverse verified relational
        "inverse_nonrelational": inverse_nonrelational,  # reference-style Integer inverse (valid)
        "inverse_dangling": inverse_dangling,          # inverse absent from a visible comodel (want 0)
        "inverse_outside_corpus": inverse_outside,     # comodel out of corpus, not downgraded
        "notes": dict(note_counts),
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

    r = run_resolver(corpus)
    print("\n=== RESOLVER SCOREBOARD (graduated engine) ===")
    print(f"  modules:                 {r['modules']}")
    print(f"  models resolved:         {r['models_resolved']}")
    print(f"  .py files:               {r['py_files']}  ({r['parse_rate_pct']}% parsed)")
    print(f"  own field defs:          {r['own_fields']}")
    print(f"  field resolution:        {r['field_resolution']}")
    print(f"  related= lift → STATIC:  {r['related_resolved_static']}")
    print(f"  related cycles (want 0): {r['related_cycles']}")
    print(f"  comodel-absent PARTIAL:  {r['comodel_absent_partial']}  (backlog-2, drained)")
    print(f"  override-merge lift:     {r['comodel_merged']}  (attr merge)")
    print(f"  Many2oneRef DYNAMIC:     {r['comodel_runtime_ref']}  (genuine runtime)")
    print(f"  one2many inverse OK:     {r['inverse_resolved']}  (verified relational)")
    print(f"  inverse non-relational:  {r['inverse_nonrelational']}  (reference-style, valid)")
    print(f"  inverse dangling (want 0):{r['inverse_dangling']}")
    print(f"  inverse comodel out-of-corpus: {r['inverse_outside_corpus']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
