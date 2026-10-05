"""The MCP tool surface (RFC §13: client #2, built to VALIDATE the API).

`OdxTools` is the whole client: every method is one MCP tool, taking JSON-able
arguments and returning JSON-able data built by `serialize.to_jsonable` from the
engine's contracts. It contains no Odoo knowledge — if a tool ever needs to
compute something the API doesn't return, that is an API flaw to fix in the
engine (the validation gate, RFC §19 Phase 5; see docs/API_GATE.md).

Honesty is carried through, not re-derived: `Resolution` is passed to the AI
verbatim, and DYNAMIC comes with the instruction to ask the runtime rather than
guess (RFC §10: "MCP tells the AI 'ask the runtime'").

Mutations follow C3 + RFC §10 ("MCP proposes for approval"): `plan_*` tools
return an EditPlan with a diff and an id; only `apply_plan(id)` writes, so the
host's tool-approval UI is the approval step.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

from odx.clients import serialize
from odx.clients.edits import apply_plan as _apply, render_plan
from odx.engine.api import Engine
from odx.engine.contracts import (
    EditPlan,
    FieldSpec,
    MenuSpec,
    ModelSpec,
    ModuleSpec,
    Resolution,
    Scope,
    ViewSpec,
)

_HINT = {
    Resolution.STATIC: "known from the source",
    Resolution.UNRESOLVED: "provably absent: the model is fully visible and does not define it",
    Resolution.DYNAMIC: ("cannot be decided statically (defined outside the indexed addons or "
                         "built at runtime) — ask the running instance instead of guessing"),
    Resolution.PARTIAL: "resolved with a known gap",
}


class OdxTools:
    def __init__(self, root: Path | str = ".") -> None:
        self.root = Path(root).resolve()
        self.engine = Engine.load(self.root)
        self._plans: dict[str, EditPlan] = {}
        self._ids = itertools.count(1)

    # -- helpers ----------------------------------------------------------- #
    def _j(self, obj: Any) -> Any:
        return serialize.to_jsonable(obj, relative_to=self.root)

    def _scope(self, modules: list[str] | None, all_modules: bool = False) -> Scope:
        if modules:
            return Scope(label="modules", modules=tuple(modules))
        return Scope(label="all") if all_modules else Scope(label="project")

    def _propose(self, plan: EditPlan) -> dict:
        pid = f"plan-{next(self._ids)}"
        self._plans[pid] = plan
        return {"plan_id": pid, "summary": plan.summary,
                "files": [self._j(e.file) for e in plan.edits],
                "preview": render_plan(plan, cwd=self.root),
                "next": "call apply_plan with this plan_id after the user approves"
                if plan.edits else "nothing to change"}

    # -- discovery ---------------------------------------------------------- #
    def list_modules(self, project_only: bool = True) -> list[str]:
        """Modules in load order (project modules only by default)."""
        return self.engine.modules(project_only=project_only)

    def list_models(self, prefix: str = "", module: str | None = None,
                    limit: int = 500) -> list[dict]:
        """Model names (optionally filtered by prefix / contributing module)."""
        return self._j(self.engine.list_models(prefix, module)[:limit])

    def get_model(self, name: str) -> dict:
        """Effective model: _inherit chain, _inherits delegates, every field with
        type/attributes/provenance/resolution. Use before writing code for it."""
        m = self.engine.get_model(name)
        if m is None:
            return {"found": False, "hint": _HINT[Resolution.DYNAMIC]}
        return {"found": True, **self._j(m)}

    def field_exists(self, model: str, field: str) -> dict:
        """Tri-state answer: exists / provably absent / unknowable statically."""
        st = self.engine.field_state(model, field)
        info = self.engine.get_field(model, field) if st is Resolution.STATIC else None
        return {"model": model, "field": field, "exists": st is Resolution.STATIC,
                "state": st.value, "hint": _HINT[st],
                "field_info": self._j(info) if info else None}

    def find_references(self, target: str, kinds: list[str] | None = None,
                        limit: int = 200) -> dict:
        """Where-used for `model` or `model.field` (or a method): every view,
        domain, depends, report, action, rule, ACL, data record... (RFC §7)."""
        u = self.engine.usage(target)
        refs = [r for r in u.references if not kinds or r.kind in kinds]
        data = self._j(u)
        data["references"] = self._j(refs[:limit])
        data["truncated"] = len(refs) > limit
        return data

    # -- analysis ----------------------------------------------------------- #
    def check(self, modules: list[str] | None = None, only: list[str] | None = None,
              all_modules: bool = False) -> dict:
        """Static diagnostics (no DB). `only`: views, fields, access, security,
        xmlids, manifest, data, actions, reports, python, dependencies."""
        diags = self.engine.check(self._scope(modules, all_modules), only)
        return {"count": len(diags), "diagnostics": self._j(diags)}

    def report(self, modules: list[str] | None = None) -> dict:
        """Scored health report (score/grade, categories, recommendations)."""
        rep = self.engine.report(self._scope(modules))
        data = self._j(rep)
        data["diagnostics"] = data["diagnostics"][:200]
        return data

    def validate_field(self, model: str, name: str, type: str, comodel: str | None = None,
                       inverse_name: str | None = None, currency_field: str | None = None,
                       compute: str | None = None, depends: list[str] | None = None,
                       store: bool = False, module: str | None = None) -> dict:
        """§16 field-type matrix + engine checks for a field you intend to add."""
        spec = FieldSpec(model=model, name=name, type=type, comodel=comodel,
                         inverse_name=inverse_name, currency_field=currency_field,
                         compute=compute, depends=tuple(depends or ()), store=store,
                         module=module)
        diags = self.engine.validate_field(spec)
        return {"ok": not any(d.severity.value == "error" for d in diags),
                "diagnostics": self._j(diags)}

    def upgrade_check(self, target: str, modules: list[str] | None = None) -> dict:
        """Migration findings from the project's version to `target`."""
        return self._j(self.engine.upgrade_check(target, self._scope(modules)))

    def reload(self, files: list[str] | None = None) -> dict:
        """Re-index after edits (only the changed files when given)."""
        self.engine.reload([Path(f) if Path(f).is_absolute() else self.root / f
                            for f in files] if files else None)
        return {"models": len(self.engine.models()), "modules": len(self.engine.modules())}

    # -- proposals (never write) -------------------------------------------- #
    def plan_add_field(self, model: str, name: str, type: str, module: str | None = None,
                       comodel: str | None = None, inverse_name: str | None = None,
                       string: str | None = None, required: bool = False,
                       currency_field: str | None = None, compute: str | None = None,
                       depends: list[str] | None = None, store: bool = False,
                       options: list[list[str]] | None = None) -> dict:
        """Propose adding a field (validated first; errors block the plan). Returns plan_id + diff."""
        spec = FieldSpec(model=model, name=name, type=type, module=module, comodel=comodel,
                         inverse_name=inverse_name, string=string, required=required,
                         currency_field=currency_field, compute=compute,
                         depends=tuple(depends or ()), store=store,
                         options=tuple((o[0], o[1] if len(o) > 1 else o[0]) for o in options or []))
        diags = self.engine.validate_field(spec)
        if any(d.severity.value == "error" for d in diags):
            return {"plan_id": None, "diagnostics": self._j(diags)}
        out = self._propose(self.engine.add_field(spec))
        out["diagnostics"] = self._j(diags)
        return out

    def plan_new_model(self, name: str, module: str, description: str = "",
                       kind: str = "Model", inherit: list[str] | None = None,
                       with_views: bool = True) -> dict:
        """Propose a new model (class, ACL row, views, action, menu). Returns plan_id + diff."""
        return self._propose(self.engine.new_model(ModelSpec(
            name=name, module=module, description=description, kind=kind,
            inherit=tuple(inherit or ()), with_views=with_views, with_menu=with_views)))

    def plan_new_module(self, name: str, depends: list[str] | None = None,
                        summary: str = "", addons_path: str | None = None) -> dict:
        """Propose a new module skeleton. Returns plan_id + diff."""
        path = Path(addons_path) if addons_path else self.engine.addons_paths()[0]
        return self._propose(self.engine.new_module(ModuleSpec(
            name=name, addons_path=path, summary=summary,
            depends=tuple(depends or ("base",)))))

    def plan_add_view(self, model: str, kind: str, module: str,
                      inherit_xmlid: str | None = None, fields: list[str] | None = None) -> dict:
        """Propose a view (form/list/kanban/search) or an inherited view. Returns plan_id + diff."""
        return self._propose(self.engine.add_view(ViewSpec(
            model=model, kind=kind, module=module, inherit_xmlid=inherit_xmlid,
            fields=tuple(fields or ()))))

    def plan_add_menu(self, label: str, module: str, parent: str | None = None,
                      model: str | None = None) -> dict:
        """Propose a menu item (and a window action when `model` is given)."""
        return self._propose(self.engine.add_menu(MenuSpec(
            label=label, module=module, parent=parent, model=model)))

    def plan_fix(self, only: list[str] | None = None, modules: list[str] | None = None) -> dict:
        """Safe fixes for current diagnostics (missing ACLs, manifest depends)."""
        return self._propose(self.engine.fix(self._scope(modules), only))

    def plan_upgrade(self, target: str, modules: list[str] | None = None) -> dict:
        """Propose the mechanical upgrade rewrites to `target` (attrs->expressions, tree->list, ...)."""
        return self._propose(self.engine.upgrade_apply(target, self._scope(modules)))

    def apply_plan(self, plan_id: str) -> dict:
        """WRITES the files of a previously proposed plan (after user approval)."""
        plan = self._plans.pop(plan_id, None)
        if plan is None:
            return {"applied": False, "error": f"unknown or already-applied plan {plan_id!r}"}
        written = _apply(plan)
        self.engine.reload([Path(p) for p in written])
        return {"applied": True, "files": [self._j(Path(p)) for p in written]}
