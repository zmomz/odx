"""Engine generator methods: lookups + the no-write law (C3)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from odx.clients.edits import apply_plan
from odx.engine.api import Engine
from odx.engine.contracts import (
    FieldSpec,
    MenuSpec,
    ModelSpec,
    ModuleSpec,
    ViewSpec,
)
from odx.engine.generators import ControllerSpec, GeneratorError, ReportSpec


def _snapshot(root: Path) -> dict[str, tuple[float, bytes]]:
    return {str(p): (p.stat().st_mtime_ns, p.read_bytes())
            for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def project(tmp_path):
    eng = Engine.load(tmp_path)
    apply_plan(eng.new_module(ModuleSpec(name="library", addons_path=tmp_path)))
    apply_plan(eng.new_model(ModelSpec(name="library.book", module="library", fields=(
        FieldSpec("library.book", "name", "Char"),
        FieldSpec("library.book", "partner_id", "Many2one", comodel="res.partner"),
    ))))
    (tmp_path / "library/views/partner.xml").write_text(
        '<odoo><record id="view_partner_x" model="ir.ui.view">'
        '<field name="model">res.partner</field>'
        '<field name="arch" type="xml"><form/></field></record></odoo>')
    return eng, tmp_path


def test_engine_generators_never_write(project):
    eng, root = project
    before = _snapshot(root)
    plans = [
        eng.new_module(ModuleSpec(name="other", addons_path=root)),
        eng.new_model(ModelSpec(name="library.author", module="library")),
        eng.new_wizard(ModelSpec(name="library.wiz", module="library")),
        eng.add_field(FieldSpec("library.book", "isbn", "Char")),
        eng.add_field(FieldSpec("res.partner", "x_vip", "Boolean", module="library")),
        eng.add_view(ViewSpec("library.book", "kanban", "library")),
        eng.add_view(ViewSpec("", "form", "library", inherit_xmlid="library.view_partner_x")),
        eng.add_menu(MenuSpec("Books", "library", model="library.book")),
        eng.new_report(ReportSpec("card", "library", "library.book")),
        eng.new_controller(ControllerSpec("main", "library")),
    ]
    assert all(p.edits for p in plans)
    assert _snapshot(root) == before


def test_add_field_finds_model_file(project):
    eng, root = project
    plan = eng.add_field(FieldSpec("library.book", "isbn", "Char"))
    assert [e.file for e in plan.edits] == [root / "library/models/library_book.py"]
    apply_plan(plan)
    assert "isbn = fields.Char" in (root / "library/models/library_book.py").read_text()


def test_add_field_extends_foreign_model(project):
    eng, root = project
    apply_plan(eng.add_field(FieldSpec("res.partner", "x_vip", "Boolean", module="library")))
    src = (root / "library/models/res_partner.py").read_text()
    assert "_inherit = 'res.partner'" in src and "x_vip = fields.Boolean" in src
    # a second field now goes into that class
    apply_plan(eng.add_field(FieldSpec("res.partner", "x_rank", "Integer", module="library")))
    tree = ast.parse((root / "library/models/res_partner.py").read_text())
    assert sum(isinstance(n, ast.ClassDef) for n in tree.body) == 1


def test_add_field_unknown_model_without_module(project):
    eng, _ = project
    with pytest.raises(GeneratorError):
        eng.add_field(FieldSpec("nope.model", "a", "Char"))


def test_add_view_uses_known_fields(project):
    eng, root = project
    # library.book already has a tree view from new_model
    with pytest.raises(GeneratorError, match="already exists"):
        eng.add_view(ViewSpec("library.book", "list", "library"))
    kanban = eng.add_view(ViewSpec("library.book", "kanban", "library"))
    text = kanban.edits[0].new_text
    assert '<field name="partner_id"/>' in text


def test_inherit_view_resolves_model(project):
    eng, _ = project
    plan = eng.add_view(ViewSpec("", "form", "library", inherit_xmlid="library.view_partner_x"))
    assert plan.edits[0].file.name == "res_partner_views.xml"


def test_unknown_module(project):
    eng, _ = project
    with pytest.raises(GeneratorError, match="not found"):
        eng.new_model(ModelSpec(name="a.b", module="missing"))


def test_validate_field(project):
    eng, _ = project
    diags = eng.validate_field(FieldSpec("library.book", "p_id", "Many2one"))
    assert [d.code for d in diags] == ["field.spec-missing-comodel"]


def test_addons_paths_default_and_override(tmp_path):
    eng = Engine.load(tmp_path)
    assert eng.addons_paths() == [tmp_path]
    eng.set_addons_paths([tmp_path / "addons"])
    assert eng.addons_paths() == [tmp_path / "addons"]
