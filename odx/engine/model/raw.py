"""Raw (pre-resolution) model & data types — public, inspectable intermediates.

`RawModel`/`RawField`/`RawView` are public data *types* (RFC §17): a real
intermediate value, useful for debugging and the incremental path. They hold
what a single file literally declares, before `_inherit`/`_inherits`/xpath are
applied. Resolution (raw -> resolved) happens in resolver.py / arch.py.

The Phase 0.5/0.6 spike dataclasses (`tests/spike/parser.py`,
`tests/spike/xml_parser.py`) are the proven shape these graduate from — with
`Location` added and `Resolution` state attached.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from odx.engine.contracts import Location, Resolution


@dataclass
class RawField:
    name: str
    ftype: str
    location: Location | None = None
    comodel: str | None = None
    inverse_name: str | None = None
    attrs: dict[str, object] = field(default_factory=dict)
    resolution: Resolution = Resolution.STATIC   # DYNAMIC if comodel/expr runtime
    notes: list[str] = field(default_factory=list)  # why non-STATIC, for diagnostics
    # source text (ast.unparse) of expression-valued kwargs the index reads:
    # domain / context / compute / inverse / search / related / currency_field /
    # selection / default / groups. A string literal unparses quoted ("'x'").
    exprs: dict[str, str] = field(default_factory=dict)

    @property
    def is_relational(self) -> bool:
        return self.ftype in {"Many2one", "One2many", "Many2many", "Many2oneReference"}


@dataclass
class RawMethod:
    """A method on a model class, with the Odoo decorators the index reads."""
    name: str
    location: Location | None = None
    # decorator name ("depends", "constrains", "onchange", "model", ...) ->
    # literal string args (empty list when args are not literals / absent).
    decorators: dict[str, list[str]] = field(default_factory=dict)
    # `self.env['x.y']` / `self.env.ref('mod.id')` literals used in the body
    env_models: list[tuple[str, Location | None]] = field(default_factory=list)
    xmlid_refs: list[tuple[str, Location | None]] = field(default_factory=list)
    # `super()` call present (an override that keeps the chain)
    calls_super: bool = False


@dataclass
class RawSqlConstraint:
    name: str
    definition: str
    message: str = ""
    location: Location | None = None


@dataclass
class RawModel:
    """What one class body declares (RFC §6, §17)."""
    name: str | None                  # _name literal, if present
    location: Location | None = None
    kind: str = "Model"               # Model | AbstractModel | TransientModel
    inherit: list[str] = field(default_factory=list)
    inherits: dict[str, str] = field(default_factory=dict)   # delegation
    fields: list[RawField] = field(default_factory=list)
    resolution: Resolution = Resolution.STATIC   # DYNAMIC if _name/_inherit runtime
    notes: list[str] = field(default_factory=list)  # why non-STATIC, for diagnostics
    methods: list[RawMethod] = field(default_factory=list)
    sql_constraints: list[RawSqlConstraint] = field(default_factory=list)
    order: str | None = None                      # _order literal
    rec_name: str | None = None                   # _rec_name literal
    description: str | None = None                # _description literal
    class_name: str | None = None

    @property
    def is_definition(self) -> bool:
        """This class DEFINES its model (a new `_name`), rather than extending an
        existing one in place (`_inherit` only, or `_name == _inherit`)."""
        return self.name is not None and self.name not in self.inherit

    @property
    def effective_name(self) -> str | None:
        """The model this class contributes to: `_name`, else the single
        `_inherit` (extension in place), else the first of a multi-mixin list."""
        if self.name:
            return self.name
        if self.inherit:
            return self.inherit[0]
        return None


@dataclass
class RawModule:
    """All model defs a single .py file contributes."""
    file: Path
    module: str
    models: list[RawModel] = field(default_factory=list)
    parse_error: str | None = None


# --- XML side -------------------------------------------------------------- #
@dataclass
class Locator:
    """One inheritance directive inside an inheriting view's arch."""
    kind: str                         # "xpath" | "shorthand"
    expr: str | None
    position: str | None
    location: Location | None = None
    resolution: Resolution = Resolution.STATIC


@dataclass
class RawView:
    xmlid: str | None                 # full "module.id"
    module: str
    location: Location | None = None
    model: str | None = None
    inherit_ref: str | None = None    # normalized parent xmlid
    mode: str | None = None           # primary | extension (effective, defaulted)
    priority: int = 16                # Odoo `priority`; secondary apply-order key
    is_template: bool = False
    active: bool = True               # inactive (optional/customize) views aren't applied
    arch = None                       # parsed arch element (lxml), or None
    locators: list[Locator] = field(default_factory=list)
    resolution: Resolution = Resolution.STATIC   # DYNAMIC if inherit_id via eval

    @property
    def is_primary(self) -> bool:
        """A primary view starts its own arch tree; an extension view is applied
        onto its parent's. Odoo default: primary iff it has no `inherit_id`."""
        return self.mode == "primary" or (self.mode is None and self.inherit_ref is None)

    @property
    def is_inheriting(self) -> bool:
        return self.inherit_ref is not None


@dataclass
class RawRecordField:
    """One `<field>` of a data record."""
    name: str
    location: Location | None = None
    text: str | None = None            # stripped inner text (no child elements)
    ref: str | None = None             # normalized `ref=` xml-id
    eval: str | None = None            # raw `eval=` source
    element = None                     # lxml element (children: arch / html body)


@dataclass
class RawRecord:
    """Any data-file declaration: <record>, <menuitem>, <template>, <report>,
    <act_window>, <function>, <delete>. Views are ALSO RawViews; this is the
    generic shape the index reads every channel from (RFC §7)."""
    tag: str
    module: str
    xmlid: str | None = None           # normalized module.id
    model: str | None = None           # record model (`ir.ui.view`, `ir.rule`, ...)
    location: Location | None = None
    fields: dict[str, RawRecordField] = field(default_factory=dict)
    attrs: dict[str, str] = field(default_factory=dict)   # element attributes
    noupdate: bool = False
    element = None                     # lxml element


@dataclass
class XmlIdRef:
    """A use of an xml-id (ref=, inherit_id, eval ref(), %(x)d, groups=, t-call,
    menu parent/action, CSV :id columns, env.ref)."""
    xmlid: str                          # normalized module.id
    module: str                         # module whose file makes the reference
    location: Location | None = None
    context: str = ""                   # e.g. "field:inherit_id", "groups", "t-call"
    record_model: str | None = None     # model of the record containing the ref


@dataclass
class RawXmlFile:
    file: Path
    module: str
    views: list[RawView] = field(default_factory=list)
    parse_error: str | None = None
    records: list[RawRecord] = field(default_factory=list)
    xmlid_refs: list[XmlIdRef] = field(default_factory=list)


@dataclass
class RawCsvFile:
    """A CSV data file (`<model>.csv`): ACL rows or generic records."""
    file: Path
    module: str
    model: str
    rows: list[dict[str, str]] = field(default_factory=list)
    row_locations: list[Location] = field(default_factory=list)
    xmlids: list[tuple[str, Location]] = field(default_factory=list)   # defined
    xmlid_refs: list[XmlIdRef] = field(default_factory=list)
    parse_error: str | None = None
