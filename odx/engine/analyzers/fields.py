"""FieldAnalyzer — field-definition rules from the §16 matrix that need the
resolved model (not just one spec): monetary without a currency, stored
computes without @api.depends, type-changing overrides on the _inherit chain.
"""

from __future__ import annotations

from odx.engine.contracts import Diagnostic, Resolution, Scope, Severity


def _own_field_contribs(api, model: str):
    """(module, RawModel, RawField) for every class declaring a field on `model`."""
    for rmod in api.ws.raw_python():
        for rm in rmod.models:
            if rm.effective_name != model:
                continue
            for rf in rm.fields:
                yield rmod.module, rm, rf


class FieldAnalyzer:
    code_prefix = "field"
    categories = ("fields",)

    def run(self, api, scope: Scope) -> list[Diagnostic]:
        out: list[Diagnostic] = []
        seen_models = set()
        for rmod in api.ws.raw_python():
            if not api.in_scope(scope, module=rmod.module):
                continue
            for rm in rmod.models:
                M = rm.effective_name
                if M is None:
                    continue
                m = api.resolved_model(M)
                for rf in rm.fields:
                    if not api.in_scope(scope, module=rmod.module, location=rf.location):
                        continue
                    out.extend(self._monetary(api, M, rf))
                    out.extend(self._compute_depends(api, M, m, rf))
                    out.extend(self._type_override(api, M, m, rf, rmod.module))
                seen_models.add(M)
        return out

    # §16: monetary w/o currency -> error
    def _monetary(self, api, M, rf):
        if rf.ftype != "Monetary":
            return []
        cf = rf.exprs.get("currency_field")
        if cf is not None:
            return []                                   # explicit: index checks it exists
        if rf.attrs.get("related") or rf.exprs.get("related"):
            return []                                   # related monetary inherits currency
        if api.field_state(M, "currency_id") is not Resolution.UNRESOLVED:
            return []
        return [Diagnostic(
            Severity.ERROR, "field.monetary-no-currency",
            f"Monetary field `{rf.name}` has no currency_field and `{M}` has no "
            "`currency_id`; add a currency_id Many2one or set currency_field=.",
            model=M, field=rf.name, location=rf.location)]

    # §16: compute store=True w/o depends -> warn
    def _compute_depends(self, api, M, m, rf):
        if rf.attrs.get("store") is not True:
            return []
        if rf.attrs.get("readonly") is False:
            return []          # editable stored compute w/o depends = "default at create" idiom
        comp = rf.attrs.get("compute")
        if not isinstance(comp, str) or m is None:
            return []
        contribs = m.methods.get(comp)
        if not contribs:
            return []                                   # missing method: index reports it
        if any("depends" in meth.decorators for _mod, meth in contribs):
            return []
        # inherited declarations of the same compute method count too
        for decl_model, _mod in api.methods(M).get(comp, []):
            dm = api.resolved_model(decl_model)
            if dm and any("depends" in meth.decorators for _x, meth in dm.methods.get(comp, [])):
                return []
        return [Diagnostic(
            Severity.WARNING, "field.compute-store-no-depends",
            f"Stored computed field `{rf.name}` uses `{comp}`, which has no @api.depends: "
            "the stored value is computed once and never recomputed.",
            model=M, field=rf.name, location=rf.location)]

    # §16: name collides on _inherit chain -> warn (a type-changing override)
    def _type_override(self, api, M, m, rf, module):
        if m is None:
            return []
        for parent in m.inherit_chain:
            pf = api.resolved_model(parent).fields.get(rf.name) if api.resolved_model(parent) else None
            if pf is None:
                continue
            ptype = pf.info.type
            if ptype != rf.ftype and {ptype, rf.ftype} != {"Many2one", "Many2oneReference"}:
                return [Diagnostic(
                    Severity.WARNING, "field.type-override",
                    f"`{rf.name}` redefines `{parent}.{rf.name}` ({ptype}) as {rf.ftype}; "
                    "every view, domain and module reading it as the old type may break.",
                    model=M, field=rf.name, location=rf.location)]
        return []
