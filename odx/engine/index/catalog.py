"""ModelCatalog — lookups over resolved models that the index and the semantic
API share: effective fields (plus Odoo's magic fields), method resolution over
the `_inherit` chain, dotted-path resolution, model *completeness*, and the
implicit external ids Odoo generates for models and fields.

**Completeness is what makes an UNRESOLVED honest.** A field missing from a
model only proves a defect when we can see the whole model: it is *defined* in
the corpus (some class declares its `_name`), its identity is static, no field is
built dynamically (`fields-in-loop`/`setattr-field`), and every `_inherit` /
`_inherits` parent is itself complete. Otherwise "not found" is our blind spot,
not the user's bug, and resolves DYNAMIC (RFC §10, constraint #4).
"""

from __future__ import annotations

from dataclasses import dataclass

from odx.engine.contracts import FieldInfo, Provenance, Resolution
from odx.engine.index.exprs import RECORDSET_API
from odx.engine.model.resolver import ResolvedField, ResolvedModel

# Fields every Odoo model gets from BaseModel (_log_access + identity).
MAGIC_FIELDS: dict[str, tuple[str, str | None]] = {
    "id": ("Integer", None),
    "display_name": ("Char", None),
    "create_uid": ("Many2one", "res.users"),
    "create_date": ("Datetime", None),
    "write_uid": ("Many2one", "res.users"),
    "write_date": ("Datetime", None),
    "__last_update": ("Datetime", None),
}

# Public BaseModel methods (odoo/models.py) a view button / data <function> /
# cron may call on any model without the corpus declaring them.
BASEMODEL_METHODS = frozenset({
    "create", "write", "unlink", "copy", "copy_data", "read", "search", "search_read",
    "search_count", "name_search", "name_create", "browse", "exists", "default_get",
    "fields_get", "toggle_active", "action_archive", "action_unarchive",
    "get_formview_action", "get_formview_id", "get_metadata", "load", "export_data",
    "web_read", "web_save", "web_search_read", "read_group", "web_read_group",
    "onchange", "ensure_one", "mapped", "filtered", "sorted", "with_context",
    "check_access_rights", "check_access_rule", "check_field_access_rights",
    "name_get", "get_view", "get_views", "get_empty_list_help", "_compute_display_name",
    "init", "fetch", "flush_model", "invalidate_model", "invalidate_recordset",
    "sudo", "with_user", "with_company", "user_has_groups", "has_access",
    "action_open_document", "get_base_url", "_get_view", "unlink_wizard",
})

_MAGIC_PROV = Provenance(module="base", via="magic")

# Values a data record may set that are not stored fields: the model's
# create()/write() consumes them (e.g. ir.property's `value` pseudo-field).
VIRTUAL_WRITE_FIELDS: dict[str, frozenset[str]] = {
    "ir.property": frozenset({"value"}),
}


def magic_field(name: str) -> ResolvedField | None:
    spec = MAGIC_FIELDS.get(name)
    if spec is None:
        return None
    ftype, comodel = spec
    attrs: dict[str, object] = {"readonly": True}
    if comodel:
        attrs["comodel"] = comodel
    return ResolvedField(info=FieldInfo(name=name, type=ftype, attributes=attrs,
                                        defined_in="base"),
                         provenance=_MAGIC_PROV, notes=["magic"])


@dataclass(frozen=True)
class Hop:
    model: str
    field: str
    resolution: Resolution
    note: str = ""
    found: ResolvedField | None = None


@dataclass(frozen=True)
class PathResult:
    hops: tuple[Hop, ...]
    resolution: Resolution            # worst over the hops
    final_model: str | None           # comodel after the last relational hop
    note: str = ""


_ORDER = {Resolution.STATIC: 0, Resolution.PARTIAL: 1,
          Resolution.DYNAMIC: 2, Resolution.UNRESOLVED: 3}


def worst(*rs: Resolution) -> Resolution:
    out = Resolution.STATIC
    for r in rs:
        if _ORDER[r] > _ORDER[out]:
            out = r
    return out


def model_xmlid_name(model: str) -> str:
    return "model_" + model.replace(".", "_")


def field_xmlid_name(model: str, field: str) -> str:
    return f"field_{model.replace('.', '_')}__{field}"


