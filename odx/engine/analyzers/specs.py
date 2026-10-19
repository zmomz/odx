"""Engine-backed FieldSpec validation (RFC §16 column 4): what the matrix alone
can't know — comodel must exist, one2many inverse must exist (error if absent),
the name must not collide on the _inherit chain, monetary needs a currency."""

from __future__ import annotations

from odx.engine.contracts import Diagnostic, FieldSpec, Resolution, Severity


def validate_field_spec(api, spec: FieldSpec) -> list[Diagnostic]:
    out: list[Diagnostic] = []
    t = (spec.type or "").lower()

    def d(sev, code, msg):
        out.append(Diagnostic(sev, code, msg, model=spec.model, field=spec.name))

    sees_base = "base" in api.ws.graph.manifests     # odoo source indexed?
    if spec.model not in api.resolved_models() and sees_base:
        d(Severity.WARNING, "field.spec-model-unknown",
          f"`{spec.model}` is not a model in the indexed addons; check the name "
          "(the field will be added via _inherit).")
    elif spec.name in api.resolved_model(spec.model).fields:
        f = api.resolved_model(spec.model).fields[spec.name]
        d(Severity.WARNING, "field.spec-name-collision",
          f"`{spec.model}` already has `{spec.name}` ({f.info.type}, from "
          f"{f.provenance.module}); this would override it.")
    if spec.comodel and t in ("many2one", "one2many", "many2many"):
        if spec.comodel not in api.resolved_models():
            if sees_base:
                d(Severity.ERROR, "field.spec-unknown-comodel",
                  f"comodel `{spec.comodel}` is not a known model.")
        elif t == "one2many" and spec.inverse_name:
            st = api.field_state(spec.comodel, spec.inverse_name)
            if st is Resolution.UNRESOLVED:
                d(Severity.ERROR, "field.spec-inverse-missing",
                  f"`{spec.comodel}` has no field `{spec.inverse_name}`; create the "
                  f"Many2one `{spec.inverse_name}` -> `{spec.model}` first.")
            elif st is Resolution.STATIC:
                inv = api.get_field(spec.comodel, spec.inverse_name)
                if inv and inv.type not in ("Many2one", "Many2oneReference", "Integer"):
                    d(Severity.ERROR, "field.spec-inverse-not-many2one",
                      f"`{spec.comodel}.{spec.inverse_name}` is a {inv.type}, not a Many2one.")
    if t == "monetary" and spec.model in api.resolved_models():
        cf = spec.currency_field or "currency_id"
        if api.field_state(spec.model, cf) is Resolution.UNRESOLVED:
            d(Severity.ERROR, "field.monetary-no-currency",
              f"`{spec.model}` has no `{cf}`; add a currency Many2one first.")
    return out
