"""Engine-side wiring for the Phase 2 generators (mixed into ``Engine``).

Resolves *where* a generator applies (module directory, the file defining a
model, the model's known fields) using read-only discovery — the module graph
and the Python/XML parsers — then delegates to the pure generators in
``scaffold``. Kept apart from ``api.py`` so the engine pipeline (Phase 3) can
evolve there independently; once it lands, these lookups should defer to the
semantic index.

Read-only: every lookup reads disk; nothing here writes (C3).
"""

from __future__ import annotations

from pathlib import Path

from odx.engine.contracts import (
    Diagnostic,
    EditPlan,
    FieldSpec,
    MenuSpec,
    ModelSpec,
    ModuleSpec,
    ViewSpec,
)
from odx.engine.generators import scaffold
from odx.engine.generators.base import GeneratorError, disk_read
from odx.engine.generators.fields import validate_spec
from odx.engine.loadorder import ModuleGraph
from odx.engine.model.raw import RawModel
from odx.engine.parser.python import parse_python

_SKIP_DIRS = {"tests", "migrations", "upgrades", "static", "__pycache__"}


class _GeneratorMixin:
    """Generator methods of ``Engine`` (RFC §11 "generate"). Expects
    ``self.project_root``; optional ``self._addons_paths`` / ``self.odoo_version``."""

    project_root: Path

    # -- configuration ------------------------------------------------------ #
    def addons_paths(self) -> list[Path]:
        """Directories searched for modules: ``[project_root]`` unless
        ``_addons_paths`` was set (via ``set_addons_paths``)."""
        paths = getattr(self, "_addons_paths", None)
        return [Path(p) for p in paths] if paths else [Path(self.project_root)]

    def set_addons_paths(self, paths: list[Path] | None) -> None:
        self._addons_paths = [Path(p) for p in paths] if paths else None

    def _odoo_version(self) -> str:
        return getattr(self, "odoo_version", None) or scaffold.DEFAULT_VERSION

    @staticmethod
    def _read(path: Path) -> str | None:
        return disk_read(path)

    # -- lookups ------------------------------------------------------------ #
    def _graph(self) -> ModuleGraph:
        return ModuleGraph.discover(*self.addons_paths())

    def _module_dir(self, name: str) -> Path:
        m = self._graph().manifests.get(name)
        if m is None:
            raise GeneratorError(
                f"module '{name}' not found under {', '.join(map(str, self.addons_paths()))}")
        return Path(m.path)

    def _module_py_files(self, module_dir: Path) -> list[Path]:
        out = []
        for py in sorted(module_dir.rglob("*.py")):
            rel = py.relative_to(module_dir).parts
            if any(p in _SKIP_DIRS for p in rel[:-1]):
                continue
            if py.name in ("__manifest__.py", "__openerp__.py"):
                continue
            out.append(py)
        return out

    def _find_model_defs(self, model: str, prefer_module: str | None = None
                         ) -> list[tuple[str, Path, RawModel]]:
        """Every class contributing to ``model`` as ``(module, file, raw)``,
        ``prefer_module`` first, then classes that define ``_name``."""
        hits: list[tuple[str, Path, RawModel]] = []
        for name, man in self._graph().manifests.items():
            for py in self._module_py_files(Path(man.path)):
                raw = parse_python(py, name)
                for rm in raw.models:
                    if rm.effective_name == model:
                        hits.append((name, py, rm))
        hits.sort(key=lambda h: (h[0] != prefer_module, h[2].name != model, str(h[1])))
        return hits

    def _model_fields(self, model: str) -> list[tuple[str, str]]:
        seen: dict[str, str] = {}
        for _mod, _py, rm in self._find_model_defs(model):
            for f in rm.fields:
                seen.setdefault(f.name, f.ftype)
        return list(seen.items())

    def _find_view_model(self, xmlid: str) -> str | None:
        """Model of the view ``xmlid`` (``module.id``), scanning XML data files."""
        try:
            from odx.engine.parser.xml import parse_xml
        except ImportError:                     # lxml absent: can't look it up
            return None
        mod, _, _ = xmlid.partition(".")
        g = self._graph()
        targets = [mod] if mod in g.manifests else list(g.manifests)
        for name in targets:
            mdir = Path(g.manifests[name].path)
            for xml in sorted(mdir.rglob("*.xml")):
                parsed = parse_xml(xml, name)
                for v in parsed.views:
                    if v.xmlid == xmlid or (v.xmlid or "").endswith("." + xmlid):
                        return v.model
        return None

    # -- generators (-> EditPlan; never write) ------------------------------ #
    def validate_field(self, spec: FieldSpec) -> list[Diagnostic]:
        """Field-spec diagnostics. Spec-local today (§16 matrix); engine-backed
        checks (comodel exists, name collides on the _inherit chain) join in
        Phase 4."""
        return validate_spec(spec)

    def new_module(self, spec: ModuleSpec) -> EditPlan:
        return scaffold.module_plan(spec, self._read)

    def new_model(self, spec: ModelSpec) -> EditPlan:
        return scaffold.model_plan(spec, self._module_dir(spec.module), self._read,
                                   self._odoo_version())

    def new_wizard(self, spec: ModelSpec) -> EditPlan:
        return scaffold.wizard_plan(spec, self._module_dir(spec.module), self._read,
                                    self._odoo_version())

    def add_field(self, spec: FieldSpec) -> EditPlan:
        """Insert into the class defining ``spec.model`` in ``spec.module`` (or
        wherever it is defined when no module is given). When the target module
        has no class for the model, plan an ``_inherit`` extension there."""
        hits = self._find_model_defs(spec.model, spec.module)
        in_module = [h for h in hits if spec.module is None or h[0] == spec.module]
        if in_module:
            _mod, py, rm = in_module[0]
            line = rm.location.start_line if rm.location else None
            return scaffold.field_plan(spec, py, line, self._read)
        if spec.module is None:
            raise GeneratorError(
                f"model '{spec.model}' not found; pass --module to extend it there")
        ext = ModelSpec(name=spec.model, module=spec.module, inherit=(spec.model,),
                        fields=(spec,), with_views=False, with_access=False,
                        with_menu=False)
        return scaffold.model_plan(ext, self._module_dir(spec.module), self._read,
                                   self._odoo_version())

    def add_view(self, spec: ViewSpec) -> EditPlan:
        model = spec.model
        if not model and spec.inherit_xmlid:
            model = self._find_view_model(spec.inherit_xmlid) or ""
            if not model:
                raise GeneratorError(
                    f"view '{spec.inherit_xmlid}' not found; pass --model explicitly")
            from dataclasses import replace
            spec = replace(spec, model=model)
        return scaffold.view_plan(spec, self._module_dir(spec.module), self._read,
                                  self._model_fields(model), self._odoo_version())

    def add_menu(self, spec: MenuSpec) -> EditPlan:
        return scaffold.menu_plan(spec, self._module_dir(spec.module), self._read,
                                  self._odoo_version())

    def new_report(self, spec: scaffold.ReportSpec) -> EditPlan:
        hits = self._find_model_defs(spec.model)
        defining = next((h[0] for h in hits if h[2].name == spec.model), None)
        return scaffold.report_plan(spec, self._module_dir(spec.module), self._read,
                                    model_module=defining)

    def new_controller(self, spec: scaffold.ControllerSpec) -> EditPlan:
        return scaffold.controller_plan(spec, self._module_dir(spec.module), self._read)
