"""DependencyAnalyzer — `module.missing-dependency`: a module's declarative
data (views, data records, actions, rules, ACLs) uses a field or model that
only modules OUTSIDE its dependency closure declare. It works on a dev box
where everything is installed and breaks on a clean install or a different
install order — exactly the load-time bug class RFC §1 opens with.

Only declarative channels are checked (code paths may be guarded at runtime),
only when the referencing module's closure is fully visible, and the check is
lenient: ANY declaring module in the closure (on the model or an ancestor)
satisfies it.
"""

from __future__ import annotations

from collections import defaultdict

from odx.engine.contracts import Diagnostic, Resolution, Scope, Severity
from odx.engine.index import RefKind
from odx.engine.index.catalog import MAGIC_FIELDS

DECLARATIVE_FIELD_KINDS = {
    RefKind.VIEW_FIELD.value, RefKind.VIEW_MODIFIER.value, RefKind.SEARCH_FILTER.value,
    RefKind.DATA_RECORD.value, RefKind.ACTION_DOMAIN.value, RefKind.RECORD_RULE.value,
    RefKind.VIEW_DOMAIN.value,
}
DECLARATIVE_MODEL_KINDS = {RefKind.VIEW.value, RefKind.ACTION.value, RefKind.ACL.value,
                           RefKind.RECORD_RULE.value, RefKind.DATA_RECORD.value,
                           RefKind.FIELD_COMODEL.value, RefKind.INHERIT.value}


class DependencyAnalyzer:
    code_prefix = "module"
    categories = ("dependencies", "manifest")

    def run(self, api, scope: Scope) -> list[Diagnostic]:
        from odx.engine.analyzers.fixes import add_dependency_fix
        declared: dict[tuple[str, str], set[str]] = defaultdict(set)
        model_mods: dict[str, set[str]] = defaultdict(set)
        for rmod in api.ws.raw_python():
            for rm in rmod.models:
                M = rm.effective_name
                if M is None:
                    continue
                if rm.is_definition:
                    model_mods[M].add(rmod.module)
                for rf in rm.fields:
                    declared[(M, rf.name)].add(rmod.module)

        def declaring(model: str, field: str, stack=frozenset()) -> set[str]:
            mods = set(declared.get((model, field), ()))
            m = api.resolved_model(model)
            if m is not None and model not in stack:
                for p in (*m.inherit_chain, *m.delegates):
                    mods |= declaring(p, field, stack | {model})
            return mods

        dynamic_modules = {rmod.module for rmod in api.ws.raw_python()
                           for rm in rmod.models if "dynamic-name" in rm.notes
                           or rm.resolution is Resolution.DYNAMIC}
        out: list[Diagnostic] = []
        reported: set[tuple[str, str]] = set()
        closures: dict[str, set[str]] = {}
        for e in api.entries():
            if e.resolution is not Resolution.STATIC or not e.module or not e.model:
                continue
            if not api.in_scope(scope, module=e.module, location=e.location):
                continue
            if e.module in dynamic_modules:
                continue
            if e.module not in closures:
                closures[e.module] = (api.dependency_closure(e.module)
                                      if api.closure_is_complete(e.module) else None)
            closure = closures[e.module]
            if closure is None:
                continue
            if e.field and e.kind in DECLARATIVE_FIELD_KINDS:
                f = api.get_field(e.model, e.field)
                if f is None or f.via == "magic" or e.field in MAGIC_FIELDS:
                    continue
                mods = declaring(e.model, e.field)
                if not mods or mods & closure:
                    continue
                what = f"field `{e.model}.{e.field}`"
            elif not e.field and not e.method and e.kind in DECLARATIVE_MODEL_KINDS:
                mods = model_mods.get(e.model, set())
                if not mods or mods & closure:
                    continue
                what = f"model `{e.model}`"
            else:
                continue
            order = {m: i for i, m in enumerate(api.modules())}
            dep = sorted(mods, key=lambda m: order.get(m, 1 << 30))[0]
            if (e.module, what) in reported:
                continue
            reported.add((e.module, what))
            out.append(Diagnostic(
                Severity.ERROR, "module.missing-dependency",
                f"{what} is declared by `{dep}`, which `{e.module}` does not depend on "
                f"({e.detail}); add '{dep}' to the manifest depends.",
                model=e.model, field=e.field, location=e.location,
                suggested_fix=add_dependency_fix(api, e.module, dep)))
        return out
