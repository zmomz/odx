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

    @property
    def is_relational(self) -> bool:
        return self.ftype in {"Many2one", "One2many", "Many2many", "Many2oneReference"}


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
class RawXmlFile:
    file: Path
    module: str
    views: list[RawView] = field(default_factory=list)
    parse_error: str | None = None
