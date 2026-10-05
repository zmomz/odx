"""AccessAnalyzer — security rows (RFC §7 security channel, §15 "missing ACLs").

`access.missing-acl`: a concrete model (Model / TransientModel) DEFINED in the
scope with no `ir.model.access` row anywhere in the corpus. Odoo loads it but
warns "has no access rules" and nobody but the superuser can use it. The fix is
a safe, mechanical EditPlan (a user-group row + manifest entry), so `odx fix`
offers it.
"""

from __future__ import annotations

from odx.engine.contracts import Diagnostic, Scope, Severity


class AccessAnalyzer:
    code_prefix = "access"
    categories = ("access", "security")

    def run(self, api, scope: Scope) -> list[Diagnostic]:
        from odx.engine.analyzers.fixes import missing_acl_fix
        covered = {a.model for a in api.access_rules()}
        out: list[Diagnostic] = []
        for name in api.model_names():
            m = api.resolved_model(name)
            if m is None or not m.defined or m.kind == "AbstractModel":
                continue
            if name in covered:
                continue
            module = m.defined_in
            if not api.in_scope(scope, module=module, model=name, location=m.location):
                continue
            if name.startswith("ir.") or name.startswith("base."):
                continue                       # base registry models handled by base itself
            out.append(Diagnostic(
                Severity.WARNING, "access.missing-acl",
                f"Model `{name}` has no access rules (ir.model.access); only the superuser "
                "can read or write it. Add a row to security/ir.model.access.csv.",
                model=name, location=m.location,
                suggested_fix=missing_acl_fix(api, name, module)))
        for a in api.access_rules():
            if not api.in_scope(scope, module=a.module, location=a.location):
                continue
            if a.model and a.model not in api.resolved_models() and a.model_ref:
                local = a.model_ref.split(".", 1)[-1]
                if (local.startswith("model_") and api.closure_is_complete(a.module or "")
                        and a.module not in api.dynamic_modules()):
                    out.append(Diagnostic(
                        Severity.ERROR, "access.unknown-model",
                        f"ACL `{a.xmlid}` references `{a.model_ref}`, which matches no model "
                        "defined in the module's dependency closure.",
                        location=a.location))
        return out