class ModelCatalog:
    def __init__(self, models: dict[str, ResolvedModel]) -> None:
        self.models = models
        self._complete: dict[str, bool] = {}
        self._methods: dict[str, dict[str, list[tuple[str, str]]]] = {}
        # implicit external ids -> model / (model, field)
        self.model_by_xmlid: dict[str, str] = {}
        self.field_by_xmlid: dict[str, tuple[str, str]] = {}
        # unqualified implicit names (`model_sale_order`) -> model, for refs
        # written relative to another module (ACL CSVs often do this)
        self.model_by_local: dict[str, str] = {}
        for name, m in models.items():
            local = model_xmlid_name(name)
            self.model_by_local.setdefault(local, name)
            if m.defined_in:
                self.model_by_xmlid[f"{m.defined_in}.{local}"] = name
            for fname, rf in m.fields.items():
                mod = rf.info.defined_in or m.defined_in
                if mod:
                    self.field_by_xmlid[f"{mod}.{field_xmlid_name(name, fname)}"] = (name, fname)

    # ------------------------------------------------------------------ #
    def get(self, model: str | None) -> ResolvedModel | None:
        return self.models.get(model) if model else None

    def field(self, model: str, name: str) -> ResolvedField | None:
        m = self.models.get(model)
        if m is not None:
            f = m.fields.get(name)
            if f is not None:
                return f
        return magic_field(name)

    def is_complete(self, model: str) -> bool:
        if model in self._complete:
            return self._complete[model]
        self._complete[model] = False           # cycle guard
        m = self.models.get(model)
        # AbstractModels are excluded: a mixin legitimately reads fields that
        # only its concrete inheritors declare (`hr.employee.base.child_ids`).
        ok = (m is not None and m.defined and m.kind != "AbstractModel"
              and m.resolution is not Resolution.DYNAMIC
              and not any(n in ("fields-in-loop", "setattr-field") for n in m.notes)
              and all(self.is_complete(p) for p in m.inherit_chain)
              and all(self.is_complete(p) for p in m.delegates))
        self._complete[model] = ok
        return ok

    # ------------------------------------------------------------------ #
    # methods — Odoo method resolution walks _inherit (prototype) parents;
    # _inherits delegation does NOT delegate methods.
    # ------------------------------------------------------------------ #
    def methods(self, model: str) -> dict[str, list[tuple[str, str]]]:
        """name -> [(declaring model, module)] over the model and its prototype
        ancestors (own contributions first)."""
        if model in self._methods:
            return self._methods[model]
        self._methods[model] = {}
        acc: dict[str, list[tuple[str, str]]] = {}
        m = self.models.get(model)
        if m is not None:
            for mname, contribs in m.methods.items():
                acc.setdefault(mname, []).extend((model, mod) for mod, _ in contribs)
            for p in m.inherit_chain:
                for mname, decl in self.methods(p).items():
                    acc.setdefault(mname, []).extend(decl)
        self._methods[model] = acc
        return acc

    def has_method(self, model: str, name: str) -> Resolution:
        """STATIC if found (or a BaseModel method); UNRESOLVED if the model is
        complete and nothing declares it; DYNAMIC otherwise."""
        if name in BASEMODEL_METHODS or name in self.methods(model):
            return Resolution.STATIC
        if self.is_complete(model):
            return Resolution.UNRESOLVED
        return Resolution.DYNAMIC

    # ------------------------------------------------------------------ #
    # paths
    # ------------------------------------------------------------------ #
    def resolve_path(self, model: str, path: str | tuple[str, ...] | list[str], *,
                     strict: bool = True) -> PathResult:
        """Walk `a.b.c` from `model`. `strict` (domains, depends, related, view
        fields): every segment must be a field and every non-final hop
        relational. Non-strict (QWeb, code): a segment that is a method, a
        recordset API name, or follows a non-relational field ends the walk
        without blame."""
        segs = path.split(".") if isinstance(path, str) else list(path)
        hops: list[Hop] = []
        cur: str | None = model
        for i, seg in enumerate(segs):
            last = i == len(segs) - 1
            if cur is None:
                break
            if seg.startswith("x_"):                       # studio/custom: runtime-created
                hops.append(Hop(cur, seg, Resolution.DYNAMIC, "custom-field"))
                return PathResult(tuple(hops), worst(*(h.resolution for h in hops)), None)
            if cur not in self.models:
                hops.append(Hop(cur, seg, Resolution.DYNAMIC, "model-outside-corpus"))
                return PathResult(tuple(hops), worst(*(h.resolution for h in hops)), None)
            f = self.field(cur, seg)
            if f is None:
                if not strict and (seg in RECORDSET_API or seg in self.methods(cur)
                                   or seg.startswith("_")):
                    break                                   # a method / API, not a field
                res = Resolution.UNRESOLVED if self.is_complete(cur) else Resolution.DYNAMIC
                hops.append(Hop(cur, seg, res,
                                "field-missing" if res is Resolution.UNRESOLVED
                                else "model-incomplete"))
                return PathResult(tuple(hops), worst(*(h.resolution for h in hops)), None,
                                  note=f"missing:{cur}.{seg}")
            res = Resolution.STATIC
            note = ""
            if f.resolution is Resolution.DYNAMIC:
                res, note = Resolution.DYNAMIC, "field-dynamic"
            hops.append(Hop(cur, seg, res, note, f))
            if last:
                cur = f.info.attributes.get("comodel") if isinstance(
                    f.info.attributes.get("comodel"), str) else None
                break
            comodel = f.info.attributes.get("comodel")
            if f.info.type in ("Many2one", "One2many", "Many2many") and isinstance(comodel, str):
                cur = comodel
                continue
            if f.info.type in ("Many2one", "One2many", "Many2many", "Many2oneReference",
                               "Reference"):
                # relational with unknown comodel -> can't follow, honestly
                hops[-1] = Hop(cur, seg, Resolution.DYNAMIC, "comodel-unknown", f)
                return PathResult(tuple(hops), worst(*(h.resolution for h in hops)), None)
            # non-relational with more segments
            if strict:
                hops[-1] = Hop(cur, seg, Resolution.UNRESOLVED, "hop-nonrelational", f)
                return PathResult(tuple(hops), worst(*(h.resolution for h in hops)), None,
                                  note=f"nonrelational:{cur}.{seg}")
            cur = None
            break
        return PathResult(tuple(hops), worst(*(h.resolution for h in hops)) if hops
                          else Resolution.STATIC, cur)
