"""Core data contracts (RFC §10).

These are the engine's public vocabulary: plain, frozen dataclasses that every
client renders in its own idiom. The engine returns these — never formatted
output (C2), and never mutates disk (C3): mutations are *described* by EditPlan.

Nothing here depends on any client or runtime concern.
"""

from __future__ import annotations

# `field` is aliased because Diagnostic/FieldInfo have an attribute literally
# named `field` (RFC §10), which would shadow dataclasses.field in the class body.
from dataclasses import dataclass, field as dc_field
from enum import Enum
from pathlib import Path


# --------------------------------------------------------------------------- #
# Location (C4) — recorded at parse time; editor squiggles & CI annotations
# need precise ranges, and retrofitting is expensive.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Location:
    file: Path
    start_line: int
    start_col: int
    end_line: int
    end_col: int

    def __str__(self) -> str:  # convenience for clickable "file:line" refs
        return f"{self.file}:{self.start_line}:{self.start_col}"


# --------------------------------------------------------------------------- #
# Resolution — a FIRST-CLASS state, not an exception or None (Phase 0.6 review).
# Every resolved model/field/reference carries how confidently the engine knows
# it. Downstream this is load-bearing:
#   - LSP renders DYNAMIC as a muted hint, not a red squiggle.
#   - MCP tells the AI "I can't answer this statically — ask the runtime."
#   - `report` counts DYNAMIC/UNRESOLVED apart from real defects.
# A confident wrong answer destroys trust (RFC §3); an honest "unknown" doesn't.
# --------------------------------------------------------------------------- #
class Resolution(str, Enum):
    STATIC = "static"          # fully resolved from static evidence
    DYNAMIC = "dynamic"        # provably runtime-derived (e.g. _name from data);
                               # not knowable statically → route to runtime bridge
    PARTIAL = "partial"        # resolved, but with a gap (e.g. comodel via related
                               # not yet followed) — improvable, not undecidable
    UNRESOLVED = "unresolved"  # a target we expected but couldn't find; candidate
                               # for a Diagnostic once the resolver is complete


# --------------------------------------------------------------------------- #
# Provenance — which module/file/def contributed a fact. Powers `why`/`impact`
# and incremental invalidation (RFC §8): a changed file maps to dirty models.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Provenance:
    module: str
    location: Location | None = None
    via: str = ""              # mechanism: "_inherit" | "_inherits" | "own" | ...


# --------------------------------------------------------------------------- #
# Diagnostic — the unit of analysis.
# Same object -> CLI table row, editor squiggle, MCP JSON, PR annotation.
# --------------------------------------------------------------------------- #
class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
    HINT = "hint"


@dataclass(frozen=True)
class Diagnostic:
    severity: Severity
    code: str                       # stable, namespaced, e.g. "view.unknown-field"
    message: str                    # action-oriented
    model: str | None = None
    field: str | None = None
    location: Location | None = None
    suggested_fix: "EditPlan | None" = None
    related: list[Location] = dc_field(default_factory=list)


# --------------------------------------------------------------------------- #
# EditPlan (C3) — mutations are described, never performed by the engine.
# --------------------------------------------------------------------------- #
class EditKind(str, Enum):
    CREATE = "create"
    INSERT = "insert"
    REPLACE = "replace"


@dataclass(frozen=True)
class FileEdit:
    file: Path
    kind: EditKind
    range: Location | None
    new_text: str


@dataclass(frozen=True)
class EditPlan:
    edits: list[FileEdit]
    summary: str


# --------------------------------------------------------------------------- #
# Info structures — read-only views over resolved models + index.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FieldInfo:
    name: str
    type: str
    attributes: dict[str, object] = dc_field(default_factory=dict)
    defined_in: str = ""            # provenance — powers `why` + invalidation
    location: Location | None = None
    # folded in from the resolver when the Semantic API landed (RFC §10):
    resolution: Resolution = Resolution.STATIC
    notes: tuple[str, ...] = ()
    via: str = "own"                # own | _inherit | _inherits | magic


