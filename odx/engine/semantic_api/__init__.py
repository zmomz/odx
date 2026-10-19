"""semantic_api — the typed read-only surface over resolved models + index.

Formerly "queries" (renamed in RFC §6 to avoid reading as SQL). The ONLY thing
analyzers and generators consult: models and their effective fields (with
Resolution folded in), methods, views, the §7 reference index, ACLs/record
rules, the XML-ID graph, modules and their dependency closure.

Everything returned is plain data (C2). Nothing here parses or mutates: the
pipeline (`odx.engine.pipeline`) built the inputs; this only reads them.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from odx.engine.contracts import (
    AccessRule,
    FieldInfo,
    Location,
    ModelInfo,
    Reference,
    Resolution,
    Scope,
)
from odx.engine.index import IndexEntry, SemanticIndex, XmlIdGraph
from odx.engine.index.catalog import MAGIC_FIELDS, ModelCatalog, magic_field
from odx.engine.model.arch import EffectiveView
from odx.engine.model.raw import RawRecord, RawView
from odx.engine.model.resolver import ResolvedField, ResolvedModel
from odx.engine.workspace import ModuleSources, Workspace


def _field_info(name: str, rf: ResolvedField) -> FieldInfo:
    return replace(rf.info, name=name, resolution=rf.resolution, notes=tuple(rf.notes),
                   via=rf.provenance.via or "own")


class SemanticAPI:
    def __init__(self, ws: Workspace, models: dict[str, ResolvedModel],
                 views: dict[str, EffectiveView], raw_views: list[RawView],
                 index: SemanticIndex, access: list[AccessRule]) -> None:
        self.ws = ws
        self.catalog = ModelCatalog(models)
        self._models = models
        self._views = views
        self._raw_views = raw_views
        self._raw_view_by_id = {v.xmlid: v for v in raw_views if v.xmlid}
        self.index = index
        self._access = access
        self._records: dict[str, RawRecord] = {}
        for x in ws.raw_xml():
            for r in x.records:
                if r.xmlid:
                    self._records.setdefault(r.xmlid, r)

    # ------------------------------------------------------------------ #
    # modules
    # ------------------------------------------------------------------ #
    def modules(self) -> list[str]:
        return list(self.ws.graph.load_order())

    def project_modules(self) -> list[str]:
        return [m for m in self.modules() if m in self.ws.project_modules]

    def module(self, name: str) -> ModuleSources | None:
        return self.ws.modules.get(name)

    def module_path(self, name: str) -> Path | None:
        ms = self.ws.modules.get(name)
        return ms.path if ms else None

    def depends(self, module: str) -> tuple[str, ...]:
        return self.ws.graph.depends_of(module)

    def dependency_closure(self, module: str) -> set[str]:
        return self.ws.dependency_closure(module)

    def closure_is_complete(self, module: str) -> bool:
        return self.ws.closure_is_complete(module)

    def dynamic_modules(self) -> set[str]:
        """Modules declaring runtime-named models (`_name = 'x.%s' % y`): an
        unknown model/id referenced from there may be one of those."""
        if not hasattr(self, "_dyn_mods"):
            self._dyn_mods = {rmod.module for rmod in self.ws.raw_python()
                              for rm in rmod.models
                              if rm.resolution is Resolution.DYNAMIC or "dynamic-name" in rm.notes}
        return self._dyn_mods

    def module_of_file(self, path: Path) -> str | None:
        return self.ws.module_of_file(path)

    # ------------------------------------------------------------------ #
    # models & fields
    # ------------------------------------------------------------------ #
    def model_names(self) -> list[str]:
        return sorted(self._models)

    def resolved_model(self, name: str) -> ResolvedModel | None:
        return self._models.get(name)

    def resolved_models(self) -> dict[str, ResolvedModel]:
        return self._models

    def is_complete(self, model: str) -> bool:
        return self.catalog.is_complete(model)

    def get_model(self, name: str) -> ModelInfo | None:
        m = self._models.get(name)
        if m is None:
            return None
        mods: list[str] = []
        for p in m.contributors:
            if p.module not in mods:
                mods.append(p.module)
        return ModelInfo(
            name=m.name, inherit_chain=list(m.inherit_chain), delegates=list(m.delegates),
            fields=self.get_fields(name), location=m.location, kind=m.kind,
            defined_in=m.defined_in, modules=mods, description=m.description,
            resolution=m.resolution, complete=self.is_complete(name), notes=tuple(m.notes))

    def get_fields(self, model: str, *, include_magic: bool = False) -> list[FieldInfo]:
        m = self._models.get(model)
        if m is None:
            return []
        out = [_field_info(n, rf) for n, rf in sorted(m.fields.items())]
        if include_magic:
            have = {f.name for f in out}
            for n in MAGIC_FIELDS:
                if n not in have and n != "__last_update":
                    mf = magic_field(n)
                    out.append(_field_info(n, mf))
        return out

    def get_field(self, model: str, field: str) -> FieldInfo | None:
        rf = self.catalog.field(model, field) if model in self._models else None
        return _field_info(field, rf) if rf is not None else None

    def field_state(self, model: str, field: str) -> Resolution:
        """STATIC: exists · UNRESOLVED: provably absent (model complete) ·
        DYNAMIC: can't tell statically (model outside/incomplete, custom x_)."""
        if field.startswith("x_"):
            return Resolution.DYNAMIC
        if model in self._models and self.catalog.field(model, field) is not None:
            return Resolution.STATIC
        if model in self._models and self.is_complete(model):
            return Resolution.UNRESOLVED
        return Resolution.DYNAMIC

    def field_exists(self, model: str, field: str) -> bool:
        return self.field_state(model, field) is Resolution.STATIC

    def methods(self, model: str) -> dict[str, list[tuple[str, str]]]:
        return self.catalog.methods(model)

    def method_state(self, model: str, method: str) -> Resolution:
        if model not in self._models:
            return Resolution.DYNAMIC
        return self.catalog.has_method(model, method)

    def models_defined_in(self, module: str) -> list[str]:
        return sorted(n for n, m in self._models.items() if m.defined_in == module)

    def models_touched_by(self, module: str) -> list[str]:
        return sorted(n for n, m in self._models.items()
                      if any(p.module == module for p in m.contributors))

    def resolve_path(self, model: str, path: str, strict: bool = True):
        return self.catalog.resolve_path(model, path, strict=strict)

    # ------------------------------------------------------------------ #
    # views
    # ------------------------------------------------------------------ #
    def effective_views(self) -> dict[str, EffectiveView]:
        return self._views

    def raw_views(self) -> list[RawView]:
        return self._raw_views

    def raw_view(self, xmlid: str) -> RawView | None:
        return self._raw_view_by_id.get(xmlid)

    def views_for(self, model: str) -> list[EffectiveView]:
        return [v for v in self._views.values() if v.model == model]

    # ------------------------------------------------------------------ #
    # references (the §7 index)
    # ------------------------------------------------------------------ #
    def entries(self) -> list[IndexEntry]:
        return self.index.entries()

    def references_to(self, model: str, field: str | None = None,
                      kinds: set[str] | None = None) -> list[Reference]:
        return [e.to_reference() for e in self.index.references_to(model, field, kinds)]

    def entries_to(self, model: str, field: str | None = None,
                   kinds: set[str] | None = None) -> list[IndexEntry]:
        return self.index.references_to(model, field, kinds)

    def method_references(self, model: str, method: str) -> list[IndexEntry]:
        return self.index.method_references(model, method)

    # ------------------------------------------------------------------ #
    # security & records & xml-ids
    # ------------------------------------------------------------------ #
    def access_rules(self, model: str | None = None) -> list[AccessRule]:
        return [a for a in self._access if model is None or a.model == model]

    def records(self, model: str | None = None) -> list[RawRecord]:
        return [r for r in self._records.values() if model is None or r.model == model]

    def record(self, xmlid: str) -> RawRecord | None:
        return self._records.get(xmlid)

    def all_records(self) -> list[RawRecord]:
        return [r for x in self.ws.raw_xml() for r in x.records]

    @property
    def xmlids(self) -> XmlIdGraph:
        return self.index.xmlids

    def xmlid_defined(self, xmlid: str) -> bool:
        return self.index.xmlids.is_defined(xmlid)

    # ------------------------------------------------------------------ #
    # scope
    # ------------------------------------------------------------------ #
    def default_scope(self) -> Scope:
        """Diagnostics default to the user's own modules (not odoo/OCA deps)."""
        return Scope(label="project", modules=tuple(self.project_modules()))

    def in_scope(self, scope: Scope, *, module: str | None = None, model: str | None = None,
                 location: Location | None = None) -> bool:
        if module is None and location is not None:
            module = self.module_of_file(Path(location.file))
        return (scope.includes_module(module) and scope.includes_model(model)
                and scope.includes_location(location))


__all__ = ["SemanticAPI"]
