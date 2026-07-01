"""The public API (RFC §11).

Typed, client-agnostic, returns plain data (the §10 contracts). Read-only vs
mutating is encoded in return types: query/analyze methods return
Diagnostic/*Info/*Report and never touch disk; generators return an EditPlan a
client chooses to apply. No direct-write method exists (C2, C3).

Phase 0: the surface is shaped and importable; bodies are stubs. The engine
pipeline behind it lands in Phase 3.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from odx.engine.contracts import (
    AccessRule,
    Diagnostic,
    EditPlan,
    FieldInfo,
    ModelInfo,
    Reference,
)


# Scope: the unit analyzers accept (C5/§8) — a project-wide default sentinel now,
# a real scope object (module/file/model) once incrementality lands.
@dataclass(frozen=True)
class Scope:
    label: str = "all"


ALL: Final[Scope] = Scope("all")


class Engine:
    """Static semantic engine over an Odoo project (addons tree)."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    # -- lifecycle -------------------------------------------------------- #
    @classmethod
    def load(cls, project_root: Path) -> "Engine":
        return cls(Path(project_root))

    def reload(self, changed: list[Path] | None = None) -> None:
        raise NotImplementedError("engine pipeline lands in Phase 3")

    # -- query (read-only) ------------------------------------------------ #
    def get_model(self, name: str) -> ModelInfo | None:
        raise NotImplementedError

    def get_fields(self, model: str) -> list[FieldInfo]:
        raise NotImplementedError

    def field_exists(self, model: str, field: str) -> bool:
        raise NotImplementedError

    def references_to(
        self, model: str, field: str | None = None
    ) -> list[Reference]:
        raise NotImplementedError

    def access_rules(self, model: str) -> list[AccessRule]:
        raise NotImplementedError

    # -- analyze (read-only -> Diagnostics) ------------------------------- #
    def check(
        self, scope: Scope = ALL, only: list[str] | None = None
    ) -> list[Diagnostic]:
        raise NotImplementedError("analyzers land in Phase 4")

    def report(self, scope: Scope = ALL, with_runtime: bool = False):
        raise NotImplementedError("report lands in Phase 4")

    # -- generate / fix (-> EditPlan; never writes) ----------------------- #
    def new_module(self, spec) -> EditPlan:
        raise NotImplementedError("generators land in Phase 2")

    def new_model(self, spec) -> EditPlan:
        raise NotImplementedError

    def add_field(self, spec) -> EditPlan:
        raise NotImplementedError

    def add_view(self, spec) -> EditPlan:
        raise NotImplementedError

    def fix(self, scope: Scope = ALL) -> EditPlan:
        raise NotImplementedError

    # -- runtime-composed (engine + runtime; see §12) --------------------- #
    # These *compose* runtime but the engine never imports it: a client passes
    # the runtime-produced data in. Signatures are placeholders until Phase 6.
    def diff_against_db(self, conn) -> list[Diagnostic]:
        raise NotImplementedError("runtime composition lands in Phase 6")

    def doctor(self, conn):
        raise NotImplementedError

    def upgrade_check(self, target: str):
        raise NotImplementedError

    def upgrade_apply(self, target: str) -> EditPlan:
        raise NotImplementedError