@dataclass(frozen=True)
class ModelInfo:
    name: str
    inherit_chain: list[str] = dc_field(default_factory=list)   # _inherit (extension / prototype)
    # _inherits delegation targets. Kept SEPARATE from inherit_chain because
    # delegation is a different mechanism (RFC §2, §17): a real many2one to another
    # record, not a field merge. Collapsing the two would blind every client — the
    # index couldn't attribute delegated fields' true origin, and `why`/`impact`
    # would under-report. Load-bearing for the §7 index; hence in the contract, not
    # just the resolver's internal ResolvedModel.
    delegates: list[str] = dc_field(default_factory=list)
    fields: list[FieldInfo] = dc_field(default_factory=list)   # effective (own + inherited + delegated)
    location: Location | None = None
    kind: str = "Model"             # Model | AbstractModel | TransientModel
    defined_in: str | None = None   # module whose class declares `_name` (None: outside corpus)
    modules: list[str] = dc_field(default_factory=list)   # every contributing module, load order
    description: str | None = None
    resolution: Resolution = Resolution.STATIC
    complete: bool = False          # whole model visible (missing field => provable defect)
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Reference:
    kind: str                       # one of the RFC §7 reference kinds
    location: Location
    detail: str = ""
    model: str | None = None        # the referenced model
    field: str | None = None        # the referenced field (None: model-level ref)
    module: str | None = None       # module whose source makes the reference
    resolution: Resolution = Resolution.STATIC


@dataclass(frozen=True)
class AccessRule:
    model: str
    group: str | None
    perm_read: bool
    perm_write: bool
    perm_create: bool
    perm_unlink: bool
    location: Location | None = None
    xmlid: str | None = None          # the row's external id (module.id)
    name: str | None = None
    model_ref: str | None = None      # the model's external id as written (model_x)
    module: str | None = None         # module declaring the row


# --------------------------------------------------------------------------- #
# Scope (C5/§8) — the unit analyzers accept. Empty tuples mean "no restriction";
# a populated tuple narrows to those modules / models / files.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Scope:
    label: str = "all"
    modules: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    files: tuple[Path, ...] = ()

    @property
    def is_all(self) -> bool:
        return not (self.modules or self.models or self.files)

    def includes_module(self, module: str | None) -> bool:
        return not self.modules or module in self.modules

    def includes_model(self, model: str | None) -> bool:
        return not self.models or model in self.models

    def includes_location(self, loc: "Location | None") -> bool:
        if not self.files:
            return True
        if loc is None:
            return False
        f = Path(loc.file).resolve()
        return any(f == Path(p).resolve() or Path(p).resolve() in f.parents
                   for p in self.files)


# --------------------------------------------------------------------------- #
# Generator specs (Phase 2, RFC §11/§16) — what a client gathers (flags, prompts,
# name-based guesses) and hands to a generator. The engine never assumes a type;
# a FieldSpec carries only that type's legal follow-ups (§16 matrix).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FieldSpec:
    model: str
    name: str
    type: str                                  # odoo field class name, e.g. "Many2one"
    module: str | None = None                  # module that gets the edit (None: infer)
    string: str | None = None
    comodel: str | None = None                 # relational
    inverse_name: str | None = None            # one2many
    relation: str | None = None                # many2many table
    required: bool = False
    default: str | None = None                 # python literal source text
    help: str | None = None
    options: tuple[tuple[str, str], ...] = ()  # selection (key, label)
    currency_field: str | None = None          # monetary
    digits: str | None = None                  # float/monetary
    compute: str | None = None                 # compute method name
    depends: tuple[str, ...] = ()              # @api.depends paths for a compute
    store: bool = False
    ondelete: str | None = None                # many2one
    translate: bool = False
    size: int | None = None                    # char


@dataclass(frozen=True)
class ModuleSpec:
    name: str
    addons_path: Path
    summary: str = ""
    depends: tuple[str, ...] = ("base",)
    author: str = ""
    version: str = "17.0.1.0.0"
    license: str = "LGPL-3"
    category: str = "Uncategorized"
    application: bool = False


