"""index — the semantic/reference graph + XML-ID graph (RFC §7).

The moat. A first-class subsystem with an **explicit, enumerated** set of
reference kinds (`RefKind`): every channel through which Odoo can reference a
model, a field or a method — views, domains, compute-depends, QWeb reports,
server/automated actions, constraints, security (ACLs, record rules, groups),
actions, menus, data records — plus the XML-ID graph (defined vs referenced).

Each `IndexEntry` is one (model[, field | method]) use at a `Location`, carrying
its own `Resolution` (STATIC: target exists; UNRESOLVED: a complete model lacks
it — a candidate Diagnostic; DYNAMIC: outside what we can see). The index
*records*; analyzers *accuse* (RFC §6).

Targeted invalidation (C5/§8): entries are bucketed by source file and by
target model, so `drop_file()` / `drop_model()` remove exactly what a change
touches without a rebuild.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from odx.engine.contracts import Location, Reference, Resolution
from odx.engine.model.raw import XmlIdRef


class RefKind(str, Enum):
    """The enumerated §7 reference channels. Values are stable strings — they
    are what clients render and what `references_to` filters on."""
    # python
    INHERIT = "inherit"                     # _inherit parent
    DELEGATE = "delegate"                   # _inherits / delegate=True parent
    FIELD_COMODEL = "field-comodel"         # Many2one('x.y') target model
    INVERSE = "inverse"                     # One2many inverse_name
    RELATED = "related"                     # related='a.b' hops
    FIELD_DOMAIN = "field-domain"           # domain= on a relational field
    CURRENCY_FIELD = "currency-field"       # Monetary currency_field
    FIELD_METHOD = "field-method"           # compute=/inverse=/search=/selection= method
    DEPENDS = "depends"                     # @api.depends paths
    CONSTRAINT = "constraint"               # @api.constrains fields
    ONCHANGE = "onchange"                   # @api.onchange fields
    SQL_CONSTRAINT = "sql-constraint"       # _sql_constraints columns
    MODEL_ATTR = "model-attr"               # _order / _rec_name
    CODE = "code"                           # self.env['x.y']
    # views
    VIEW = "view"                           # a view declared for the model
    VIEW_FIELD = "view-field"               # <field name=...>
    VIEW_MODIFIER = "view-modifier"         # invisible/readonly/required/attrs/states/decoration
    VIEW_DOMAIN = "view-domain"             # domain= on a view <field>
    VIEW_BUTTON = "view-button"             # <button type="object" name=...>
    SEARCH_FILTER = "search-filter"         # <filter domain/context group_by>, filter_domain
    # data / actions / security
    ACTION = "action"                       # act_window res_model / binding model
    ACTION_DOMAIN = "action-domain"         # act_window domain
    MENU = "menu"                           # menu -> action -> model
    SERVER_ACTION = "server-action"         # ir.actions.server model + code
    CRON = "cron"                           # ir.cron model + code method calls
    AUTOMATION = "automation"               # base.automation model/domains/trigger fields
    RECORD_RULE = "record-rule"             # ir.rule model + domain_force
    ACL = "acl"                             # ir.model.access row
    REPORT = "report"                       # ir.actions.report model + QWeb template paths
    MAIL_TEMPLATE = "mail-template"         # mail.template model + body paths
    DATA_RECORD = "data-record"             # <record model=X><field name=f> / CSV column
    FUNCTION = "function"                   # <function model= name=>
    FILTER = "filter"                       # ir.filters model + domain


@dataclass(frozen=True)
class IndexEntry:
    kind: str
    model: str | None
    field: str | None = None
    method: str | None = None
    location: Location | None = None
    module: str | None = None               # module whose source makes the reference
    resolution: Resolution = Resolution.STATIC
    detail: str = ""                        # e.g. "view sale.view_order_form"
    source: str | None = None               # xmlid / "model.field" / "model.method" of the referrer
    path: str | None = None                 # full path as written, when from a path
    note: str = ""                          # why non-STATIC

    def __post_init__(self) -> None:
        if isinstance(self.kind, Enum):         # store the stable string value
            object.__setattr__(self, "kind", self.kind.value)

    def to_reference(self) -> Reference:
        loc = self.location or Location(Path("<unknown>"), 0, 0, 0, 0)
        return Reference(kind=self.kind, location=loc, detail=self.detail,
                         model=self.model, field=self.field or self.method,
                         module=self.module, resolution=self.resolution)


# --------------------------------------------------------------------------- #
# XML-ID graph
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class XmlIdDef:
    xmlid: str
    module: str                             # module whose file declares it
    model: str | None = None                # record model
    location: Location | None = None
    kind: str = "record"                    # record|menuitem|template|report|act_window
                                            # |csv|implicit-model|implicit-field|implicit-module


@dataclass
class XmlIdGraph:
    defined: dict[str, list[XmlIdDef]] = field(default_factory=lambda: defaultdict(list))
    refs: list[XmlIdRef] = field(default_factory=list)

    def define(self, d: XmlIdDef) -> None:
        self.defined[d.xmlid].append(d)

    def is_defined(self, xmlid: str) -> bool:
        return xmlid in self.defined

    def definition(self, xmlid: str) -> XmlIdDef | None:
        ds = self.defined.get(xmlid)
        return ds[0] if ds else None

    def refs_to(self, xmlid: str) -> list[XmlIdRef]:
        return [r for r in self.refs if r.xmlid == xmlid]

    def drop_file(self, file: Path) -> None:
        f = Path(file)
        for k in list(self.defined):
            kept = [d for d in self.defined[k]
                    if d.location is None or Path(d.location.file) != f]
            if kept:
                self.defined[k] = kept
            else:
                del self.defined[k]
        self.refs = [r for r in self.refs
                     if r.location is None or Path(r.location.file) != f]


# --------------------------------------------------------------------------- #
# the index
# --------------------------------------------------------------------------- #
class SemanticIndex:
    def __init__(self) -> None:
        self._entries: list[IndexEntry] = []
        self._seen: set[tuple] = set()
        self._by_model: dict[str, list[IndexEntry]] = defaultdict(list)
        self._by_field: dict[tuple[str, str], list[IndexEntry]] = defaultdict(list)
        self._by_file: dict[str, list[IndexEntry]] = defaultdict(list)
        self.xmlids = XmlIdGraph()

    # -- build ------------------------------------------------------------ #
    def add(self, e: IndexEntry) -> None:
        key = (e.kind, e.model, e.field, e.method, e.location, e.path)
        if key in self._seen:
            return
        self._seen.add(key)
        self._entries.append(e)
        if e.model:
            self._by_model[e.model].append(e)
            if e.field:
                self._by_field[(e.model, e.field)].append(e)
        if e.location is not None:
            self._by_file[str(Path(e.location.file))].append(e)

    # -- query ------------------------------------------------------------ #
    def __len__(self) -> int:
        return len(self._entries)

    def entries(self) -> list[IndexEntry]:
        return list(self._entries)

    def references_to(self, model: str, field: str | None = None,
                      kinds: set[str] | None = None) -> list[IndexEntry]:
        src = self._by_field.get((model, field), []) if field else self._by_model.get(model, [])
        return [e for e in src if kinds is None or e.kind in kinds]

    def method_references(self, model: str, method: str) -> list[IndexEntry]:
        return [e for e in self._by_model.get(model, []) if e.method == method]

    def in_file(self, file: Path) -> list[IndexEntry]:
        return list(self._by_file.get(str(Path(file)), []))

    def by_kind(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for e in self._entries:
            out[e.kind] += 1
        return dict(out)

    def by_resolution(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for e in self._entries:
            out[e.resolution.value] += 1
        return dict(out)

    # -- targeted invalidation (RFC §8) ---------------------------------- #
    def _rebuild_buckets(self) -> None:
        entries, self._entries = self._entries, []
        self._seen.clear()
        self._by_model.clear()
        self._by_field.clear()
        self._by_file.clear()
        for e in entries:
            self.add(e)

    def drop_file(self, file: Path) -> int:
        """Drop every entry ORIGINATING from `file` (and its xml-id facts)."""
        f = str(Path(file))
        before = len(self._entries)
        self._entries = [e for e in self._entries
                         if e.location is None or str(Path(e.location.file)) != f]
        self._rebuild_buckets()
        self.xmlids.drop_file(Path(file))
        return before - len(self._entries)

    def drop_model(self, model: str) -> int:
        """Drop every entry TARGETING `model`."""
        before = len(self._entries)
        self._entries = [e for e in self._entries if e.model != model]
        self._rebuild_buckets()
        return before - len(self._entries)


__all__ = ["IndexEntry", "RefKind", "SemanticIndex", "XmlIdDef", "XmlIdGraph"]
