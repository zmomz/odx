"""IndexBuilder — populate the SemanticIndex from every §7 channel.

Runs after the model resolver and the effective-arch builder (RFC §6: a stage
depends only on earlier stages). Inputs are the workspace's raw facts, the
resolved models (through `ModelCatalog`) and the effective views. Output: one
`SemanticIndex` whose entries each carry an honest `Resolution`.

Channel map (each method below is one channel family):
  - `_python`     _inherit/_inherits, field comodel/inverse/related/domain/
                  currency/methods, @api.depends/constrains/onchange, SQL
                  constraints, _order/_rec_name, self.env['x'], env.ref()
  - `_views`      effective arch walk with nested model context: <field>,
                  modifiers, domains, buttons, search filters
  - `_records`    actions, menus, server actions, crons, automations, record
                  rules, reports, mail templates, filters, generic data records,
                  <function>
  - `_csv`        ACL rows + generic CSV data columns
  - `_qweb`       report templates bound to their report model (QWeb paths)
  - `_xmlids`     the XML-ID graph: defined (explicit + implicit) vs referenced;
                  implicit model/field ids referenced become model/field refs
"""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

from lxml import etree

from odx.engine.contracts import Location, Resolution
from odx.engine.index import IndexEntry, RefKind, SemanticIndex, XmlIdDef
from odx.engine.index import exprs as X
from odx.engine.index.catalog import (
    VIRTUAL_WRITE_FIELDS,
    ModelCatalog,
    field_xmlid_name,
    model_xmlid_name,
)
from odx.engine.model.arch import EffectiveView, node_origin
from odx.engine.model.raw import RawRecord, RawView, XmlIdRef
from odx.engine.workspace import Workspace

VIEW_TAGS = {"form", "tree", "list", "kanban", "search", "graph", "pivot", "calendar",
             "gantt", "activity", "cohort", "map", "hierarchy", "grid", "timeline"}
MODIFIER_ATTRS = ("invisible", "readonly", "required", "column_invisible")
QWEB_EXPR_ATTRS = ("t-field", "t-esc", "t-out", "t-raw", "t-if", "t-elif",
                   "t-foreach", "t-value", "t-options")
REPORT_VARS = frozenset({"doc", "docs", "o", "object", "record"})


def _loc(file: Path | str | None, line: int) -> Location | None:
    if file is None:
        return None
    return Location(Path(file), line, 0, line, 0)


def _text_or_eval(f) -> str | None:
    if f is None:
        return None
    if f.text:
        return f.text
    return f.eval