@dataclass(frozen=True)
class ModelSpec:
    name: str                                  # e.g. "library.book"
    module: str                                # module that gets the edit
    description: str = ""
    kind: str = "Model"                        # Model | TransientModel | AbstractModel
    inherit: tuple[str, ...] = ()              # extension parents (no _name => in-place)
    fields: tuple[FieldSpec, ...] = ()
    with_views: bool = True
    with_access: bool = True
    with_menu: bool = True


@dataclass(frozen=True)
class ViewSpec:
    model: str
    kind: str                                  # form | list | kanban | search
    module: str
    inherit_xmlid: str | None = None           # inherit an existing view
    fields: tuple[str, ...] = ()               # explicit fields; else engine picks


@dataclass(frozen=True)
class MenuSpec:
    label: str
    module: str
    parent: str | None = None                  # parent menu xmlid
    model: str | None = None                   # create a window action for this model
    action: str | None = None                  # existing action xmlid


# --------------------------------------------------------------------------- #
# Reports (Phase 4/6)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class HealthReport:
    score: int                                 # 0..100
    grade: str                                 # A..F
    diagnostics: list[Diagnostic] = dc_field(default_factory=list)
    by_severity: dict[str, int] = dc_field(default_factory=dict)
    by_category: dict[str, int] = dc_field(default_factory=dict)   # code prefix -> count
    resolution: dict[str, int] = dc_field(default_factory=dict)    # Resolution -> field count
    stats: dict[str, int] = dc_field(default_factory=dict)         # modules/models/views/...
    recommendations: list[str] = dc_field(default_factory=list)
    runtime: "DoctorReport | None" = None


# --------------------------------------------------------------------------- #
# Runtime-composed data (Phase 6). The runtime PRODUCES these (it may import
# contracts); the engine CONSUMES them — it never imports the runtime (C7).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DbField:
    name: str
    type: str                                  # odoo ttype, e.g. "many2one"
    relation: str | None = None
    required: bool = False
    store: bool = True
    modules: tuple[str, ...] = ()              # ir.model.fields.modules


@dataclass(frozen=True)
class DbModel:
    name: str
    fields: dict[str, DbField] = dc_field(default_factory=dict)
    modules: tuple[str, ...] = ()
    transient: bool = False


@dataclass(frozen=True)
class DbSnapshot:
    database: str
    models: dict[str, DbModel] = dc_field(default_factory=dict)
    installed_modules: dict[str, str] = dc_field(default_factory=dict)   # name -> state
    xmlids: tuple[str, ...] = ()               # ir.model.data complete names


@dataclass(frozen=True)
class InstallProbe:
    """Raw result of a fresh-install run (runtime-produced)."""
    database: str
    modules: tuple[str, ...]
    returncode: int
    log: str = ""
    command: tuple[str, ...] = ()


@dataclass(frozen=True)
class DoctorReport:
    database: str
    modules: tuple[str, ...]
    ok: bool
    diagnostics: list[Diagnostic] = dc_field(default_factory=list)
    log_excerpt: str = ""


@dataclass(frozen=True)
class UpgradeReport:
    source: str                                # detected/declared version
    target: str
    diagnostics: list[Diagnostic] = dc_field(default_factory=list)
    stats: dict[str, int] = dc_field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Where-used (`why` / `impact`, RFC §7/§14) — one index query; clients choose
# the verbosity (terse counts vs the detailed reference list).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class UsageReport:
    target: str
    model: str | None
    field: str | None = None
    known: bool = False                  # target resolved to a model/field we know
    resolution: Resolution = Resolution.STATIC
    definition: Location | None = None   # where the model/field is declared
    defined_in: list[str] = dc_field(default_factory=list)   # contributing modules
    by_kind: dict[str, int] = dc_field(default_factory=dict)
    references: list[Reference] = dc_field(default_factory=list)
    message: str = ""                    # why it isn't known, what to do next
