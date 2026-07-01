"""Analyzer: turn the model resolver's *unresolvable* states into Diagnostics.

This is the accusation half of the discovery/accusation split (RFC §6). The
`ModelResolver` records that a field could not be resolved — `Resolution.UNRESOLVED`
plus a reason in `ResolvedField.notes` (e.g. `"related-cycle"`) — but it does not
judge: emitting a `Diagnostic` is an analyzer's job. Keeping it here means there is
one path from broken-state to Diagnostic for *every* field defect, not a special
resolver-emitted path for cycles and a separate analyzer path for the rest.

The first rule implemented is the related-field cycle (`field.related-cycle`): a
`related=` chain that walks back onto itself, which Odoo cannot compute. It is a
real defect in user code, so it is an ERROR.

Contract note: RFC §9 gives every analyzer `run(self, api: SemanticAPI, scope)`.
SemanticAPI is a later Phase-3 stage; until it exists, this analyzer reads the
resolved-model mapping directly. When SemanticAPI lands, `run` binds to its
"all resolved models" surface with no change to the logic below.
"""

from __future__ import annotations

from odx.engine.contracts import Diagnostic, Resolution, Severity
from odx.engine.model.resolver import ResolvedModel

# each note the resolver may leave, mapped to how the analyzer reports it.
# only genuinely-defective states become Diagnostics; DYNAMIC/PARTIAL are honest
# non-defects (routed to the runtime bridge / improvable) and are left alone.
_RULES = {
    "related-cycle": (
        Severity.ERROR,
        "field.related-cycle",
        "`related` chain cycles back on itself; Odoo cannot compute this field.",
    ),
}


class ModelResolutionAnalyzer:
    """Reports fields the resolver marked UNRESOLVED for a defective reason."""

    code_prefix = "field"

    def run_on_models(self, models: dict[str, ResolvedModel]) -> list[Diagnostic]:
        """Read-only over resolved models → Diagnostics (RFC §6, C2).

        Named `run_on_models` rather than `run` because the RFC §9 `run(api, scope)`
        surface (SemanticAPI) is not built yet; this is the same logic against the
        interim input. The rename is intentional so the eventual `run` is a thin
        adapter, not a silent signature change.
        """
        out: list[Diagnostic] = []
        for model in models.values():
            for name, rf in model.fields.items():
                if rf.resolution is not Resolution.UNRESOLVED:
                    continue
                for note in rf.notes:
                    rule = _RULES.get(note)
                    if rule is None:
                        continue
                    severity, code, message = rule
                    out.append(Diagnostic(
                        severity=severity,
                        code=code,
                        message=message,
                        model=model.name,
                        field=name,
                        location=rf.info.location,
                    ))
        return out
