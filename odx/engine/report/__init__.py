"""report — HealthReport aggregation (RFC §15).

The flagship `report`: the full analyzer battery composed into a scored
HealthReport with recommendations. Distinct from fast `check` (same
diagnostics, no score) and runtime `doctor` (folded in with --with-runtime).

Score: weighted defect density over the scope's size, so a large codebase with
three warnings isn't graded like a tiny module with three errors.
    weight = 5·error + 2·warning + 0.5·info
    score  = round(100 · exp(−weight / (8 · max(1, models + views/4))))
DYNAMIC / UNRESOLVED counts are reported separately (RFC §10) — "we can't see
it" never lowers the score.
"""

from __future__ import annotations

import math
from collections import Counter

from odx.engine.contracts import Diagnostic, DoctorReport, HealthReport, Scope, Severity

WEIGHTS = {Severity.ERROR: 5.0, Severity.WARNING: 2.0, Severity.INFO: 0.5, Severity.HINT: 0.0}

RECOMMENDATIONS = {
    "view": "Fix view references first — Odoo refuses to load a view with an unknown field "
            "or a dangling xpath, which blocks module install/upgrade.",
    "xmlid": "Resolve dangling/undeclared external ids; add missing manifest dependencies "
             "(`odx fix` can add them).",
    "module": "Declare every module whose fields/models your data uses in `depends` "
              "(`odx fix` adds them).",
    "access": "Add ir.model.access rows for new models (`odx fix access`).",
    "field": "Fix field definitions: missing compute methods, broken related/depends paths.",
    "data": "Data files set unknown fields or call missing methods — install will fail.",
    "security": "Record rules reference unknown fields; access checks will crash.",
    "manifest": "Repair the manifest: missing data files and unknown dependencies.",
    "action": "Actions/crons/automations reference unknown fields or methods.",
    "report": "QWeb reports read unknown fields; printing will fail.",
    "parse": "Some files fail to parse; nothing in them is analyzed.",
    "python": "Code references models no dependency defines.",
    "model": "Model attributes (_order/_inherit) reference unknown fields/models.",
    "doctor": "The fresh-install probe failed; see the runtime findings.",
}


def _grade(score: int) -> str:
    return "A" if score >= 90 else "B" if score >= 80 else "C" if score >= 70 else \
        "D" if score >= 60 else "F"


def build_report(api, scope: Scope, diagnostics: list[Diagnostic],
                 doctor: DoctorReport | None = None) -> HealthReport:
    diags = list(diagnostics) + (list(doctor.diagnostics) if doctor else [])
    by_sev = Counter(d.severity.value for d in diags)
    by_cat = Counter(d.code.split(".", 1)[0] for d in diags)

    modules = [m for m in api.modules() if api.in_scope(scope, module=m)]
    models = [n for n in api.model_names()
              if any(api.in_scope(scope, module=p.module)
                     for p in api.resolved_model(n).contributors)]
    views = [v for v in api.raw_views() if api.in_scope(scope, module=v.module)]
    res = Counter()
    for n in models:
        for rf in api.resolved_model(n).fields.values():
            if api.in_scope(scope, module=rf.provenance.module):
                res[rf.resolution.value] += 1
    refs = Counter()
    for e in api.entries():
        if e.module and api.in_scope(scope, module=e.module):
            refs[e.resolution.value] += 1

    weight = sum(WEIGHTS.get(d.severity, 0) for d in diags)
    size = max(1.0, len(models) + len(views) / 4)
    score = int(round(100 * math.exp(-weight / (8 * size))))
    if doctor is not None and not doctor.ok:
        score = min(score, 59)                     # a failing clean install caps at D/F
    recs = [RECOMMENDATIONS[c] for c, _n in by_cat.most_common() if c in RECOMMENDATIONS]
    if res.get("dynamic"):
        recs.append(f"{res['dynamic']} field facts are runtime-derived (DYNAMIC); run "
                    "`odx report --with-runtime` or `odx diff db` to confirm them live.")
    if not diags:
        recs.append("No findings. Keep `odx check` in CI to hold the line.")
    return HealthReport(
        score=score, grade=_grade(score), diagnostics=diags,
        by_severity=dict(by_sev), by_category=dict(by_cat), resolution=dict(res),
        stats={"modules": len(modules), "models": len(models), "views": len(views),
               "references": sum(refs.values()),
               "references_unresolved": refs.get("unresolved", 0),
               "references_dynamic": refs.get("dynamic", 0)},
        recommendations=recs, runtime=doctor)
