"""The public API (RFC §11).

Typed, client-agnostic, returns plain data (the §10 contracts). Read-only vs
mutating is encoded in return types: query/analyze methods return
Diagnostic/*Info/*Report and never touch disk; generators return an EditPlan a
client chooses to apply. No direct-write method exists (C2, C3).

The pipeline (parse -> resolve -> arch -> index -> SemanticAPI) is built lazily
on first query and rebuilt by `reload()`; generators don't need it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from odx.engine.config import EngineConfig, load_config
from odx.engine.contracts import (
    AccessRule,
    DbSnapshot,
    Diagnostic,
    DoctorReport,
    EditPlan,
    FieldInfo,
    FieldSpec,
    HealthReport,
    InstallProbe,
    ModelInfo,
    ModelSummary,
    Reference,
    Resolution,
    Scope,
    UpgradeReport,
)
from odx.engine.generators.base import GeneratorError  # re-exported: part of the API
from odx.engine.generators.engine_glue import _GeneratorMixin
from odx.engine.loadorder import MANIFEST_NAMES, ModuleGraph

__all__ = ["ALL", "PROJECT", "Engine", "GeneratorError"]

ALL: Final[Scope] = Scope("all")
PROJECT: Final[Scope] = Scope("project")       # sentinel: the user's own modules


class Engine(_GeneratorMixin):
    """Static semantic engine over an Odoo project (addons tree)."""

    def __init__(self, project_root: Path, *, addons_paths: list[Path] | None = None,
                 context_paths: list[Path] | None = None,
                 odoo_version: str | None = None) -> None:
        self.project_root = Path(project_root)
        self._context_paths = [Path(p) for p in context_paths or []]
        self.odoo_version = odoo_version
        self._addons_paths = [Path(p) for p in addons_paths] if addons_paths else None
        self._build = None
        self._incremental = None

    # -- lifecycle -------------------------------------------------------- #
    @classmethod
    def load(cls, project_root: Path, *, addons_paths: list[Path] | None = None,
             context_paths: list[Path] | None = None,
             odoo_version: str | None = None) -> "Engine":
        """Engine over `project_root`. Without explicit paths, `.odx.toml`
        (at/above the root) supplies them; else the root is the addons path."""
        root = Path(project_root)
        if addons_paths is None and context_paths is None:
            cfg = load_config(root)
            return cls(cfg.root if cfg.raw else root, addons_paths=list(cfg.project_paths),
                       context_paths=list(cfg.context_paths),
                       odoo_version=odoo_version or cfg.odoo_version)
        return cls(root, addons_paths=addons_paths, context_paths=context_paths,
                   odoo_version=odoo_version)

    @classmethod
    def from_config(cls, cfg: EngineConfig) -> "Engine":
        return cls(cfg.root, addons_paths=list(cfg.project_paths),
                   context_paths=list(cfg.context_paths), odoo_version=cfg.odoo_version)

    def context_paths(self) -> list[Path]:
        return list(self._context_paths)

    def set_context_paths(self, paths: list[Path] | None) -> None:
        self._context_paths = [Path(p) for p in paths or []]
        self._build = None

    def set_addons_paths(self, paths: list[Path] | None) -> None:
        super().set_addons_paths(paths)
        self._build = None

    def _ensure(self):
        if self._build is None:
            from odx.engine.incremental import IncrementalPipeline
            project = self.addons_paths()
            paths = list(project) + [p for p in self._context_paths if p not in project]
            self._incremental = IncrementalPipeline(paths, project_paths=project)
            self._build = self._incremental.result
        return self._build

    @property
    def api(self):
        """The SemanticAPI (RFC §6) — what analyzers and generators consult."""
        return self._ensure().api

    def reload(self, changed: list[Path] | None = None) -> None:
        """Re-run the pipeline. With `changed`, only those files are re-parsed
        and only the models/views/index entries they touch are recomputed
        (RFC §8); without, a full rebuild."""
        if self._build is None or changed is None:
            self._build = None
            self._ensure()
            return
        self._build = self._incremental.update(changed)

    # -- query (read-only) ------------------------------------------------ #
    def get_model(self, name: str) -> ModelInfo | None:
        return self.api.get_model(name)

    def get_fields(self, model: str) -> list[FieldInfo]:
        return self.api.get_fields(model)

    def get_field(self, model: str, field: str) -> FieldInfo | None:
        return self.api.get_field(model, field)

    def list_models(self, prefix: str = "", module: str | None = None) -> list[ModelSummary]:
        """Model identities (no fields), optionally by name prefix / contributing
        module. Added at the Phase-5 gate: MCP needed it and had to reach into
        the SemanticAPI to build it."""
        out: list[ModelSummary] = []
        for name in self.api.model_names():
            if prefix and not name.startswith(prefix):
                continue
            m = self.api.resolved_model(name)
            mods = list(dict.fromkeys(p.module for p in m.contributors))
            if module and module not in mods:
                continue
            out.append(ModelSummary(name=name, kind=m.kind, defined_in=m.defined_in,
                                    complete=self.api.is_complete(name), modules=mods,
                                    resolution=m.resolution))
        return out

    def discover_modules(self) -> list[str]:
        """Module names under the project's addons paths — manifest discovery
        only, no parsing (cheap; for clients resolving `--module` defaults)."""
        return sorted(ModuleGraph.discover(*self.addons_paths()).manifests)

    @staticmethod
    def module_containing(path: Path) -> str | None:
        """Name of the module whose directory contains `path` (by manifest)."""
        p = Path(path).resolve()
        for d in (p, *p.parents):
            if any((d / n).is_file() for n in MANIFEST_NAMES):
                return d.name
        return None

    def warm(self) -> None:
        """Build the pipeline now (clients call this to show progress)."""
        self._ensure()

    def field_exists(self, model: str, field: str) -> bool:
        return self.api.field_exists(model, field)

    def field_state(self, model: str, field: str) -> Resolution:
        return self.api.field_state(model, field)

    def references_to(self, model: str, field: str | None = None,
                      kinds: list[str] | None = None) -> list[Reference]:
        return self.api.references_to(model, field, set(kinds) if kinds else None)

    def access_rules(self, model: str) -> list[AccessRule]:
        return self.api.access_rules(model)

    def models(self) -> list[str]:
        return self.api.model_names()

    def modules(self, project_only: bool = False) -> list[str]:
        return self.api.project_modules() if project_only else self.api.modules()

    def resolve_target(self, target: str) -> tuple[str | None, str | None]:
        """`sale.order.partner_id` -> ("sale.order", "partner_id");
        `sale.order` -> ("sale.order", None). Longest known model prefix wins;
        (None, None) if no prefix is a known model."""
        parts = target.strip().split(".")
        for i in range(len(parts), 0, -1):
            model = ".".join(parts[:i])
            if model in self.api.resolved_models():
                rest = parts[i:]
                return model, (".".join(rest) if rest else None)
        return None, None

    def usage(self, target: str):
        """`why`/`impact` (RFC §7/§14): one index query, two verbosities."""
        from odx.engine.semantic_api.usage import usage_report
        return usage_report(self.api, target)

    # -- analyze (read-only -> Diagnostics) ------------------------------- #
    def _scope(self, scope: Scope) -> Scope:
        if scope.label == "project" and scope.is_all:
            return self.api.default_scope()
        return scope

    def check(self, scope: Scope = PROJECT, only: list[str] | None = None) -> list[Diagnostic]:
        from odx.engine.analyzers import run_analyzers
        return run_analyzers(self.api, self._scope(scope), only)

    def report(self, scope: Scope = PROJECT, with_runtime: bool = False,
               probe: InstallProbe | None = None) -> HealthReport:
        """Scored aggregate (RFC §15). `with_runtime` folds in a `doctor` result
        built from `probe` — a fresh-install run the client obtained from the
        runtime (the engine never imports the runtime, C7)."""
        from odx.engine.report import build_report
        sc = self._scope(scope)
        doctor = self.doctor(probe) if (with_runtime and probe is not None) else None
        return build_report(self.api, sc, self.check(sc), doctor)

    # -- generate / fix (-> EditPlan; never writes) ----------------------- #
    # new_module / new_model / new_wizard / new_report / new_controller /
    # add_field / add_view / add_menu: see _GeneratorMixin
    # (odx/engine/generators/engine_glue.py).

    def validate_field(self, spec: FieldSpec) -> list[Diagnostic]:
        """§16 validations: spec-local (matrix) + engine-backed (comodel and
        inverse exist, name collides on the _inherit chain) when resolvable."""
        diags = super().validate_field(spec)
        try:
            from odx.engine.analyzers.specs import validate_field_spec
            diags += validate_field_spec(self.api, spec)
        except Exception:                      # engine-backed checks are best-effort
            pass
        return diags

    @staticmethod
    def guess_field_type(name: str) -> list[str]:
        """§16 name-based type suggestions (a client confirms; never assumed)."""
        from odx.engine.generators.fields import guess_type
        return guess_type(name)

    @staticmethod
    def normalize_field_type(t: str | None) -> str | None:
        """`many2one` / `m2o` / `Many2one` -> `Many2one` (None if unknown)."""
        from odx.engine.generators.fields import normalize_type
        return normalize_type(t)

    def fix(self, scope: Scope = PROJECT, only: list[str] | None = None) -> EditPlan:
        """One EditPlan composing every safe fix for the scope's Diagnostics."""
        from odx.engine.analyzers.fixes import compose_fixes
        return compose_fixes(self.api, self.check(scope, only))

    # -- runtime-composed (engine + runtime; see §12) --------------------- #
    # The client obtains runtime data (a DbSnapshot, an InstallProbe) and passes
    # it in: the engine composes it with static knowledge but never imports the
    # runtime (C7).
    def diff_against_db(self, snapshot: DbSnapshot,
                        scope: Scope = PROJECT) -> list[Diagnostic]:
        from odx.engine.analyzers.drift import diff_snapshot
        return diff_snapshot(self.api, snapshot, self._scope(scope))

    def doctor(self, probe: InstallProbe) -> DoctorReport:
        from odx.engine.analyzers.doctor import doctor_report
        return doctor_report(self.api, probe)

    def upgrade_check(self, target: str, scope: Scope = PROJECT) -> UpgradeReport:
        from odx.engine.migration import upgrade_check
        return upgrade_check(self.api, target, self._scope(scope), self.odoo_version)

    def upgrade_apply(self, target: str, scope: Scope = PROJECT) -> EditPlan:
        from odx.engine.migration import upgrade_apply
        return upgrade_apply(self.api, target, self._scope(scope), self.odoo_version)