class IndexBuilder:
    def __init__(self, ws: Workspace, catalog: ModelCatalog,
                 views: dict[str, EffectiveView], raw_views: list[RawView]) -> None:
        self.ws = ws
        self.cat = catalog
        self.views = views
        self.raw_views = {v.xmlid: v for v in raw_views if v.xmlid}
        self.raw_view_list = raw_views
        self.idx = SemanticIndex()
        # record registry: xmlid -> RawRecord (first definition)
        self.records: dict[str, RawRecord] = {}

        self._sink: list[IndexEntry] | None = None
        # modules declaring runtime-named models (`_name = 'export.%s' % x`):
        # an unknown model referenced from there may be one of those.
        self.dynamic_modules = {rmod.module for rmod in ws.raw_python()
                                for rm in rmod.models
                                if rm.resolution is Resolution.DYNAMIC
                                or "dynamic-name" in rm.notes}

    def _emit(self, e: IndexEntry) -> None:
        if self._sink is not None:
            self._sink.append(e)
        else:
            self.idx.add(e)

    # ------------------------------------------------------------------ #
    def build(self) -> SemanticIndex:
        for x in self.ws.raw_xml():
            for r in x.records:
                if r.xmlid:
                    self.records.setdefault(r.xmlid, r)
        self._python()
        self._views()
        self._records()
        self._csv()
        self._qweb()
        self._xmlids()
        return self.idx

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _model_res(self, model: str | None, module: str | None, *,
                   declarative: bool = True) -> Resolution:
        """A model-level reference: STATIC if known; UNRESOLVED only when the
        referring module's whole dependency closure is visible and none of the
        corpus defines it (declarative refs); otherwise DYNAMIC."""
        if not model:
            return Resolution.DYNAMIC
        if model in self.cat.models:
            return Resolution.STATIC
        if (declarative and module and module not in self.dynamic_modules
                and self.ws.closure_is_complete(module)):
            return Resolution.UNRESOLVED
        return Resolution.DYNAMIC

    def add_model_ref(self, kind: str, model: str | None, loc: Location | None,
                      module: str | None, detail: str = "", source: str | None = None,
                      declarative: bool = True) -> None:
        if not model:
            return
        res = self._model_res(model, module, declarative=declarative)
        self._emit(IndexEntry(kind=kind, model=model, location=loc, module=module,
                                resolution=res, detail=detail, source=source,
                                note="" if res is Resolution.STATIC else "model-unknown"))

    def add_path(self, kind: str, model: str | None, path: str, loc: Location | None,
                 module: str | None, detail: str = "", source: str | None = None,
                 strict: bool = True) -> str | None:
        """Resolve `path` from `model`, one entry per hop. Returns the final
        comodel (for nested contexts) or None."""
        if not model or not path:
            return None
        if model not in self.cat.models:
            self._emit(IndexEntry(kind=kind, model=model, field=path.split(".")[0],
                                    location=loc, module=module,
                                    resolution=Resolution.DYNAMIC, detail=detail,
                                    source=source, path=path, note="model-outside-corpus"))
            return None
        pr = self.cat.resolve_path(model, path, strict=strict)
        for hop in pr.hops:
            self._emit(IndexEntry(kind=kind, model=hop.model, field=hop.field,
                                    location=loc, module=module, resolution=hop.resolution,
                                    detail=detail, source=source, path=path, note=hop.note))
        return pr.final_model

    def add_method(self, kind: str, model: str | None, method: str, loc: Location | None,
                   module: str | None, detail: str = "", source: str | None = None) -> None:
        if not model or not method:
            return
        if model not in self.cat.models:
            res = Resolution.DYNAMIC
        else:
            res = self.cat.has_method(model, method)
        self._emit(IndexEntry(kind=kind, model=model, method=method, location=loc,
                                module=module, resolution=res, detail=detail, source=source,
                                note="" if res is Resolution.STATIC else
                                ("method-missing" if res is Resolution.UNRESOLVED
                                 else "model-incomplete")))

    def add_domain(self, kind: str, model: str | None, src: str | None, loc: Location | None,
                   module: str | None, detail: str = "", source: str | None = None,
                   value_model: str | None = None, value_kind: str | None = None) -> None:
        """Domain `src` evaluated on `model`; free names in values are fields of
        `value_model` (the record the view shows), when given."""
        if not src:
            return
        text = src
        lit = X.literal_str(src)
        if lit is not None and lit.strip().startswith("["):
            text = lit                               # python field: domain="[...]" string
        d = X.domain_refs(text)
        for p in d.paths:
            self.add_path(kind, model, p, loc, module, detail, source)
        if value_model:
            for n in d.names:
                self.add_path(value_kind or kind, value_model, n, loc, module,
                              detail + " (domain value)", source)

    # ------------------------------------------------------------------ #
    # python
    # ------------------------------------------------------------------ #
    def _python(self) -> None:
        for rmod in self.ws.raw_python():
            module = rmod.module
            for rm in rmod.models:
                M = rm.effective_name
                if M is None:
                    continue
                for p in rm.inherit:
                    if p != M:
                        self.add_model_ref(RefKind.INHERIT, p, rm.location, module,
                                           f"{M} _inherit {p}", M)
                for p, fname in rm.inherits.items():
                    self.add_model_ref(RefKind.DELEGATE, p, rm.location, module,
                                       f"{M} _inherits {p} via {fname}", M)
                    self.add_path(RefKind.DELEGATE, M, fname, rm.location, module,
                                  f"{M} delegation field", M)
                for rf in rm.fields:
                    self._python_field(M, rf, module)
                for meth in rm.methods:
                    self._python_method(M, meth, module)
                for sc in rm.sql_constraints:
                    for ident in X.sql_identifiers(sc.definition):
                        if self.cat.field(M, ident) is not None:
                            self.add_path(RefKind.SQL_CONSTRAINT, M, ident, sc.location,
                                          module, f"{M} sql constraint {sc.name}", M)
                if rm.order:
                    for part in rm.order.split(","):
                        name = part.strip().split(" ")[0]
                        if X.is_path(name):
                            self.add_path(RefKind.MODEL_ATTR, M, name, rm.location, module,
                                          f"{M}._order", M)
                if rm.rec_name:
                    self.add_path(RefKind.MODEL_ATTR, M, rm.rec_name, rm.location, module,
                                  f"{M}._rec_name", M)

    def _python_field(self, M: str, rf, module: str) -> None:
        loc = rf.location
        src = f"{M}.{rf.name}"
        if rf.comodel:
            self.add_model_ref(RefKind.FIELD_COMODEL, rf.comodel, loc, module,
                               f"field {src} -> {rf.comodel}", src)
        if rf.inverse_name and rf.comodel:
            self.add_path(RefKind.INVERSE, rf.comodel, rf.inverse_name, loc, module,
                          f"inverse of {src}", src)
        related = rf.attrs.get("related")
        if isinstance(related, str) and X.is_path(related):
            self.add_path(RefKind.RELATED, M, related, loc, module, f"related of {src}", src)
        comodel = rf.comodel
        if comodel is None:
            f = self.cat.field(M, rf.name)
            c = f.info.attributes.get("comodel") if f else None
            comodel = c if isinstance(c, str) else None
        dom = rf.exprs.get("domain")
        if dom and comodel and not dom.lstrip().startswith("lambda"):
            self.add_domain(RefKind.FIELD_DOMAIN, comodel, dom, loc, module,
                            f"domain of {src}", src)
        cf = X.literal_str(rf.exprs.get("currency_field"))
        if cf:
            self.add_path(RefKind.CURRENCY_FIELD, M, cf, loc, module,
                          f"currency_field of {src}", src)
        for key in ("compute", "inverse", "search"):
            mname = X.literal_str(rf.exprs.get(key))
            if mname:
                self.add_method(RefKind.FIELD_METHOD, M, mname, loc, module,
                                f"{key}= of {src}", src)
        sel = X.literal_str(rf.exprs.get("selection"))
        if sel and X.is_path(sel) and "." not in sel:
            self.add_method(RefKind.FIELD_METHOD, M, sel, loc, module,
                            f"selection= of {src}", src)
        grp = X.literal_str(rf.exprs.get("groups"))
        if grp:
            for g in X.groups_list(grp):
                self.idx.xmlids.refs.append(XmlIdRef(
                    xmlid=g if "." in g else f"{module}.{g}", module=module,
                    location=loc, context="python:groups", record_model=None))

    def _python_method(self, M: str, meth, module: str) -> None:
        loc = meth.location
        src = f"{M}.{meth.name}"
        for dname, kind in (("depends", RefKind.DEPENDS), ("constrains", RefKind.CONSTRAINT),
                            ("onchange", RefKind.ONCHANGE)):
            for p in meth.decorators.get(dname, []):
                if X.is_path(p):
                    self.add_path(kind, M, p, loc, module, f"@api.{dname} on {src}", src)
        for model, mloc in meth.env_models:
            self.add_model_ref(RefKind.CODE, model, mloc, module, f"env[...] in {src}", src,
                               declarative=False)
        for xid, xloc in meth.xmlid_refs:
            if "." in xid:
                self.idx.xmlids.refs.append(XmlIdRef(
                    xmlid=xid, module=module, location=xloc, context="python:env.ref"))

    # ------------------------------------------------------------------ #
    # views — effective arch walk with nested model context
    # ------------------------------------------------------------------ #
    def _views(self) -> None:
        walked: set[str] = set()
        for xid, ev in self.views.items():
            rv = self.raw_views.get(xid)
            model = ev.model or (rv.model if rv else None)
            if rv is not None and model:
                self.add_model_ref(RefKind.VIEW, model, rv.location, rv.module,
                                   f"view {xid}", xid)
            if rv is not None and rv.is_template:
                continue
            if ev.arch is not None and model:
                for origin in ev.contributors:
                    walked.add(origin)
                self._walk_arch(ev.arch, model, xid)
        # extension views not covered by any effective tree (base outside the
        # corpus / dynamic): index their raw payload against their own model.
        for v in self.raw_view_list:
            if v.is_template or not v.xmlid or v.xmlid in walked or v.arch is None:
                continue
            model = v.model or self._root_model(v)
            if model:
                self.add_model_ref(RefKind.VIEW, model, v.location, v.module,
                                   f"view {v.xmlid}", v.xmlid)
                self._walk_arch(v.arch, model, v.xmlid, approximate=True)

    def _root_model(self, v: RawView) -> str | None:
        cur, seen = v, set()
        while cur is not None and cur.xmlid not in seen:
            seen.add(cur.xmlid)
            if cur.model:
                return cur.model
            cur = self.raw_views.get(cur.inherit_ref) if cur.inherit_ref else None
        return None

    def _origin(self, el, fallback_view: str) -> tuple[Location | None, str | None, str]:
        vx, line = node_origin(el)
        vx = vx or fallback_view
        rv = self.raw_views.get(vx)
        file = rv.location.file if rv and rv.location else None
        return _loc(file, line or (el.sourceline or 0)), (rv.module if rv else None), vx

    def _walk_arch(self, arch, model: str, view_xmlid: str, approximate: bool = False) -> None:
        def visit(el, M: str | None, in_qweb: bool) -> None:
            tag = el.tag
            if not isinstance(tag, str):
                return
            if tag == "templates":                      # kanban QWeb: only <field>s count
                in_qweb = True
            loc, module, vx = self._origin(el, view_xmlid)
            detail = f"view {vx}"
            field_comodel: str | None = None
            if M and tag == "field" and el.get("name") and el.getparent() is not None \
                    and el.get("position") is None:
                name = el.get("name")
                comodel = self.add_path(RefKind.VIEW_FIELD, M, name, loc, module, detail, vx)
                if approximate:
                    comodel = None if any(c.tag in VIEW_TAGS for c in el) else comodel
                dom = el.get("domain")
                if dom and comodel:
                    self.add_domain(RefKind.VIEW_DOMAIN, comodel, dom, loc, module, detail, vx,
                                    value_model=M, value_kind=RefKind.VIEW_DOMAIN)
                fdom = el.get("filter_domain")
                if fdom:
                    self.add_domain(RefKind.SEARCH_FILTER, M, fdom, loc, module, detail, vx,
                                    value_model=None)
                field_comodel = comodel
            elif M and tag == "groupby" and el.get("name"):
                # list-view <groupby name="product_id">: its children (fields,
                # buttons) are evaluated on the grouped field's comodel
                field_comodel = self.add_path(RefKind.VIEW_FIELD, M, el.get("name"), loc,
                                              module, detail + " (groupby)", vx)
            elif M and tag == "field" and el.get("position") and el.get("name"):
                # shorthand locator on an existing field (extension spec)
                self.add_path(RefKind.VIEW_FIELD, M, el.get("name"), loc, module,
                              detail + " (locator)", vx)
            if M and not in_qweb:
                self._modifiers(el, M, loc, module, detail, vx)
                if tag == "button" and el.get("type") == "object" and el.get("name"):
                    self.add_method(RefKind.VIEW_BUTTON, M, el.get("name"), loc, module,
                                    detail, vx)
                if tag == "filter":
                    if el.get("domain"):
                        self.add_domain(RefKind.SEARCH_FILTER, M, el.get("domain"), loc,
                                        module, detail, vx)
                    for g in X.context_group_by(el.get("context")):
                        self.add_path(RefKind.SEARCH_FILTER, M, g, loc, module, detail, vx)
            if tag == "groupby":
                for c in el:
                    visit(c, field_comodel, in_qweb)
                return
            for c in el:
                if tag == "field" and isinstance(c.tag, str) and c.tag in VIEW_TAGS:
                    visit(c, field_comodel, False)       # nested subview: comodel context
                else:
                    visit(c, M, in_qweb)

        # the arch container itself (<field name="arch">) holds the view root
        for child in arch:
            visit(child, model, False)

    def _modifiers(self, el, M: str, loc, module, detail, vx) -> None:
        for attr in MODIFIER_ATTRS:
            v = el.get(attr)
            if v:
                names, parsed = X.modifier_names(v)
                for n in names:
                    self.add_path(RefKind.VIEW_MODIFIER, M, n, loc, module,
                                  f"{detail} ({attr})", vx)
        for attr, v in el.attrib.items():
            if isinstance(attr, str) and attr.startswith("decoration-") and v:
                names, _ = X.modifier_names(v)
                for n in names:
                    self.add_path(RefKind.VIEW_MODIFIER, M, n, loc, module,
                                  f"{detail} ({attr})", vx)
        if el.get("attrs"):
            d = X.attrs_refs(el.get("attrs"))
            for p in d.paths:
                self.add_path(RefKind.VIEW_MODIFIER, M, p, loc, module, f"{detail} (attrs)", vx)
        if el.get("states"):
            self.add_path(RefKind.VIEW_MODIFIER, M, "state", loc, module,
                          f"{detail} (states)", vx)

    # ------------------------------------------------------------------ #
    # data records
    # ------------------------------------------------------------------ #
    def _records(self) -> None:
        for x in self.ws.raw_xml():
            for r in x.records:
                try:
                    self._record(r)
                except (ValueError, TypeError, RecursionError):   # never crash on a record
                    continue

    def _model_of_ref(self, xid: str | None) -> str | None:
        if not xid:
            return None
        m = self.cat.model_by_xmlid.get(xid)
        if m:
            return m
        return self.cat.model_by_local.get(xid.split(".", 1)[-1])

    def _record_model_field(self, r: RawRecord, name: str = "model_id") -> str | None:
        f = r.fields.get(name)
        if f is None:
            return None
        if f.ref:
            return self._model_of_ref(f.ref)
        if f.eval:
            for x in X.ref_calls(f.eval):
                return self._model_of_ref(x if "." in x else f"{r.module}.{x}")
        if f.text:
            # model_id search eval / plain name
            return f.text if f.text in self.cat.models else None
        return None

    def _record(self, r: RawRecord) -> None:
        mod, loc, src = r.module, r.location, r.xmlid
        rm = r.model
        if r.tag == "record" and rm and rm in self.cat.models:
            virtual = VIRTUAL_WRITE_FIELDS.get(rm, frozenset())
            for fname, f in r.fields.items():
                if fname in virtual:
                    continue
                self.add_path(RefKind.DATA_RECORD, rm, fname, f.location, mod,
                              f"record {src or '?'} ({rm})", src)
        if r.tag == "record" and rm and rm not in self.cat.models:
            self.add_model_ref(RefKind.DATA_RECORD, rm, loc, mod, f"record {src or '?'}", src)

        if rm in ("ir.actions.act_window",) or r.tag == "act_window":
            res_model = (r.attrs.get("res_model") if r.tag == "act_window"
                         else _text_or_eval(r.fields.get("res_model")))
            res_model = (X.literal_str(res_model) or res_model or "").strip() or None
            if res_model and X.is_path(res_model):
                self.add_model_ref(RefKind.ACTION, res_model, loc, mod, f"action {src}", src)
                dom = (r.attrs.get("domain") if r.tag == "act_window"
                       else _text_or_eval(r.fields.get("domain")))
                if dom:
                    self.add_domain(RefKind.ACTION_DOMAIN, res_model, dom, loc, mod,
                                    f"action {src} domain", src)
            bm = self._record_model_field(r, "binding_model_id")
            if bm:
                self.add_model_ref(RefKind.ACTION, bm, loc, mod, f"action {src} binding", src)
        elif rm == "ir.actions.server":
            M = self._record_model_field(r)
            if M:
                self.add_model_ref(RefKind.SERVER_ACTION, M, loc, mod, f"server action {src}", src)
                self._code(RefKind.SERVER_ACTION, M, r, mod, f"server action {src}")
        elif rm == "ir.cron":
            M = self._record_model_field(r)
            if M:
                self.add_model_ref(RefKind.CRON, M, loc, mod, f"cron {src}", src)
                self._code(RefKind.CRON, M, r, mod, f"cron {src}")
        elif rm == "base.automation":
            M = self._record_model_field(r)
            if M:
                self.add_model_ref(RefKind.AUTOMATION, M, loc, mod, f"automation {src}", src)
                for key in ("filter_domain", "filter_pre_domain"):
                    d = _text_or_eval(r.fields.get(key))
                    if d:
                        self.add_domain(RefKind.AUTOMATION, M, d, loc, mod,
                                        f"automation {src} {key}", src)
        elif rm == "ir.rule":
            M = self._record_model_field(r)
            if M:
                self.add_model_ref(RefKind.RECORD_RULE, M, loc, mod, f"record rule {src}", src)
                d = _text_or_eval(r.fields.get("domain_force"))
                if d:
                    self.add_domain(RefKind.RECORD_RULE, M, d, loc, mod,
                                    f"record rule {src} domain", src)
        elif rm == "ir.actions.report" or r.tag == "report":
            M = (r.attrs.get("model") if r.tag == "report"
                 else _text_or_eval(r.fields.get("model")))
            M = (M or "").strip() or None
            if M:
                self.add_model_ref(RefKind.REPORT, M, loc, mod, f"report {src}", src)
        elif rm == "mail.template":
            M = self._record_model_field(r)
            if M:
                self.add_model_ref(RefKind.MAIL_TEMPLATE, M, loc, mod, f"mail template {src}", src)
                for fname, f in r.fields.items():
                    self._qweb_fragment(RefKind.MAIL_TEMPLATE, M, f, mod,
                                        f"mail template {src}", src)
        elif rm == "ir.filters":
            M = _text_or_eval(r.fields.get("model_id"))
            M = (X.literal_str(M) or M or "").strip() or None
            if M and X.is_path(M):
                self.add_model_ref(RefKind.FILTER, M, loc, mod, f"filter {src}", src)
                d = _text_or_eval(r.fields.get("domain"))
                if d:
                    self.add_domain(RefKind.FILTER, M, d, loc, mod, f"filter {src}", src)
        elif r.tag == "function" and rm and r.attrs.get("name"):
            self.add_method(RefKind.FUNCTION, rm, r.attrs["name"], loc, mod,
                            f"<function> in {mod}", src)
        elif r.tag == "menuitem":
            act = r.attrs.get("action")
            if act:
                axid = act if "." in act else f"{mod}.{act}"
                ar = self.records.get(axid)
                if ar is not None:
                    res_model = (ar.attrs.get("res_model") if ar.tag == "act_window"
                                 else _text_or_eval(ar.fields.get("res_model")))
                    res_model = (X.literal_str(res_model) or res_model or "").strip() or None
                    if res_model and X.is_path(res_model):
                        self.add_model_ref(RefKind.MENU, res_model, loc, mod,
                                           f"menu {src}", src)

    def _code(self, kind: str, M: str, r: RawRecord, mod: str, detail: str) -> None:
        f = r.fields.get("code")
        code = f.text if f is not None and f.text else (f.eval if f is not None else None)
        if not code:
            return
        base_line = (f.location.start_line if f is not None and f.location else 0)
        file = f.location.file if f is not None and f.location else None
        for vp in X.var_paths(code, {"record", "records"}, mode="exec"):
            self.add_path(kind, M, ".".join(vp.path), _loc(file, base_line + max(vp.line - 1, 0)),
                          mod, detail, r.xmlid, strict=False)
        tree = X._parse_exec(code)
        if tree is None:
            return
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in ("model", "records", "record")):
                self.add_method(kind, M, node.func.attr,
                                _loc(file, base_line + max(getattr(node, "lineno", 1) - 1, 0)),
                                mod, detail, r.xmlid)

    # ------------------------------------------------------------------ #
    # QWeb (reports, mail templates)
    # ------------------------------------------------------------------ #
    def _qweb(self) -> None:
        # template xmlid -> model, from report actions
        bound: dict[str, str] = {}
        for r in self.records.values():
            if r.model == "ir.actions.report" or r.tag == "report":
                M = (r.attrs.get("model") if r.tag == "report"
                     else _text_or_eval(r.fields.get("model")))
                tpl = (r.attrs.get("name") if r.tag == "report"
                       else _text_or_eval(r.fields.get("report_name")))
                if M and tpl:
                    M, tpl = M.strip(), tpl.strip()
                    tpl = tpl if "." in tpl else f"{r.module}.{tpl}"
                    if M in self.cat.models:
                        bound.setdefault(tpl, M)
        templates = {v.xmlid: v for v in self.raw_view_list if v.is_template and v.xmlid}
        # extensions of a bound template share its model
        children: dict[str, list[str]] = {}
        for t in templates.values():
            if t.inherit_ref:
                children.setdefault(t.inherit_ref, []).append(t.xmlid)
        queue = [(x, {v: M for v in REPORT_VARS}) for x, M in bound.items()]
        done: set[tuple[str, str]] = set()
        # Buffer per template: a template reached with DIFFERENT models bound to
        # the same variable (web.external_layout, shared document bodies) is
        # polymorphic and usually guarded (`'company_id' in o`) — a missing
        # field there is not provable, so it is downgraded to DYNAMIC.
        buffered: dict[str, list[IndexEntry]] = {}
        envs: dict[str, set[tuple]] = {}
        while queue:
            xid, env = queue.pop()
            key = (xid, repr(sorted(env.items())))
            if key in done or len(done) > 20000:
                continue
            done.add(key)
            t = templates.get(xid)
            for c in children.get(xid, []):
                queue.append((c, env))
            if t is None or t.arch is None:
                continue
            envs.setdefault(xid, set()).add(tuple(sorted(env.items())))
            self._sink = buffered.setdefault(xid, [])
            try:
                calls = self._walk_qweb(t.arch, env, RefKind.REPORT, t.module,
                                        f"report template {xid}", xid)
            finally:
                self._sink = None
            for call, call_env in calls:
                cx = call if "." in call else f"{t.module}.{call}"
                if cx in templates:
                    queue.append((cx, call_env))
        for xid, entries in buffered.items():
            poly = len(envs.get(xid, ())) > 1
            for e in entries:
                if poly and e.resolution is Resolution.UNRESOLVED:
                    e = replace(e, resolution=Resolution.DYNAMIC, note="polymorphic-template")
                self.idx.add(e)

    def _walk_qweb(self, root, env: dict[str, str], kind: str, module: str, detail: str,
                   source: str) -> list[tuple[str, dict[str, str]]]:
        calls: list[tuple[str, dict[str, str]]] = []
        rv = self.raw_views.get(source)
        file = rv.location.file if rv and rv.location else None

        def resolve_expr(expr: str, scope: dict[str, str], line: int) -> str | None:
            last = None
            for vp in X.var_paths(expr, set(scope)):
                last = self.add_path(kind, scope[vp.var], ".".join(vp.path),
                                     _loc(file, line), module, detail, source, strict=False)
            return last

        def visit(el, scope: dict[str, str]) -> None:
            if not isinstance(el.tag, str):
                return
            line = el.sourceline or 0
            scope = dict(scope)
            fe = el.get("t-foreach")
            if fe:
                resolve_expr(fe, scope, line)
                var = el.get("t-as")
                target = self._expr_model(fe, scope)
                if var and target:
                    scope[var] = target
            for attr in QWEB_EXPR_ATTRS:
                if attr == "t-foreach":
                    continue
                v = el.get(attr)
                if v:
                    resolve_expr(v, scope, line)
            for attr, v in el.attrib.items():
                if isinstance(attr, str) and attr.startswith(("t-att-", "t-attf-")) and v:
                    resolve_expr(v if attr.startswith("t-att-") else "", scope, line)
                    for e in X.inline_expressions(v):
                        resolve_expr(e, scope, line)
            tset, tval = el.get("t-set"), el.get("t-value")
            if tset and tval:
                m = self._expr_model(tval, scope)
                if m:
                    scope[tset] = m
            tc = el.get("t-call")
            if tc and X.is_path(tc.strip()):
                call_env = dict(scope)
                for c in el:
                    if isinstance(c.tag, str) and c.get("t-set") and c.get("t-value"):
                        m = self._expr_model(c.get("t-value"), scope)
                        if m:
                            call_env[c.get("t-set")] = m
                calls.append((tc.strip(), call_env))
            for c in el:
                visit(c, scope)

        for c in root:
            visit(c, env)
        return calls

    def _expr_model(self, expr: str, scope: dict[str, str]) -> str | None:
        """Model of a simple path expression (`o.order_line`, `docs`)."""
        e = (expr or "").strip()
        if e in scope:
            return scope[e]
        if not X.is_path(e):
            return None
        var, _, rest = e.partition(".")
        if var not in scope or not rest:
            return None
        pr = self.cat.resolve_path(scope[var], rest, strict=False)
        return pr.final_model if pr.resolution is Resolution.STATIC else None

    def _qweb_fragment(self, kind: str, M: str, f, module: str, detail: str,
                       source: str | None) -> None:
        """Mail template body/subject: QWeb elements and inline {{ }} exprs with
        `object` bound to the template's model."""
        env = {"object": M}
        file = f.location.file if f.location else None
        line = f.location.start_line if f.location else 0
        texts = []
        if f.text:
            texts.append(f.text)
        el = f.element
        if el is not None and len(el):
            for sub in el.iter():
                if not isinstance(sub.tag, str):
                    continue
                for attr in ("t-out", "t-esc", "t-if", "t-elif", "t-field", "t-foreach",
                             "t-value"):
                    v = sub.get(attr)
                    if v:
                        for vp in X.var_paths(v, set(env)):
                            self.add_path(kind, M, ".".join(vp.path),
                                          _loc(file, sub.sourceline or line), module, detail,
                                          source, strict=False)
                if sub.text:
                    texts.append(sub.text)
        for t in texts:
            for e in X.inline_expressions(t):
                for vp in X.var_paths(e, set(env)):
                    self.add_path(kind, M, ".".join(vp.path), _loc(file, line), module,
                                  detail, source, strict=False)

    # ------------------------------------------------------------------ #
    # CSV
    # ------------------------------------------------------------------ #
    def _csv(self) -> None:
        for c in self.ws.raw_csv():
            if c.parse_error:
                continue
            header_loc = Location(c.file, 1, 0, 1, 0)
            if c.model == "ir.model.access":
                for row, loc in zip(c.rows, c.row_locations):
                    mref = row.get("model_id:id") or row.get("model_id/id") or ""
                    if not mref:
                        continue
                    full = mref if "." in mref else f"{c.module}.{mref}"
                    M = self._model_of_ref(full)
                    if M:
                        self.add_model_ref(RefKind.ACL, M, loc, c.module,
                                           f"ACL {row.get('id', '?')}", row.get("id"))
                continue
            if c.model in self.cat.models and c.rows:
                for h in c.rows[0].keys():
                    name = h.split(":")[0].split("/")[0].strip()
                    if name and name != "id" and X.is_path(name):
                        self.add_path(RefKind.DATA_RECORD, c.model, name, header_loc, c.module,
                                      f"CSV {c.file.name}", None)

    # ------------------------------------------------------------------ #
    # XML-ID graph
    # ------------------------------------------------------------------ #
    def _xmlids(self) -> None:
        g = self.idx.xmlids
        for x in self.ws.raw_xml():
            for r in x.records:
                if r.xmlid:
                    g.define(XmlIdDef(r.xmlid, r.module, r.model, r.location, r.tag))
            g.refs.extend(x.xmlid_refs)
        for c in self.ws.raw_csv():
            for xid, loc in c.xmlids:
                g.define(XmlIdDef(xid, c.module, c.model, loc, "csv"))
            g.refs.extend(c.xmlid_refs)
        # implicit ids: ir.model / ir.model.fields / ir.module.module
        for name, m in self.cat.models.items():
            if m.defined_in:
                g.define(XmlIdDef(f"{m.defined_in}.{model_xmlid_name(name)}", m.defined_in,
                                  "ir.model", m.location, "implicit-model"))
            for fname, rf in m.fields.items():
                mod = rf.info.defined_in
                if mod:
                    g.define(XmlIdDef(f"{mod}.{field_xmlid_name(name, fname)}", mod,
                                      "ir.model.fields", rf.info.location, "implicit-field"))
        for mod in self.ws.graph.manifests:
            g.define(XmlIdDef(f"base.module_{mod}", "base", "ir.module.module", None,
                              "implicit-module"))
            cat = self.ws.graph.manifests[mod].raw.get("category")
            if isinstance(cat, str):
                slug = cat.lower().replace(" / ", "_").replace("/", "_").replace(" ", "_")
                g.define(XmlIdDef(f"base.module_category_{slug}", "base",
                                  "ir.module.category", None, "implicit-module"))
        # implicit refs that resolve to models / fields become model/field refs
        kind_by_model = {"ir.model.access": RefKind.ACL, "ir.rule": RefKind.RECORD_RULE,
                         "base.automation": RefKind.AUTOMATION,
                         "ir.actions.server": RefKind.SERVER_ACTION, "ir.cron": RefKind.CRON,
                         "mail.template": RefKind.MAIL_TEMPLATE}
        for ref in g.refs:
            kind = kind_by_model.get(ref.record_model or "", RefKind.DATA_RECORD)
            ff = self.cat.field_by_xmlid.get(ref.xmlid)
            if ff is not None:
                self.idx.add(IndexEntry(kind=kind, model=ff[0], field=ff[1],
                                        location=ref.location, module=ref.module,
                                        detail=f"xml-id {ref.xmlid} ({ref.context})",
                                        path=ff[1]))
