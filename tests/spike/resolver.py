"""Spike resolver: raw model defs -> a registry + resolved effective models.

Applies the mechanisms the RFC §2 calls the hard part:
  - `_inherit` (extension): merge contributions across modules onto one model.
  - `_inherits` (delegation): pull in fields of the delegated comodel.
  - multi-mixin `_inherit` lists.

It does NOT boot Odoo. It resolves what it can from static evidence and records
what it cannot as tagged failures (see catalog.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field

from .parser import FileResult, RawField, RawModel


@dataclass
class ResolvedModel:
    name: str
    kind: str
    contributions: list[RawModel] = dc_field(default_factory=list)
    inherit: set[str] = dc_field(default_factory=set)     # direct parents (extension)
    inherits: dict[str, str] = dc_field(default_factory=dict)  # delegation comodel->fk
    own_fields: dict[str, RawField] = dc_field(default_factory=dict)
    # filled during effective-field resolution:
    effective_fields: dict[str, RawField] = dc_field(default_factory=dict)
    unresolved_parents: set[str] = dc_field(default_factory=set)


@dataclass
class Registry:
    models: dict[str, ResolvedModel] = dc_field(default_factory=dict)
    # bookkeeping for the scoreboard/catalog
    file_results: list[FileResult] = dc_field(default_factory=list)

    def add(self, rm: RawModel) -> None:
        name = rm.effective_name
        if not name:
            return  # nameless (dynamic) — counted as a failure by the harness
        model = self.models.get(name)
        if model is None:
            model = ResolvedModel(name=name, kind=rm.kind)
            self.models[name] = model
        model.contributions.append(rm)
        for parent in rm.inherit:
            if parent != name:
                model.inherit.add(parent)
        model.inherits.update(rm.inherits)
        for f in rm.fields:
            # last contribution wins (mirrors Odoo override semantics, roughly)
            model.own_fields[f.name] = f


def build_registry(file_results: list[FileResult]) -> Registry:
    reg = Registry(file_results=file_results)
    for fr in file_results:
        for rm in fr.models:
            reg.add(rm)
    return reg


def resolve_effective_fields(reg: Registry) -> None:
    """Compute effective fields per model by walking _inherit + _inherits.

    Memoised, cycle-safe. A parent not in the registry is recorded as an
    unresolved parent (likely a core/base model absent from the corpus, or a
    genuinely dangling inherit — the harness distinguishes the two).
    """
    resolving: set[str] = set()
    done: set[str] = set()

    def visit(name: str) -> dict[str, RawField]:
        model = reg.models.get(name)
        if model is None:
            return {}
        if name in done:
            return model.effective_fields
        if name in resolving:
            return model.own_fields  # cycle guard: break with own fields
        resolving.add(name)

        eff: dict[str, RawField] = {}
        # 1. inherited (extension) parents first
        for parent in model.inherit:
            if parent in reg.models:
                eff.update(visit(parent))
            else:
                model.unresolved_parents.add(parent)
        # 2. delegated (_inherits) comodels
        for comodel in model.inherits:
            if comodel in reg.models:
                eff.update(visit(comodel))
            else:
                model.unresolved_parents.add(comodel)
        # 3. own fields override inherited
        eff.update(model.own_fields)

        model.effective_fields = eff
        resolving.discard(name)
        done.add(name)
        return eff

    for name in list(reg.models):
        visit(name)
