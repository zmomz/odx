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


@dataclass(frozen=True)
class Reference:
    kind: str                       # one of the RFC §7 reference kinds
    location: Location
    detail: str = ""


@dataclass(frozen=True)
class AccessRule:
    model: str
    group: str | None
    perm_read: bool
    perm_write: bool
    perm_create: bool
    perm_unlink: bool
    location: Location | None = None
