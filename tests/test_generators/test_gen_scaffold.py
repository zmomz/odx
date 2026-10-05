"""Scaffolding generators: plans applied to a tmp tree produce a valid module."""

from __future__ import annotations

import ast
import csv
from pathlib import Path

import pytest
from lxml import etree

from odx.clients.edits import PlanApplyError, apply_plan, render_plan
from odx.engine.contracts import (
    EditKind,
    EditPlan,
    FieldSpec,
    FileEdit,
    Location,
    MenuSpec,
    ModelSpec,
    ModuleSpec,
    ViewSpec,
)
from odx.engine.generators import (
    ControllerSpec,
    GeneratorError,
    ReportSpec,
    controller_plan,
    disk_read,
    field_plan,
    menu_plan,
    model_plan,
    module_plan,
    report_plan,
    view_plan,
    wizard_plan,
)
from odx.engine.generators.base import ACCESS_CSV_HEADER, manifest_add_data
from odx.engine.loadorder import parse_manifest
from odx.engine.parser.python import parse_python

BOOK_FIELDS = (
    FieldSpec("library.book", "name", "Char", required=True),
    FieldSpec("library.book", "partner_id", "Many2one", comodel="res.partner"),
    FieldSpec("library.book", "tag_ids", "Many2many", comodel="library.tag"),
    FieldSpec("library.book", "state", "Selection",
              options=(("draft", "Draft"), ("done", "Done")), default="'draft'"),
    FieldSpec("library.book", "price", "Float"),
    FieldSpec("library.book", "total", "Float", compute="_compute_total",
              depends=("price",), store=True),
)


def _module(tmp_path: Path, name: str = "library") -> Path:
    apply_plan(module_plan(ModuleSpec(name=name, addons_path=tmp_path), disk_read))
    return tmp_path / name


def _assert_valid_module(mdir: Path) -> None:
    for py in mdir.rglob("*.py"):
        ast.parse(py.read_text(), filename=str(py))
    for xml in mdir.rglob("*.xml"):
        etree.parse(str(xml))
    manifest = ast.literal_eval((mdir / "__manifest__.py").read_text())
    for f in manifest["data"]:
        assert (mdir / f).is_file(), f"manifest lists missing file {f}"
    rows = list(csv.reader((mdir / "security/ir.model.access.csv").open()))
    assert ",".join(rows[0]) == ACCESS_CSV_HEADER
    assert all(len(r) == 8 for r in rows)


# --------------------------------------------------------------------------- #
def test_module_plan_creates_expected_files(tmp_path):
    plan = module_plan(ModuleSpec(name="library", addons_path=tmp_path,
                                  depends=("base", "mail")), disk_read)
    rel = {e.file.relative_to(tmp_path / "library").as_posix() for e in plan.edits}
    assert {"__init__.py", "__manifest__.py", "models/__init__.py",
            "security/ir.model.access.csv", "README.rst",
            "static/description/index.html"} <= rel
    assert all(e.kind is EditKind.CREATE for e in plan.edits)
    apply_plan(plan)
    mdir = tmp_path / "library"
    _assert_valid_module(mdir)
    m = parse_manifest(mdir)
    assert m.depends == ("base", "mail")
    assert m.data_files == ("security/ir.model.access.csv",)


def test_module_plan_refuses_existing(tmp_path):
    _module(tmp_path)
    with pytest.raises(GeneratorError):
        module_plan(ModuleSpec(name="library", addons_path=tmp_path), disk_read)


def test_module_plan_rejects_bad_name(tmp_path):
    with pytest.raises(GeneratorError):
        module_plan(ModuleSpec(name="my-module", addons_path=tmp_path), disk_read)


# --------------------------------------------------------------------------- #
def test_model_plan_full_and_parseable(tmp_path):
    mdir = _module(tmp_path)
    spec = ModelSpec(name="library.book", module="library", fields=BOOK_FIELDS)
    apply_plan(model_plan(spec, mdir, disk_read))
    _assert_valid_module(mdir)

    raw = parse_python(mdir / "models/library_book.py", "library")
    assert raw.parse_error is None
    [model] = raw.models
    assert model.name == "library.book"
    assert [f.name for f in model.fields] == [f.name for f in BOOK_FIELDS]
    assert {f.name: f.comodel for f in model.fields}["partner_id"] == "res.partner"

    src = (mdir / "models/library_book.py").read_text()
    assert "from odoo import api, fields, models" in src
    assert "@api.depends('price')" in src and "def _compute_total(self):" in src

    manifest = ast.literal_eval((mdir / "__manifest__.py").read_text())
    assert manifest["data"] == ["security/ir.model.access.csv",
                                "views/library_book_views.xml"]
    rows = (mdir / "security/ir.model.access.csv").read_text().splitlines()
    assert rows[1] == ("access_library_book_user,library.book.user,model_library_book,"
                       "base.group_user,1,1,1,1")
    views = etree.parse(str(mdir / "views/library_book_views.xml")).getroot()
    ids = [el.get("id") for el in views]
    assert ids == ["view_library_book_form", "view_library_book_tree",
                   "view_library_book_search", "action_library_book", "menu_library_book"]
    assert views.find(".//tree") is not None
    assert "models/library_book.py" not in (mdir / "models/__init__.py").read_text()
    assert "from . import library_book" in (mdir / "models/__init__.py").read_text()


def test_model_plan_version_18_uses_list(tmp_path):
    mdir = _module(tmp_path)
    apply_plan(model_plan(ModelSpec(name="library.book", module="library"), mdir,
                          disk_read, version="18.0"))
    text = (mdir / "views/library_book_views.xml").read_text()
    assert "<list " in text and "<tree" not in text
    assert "<field name=\"view_mode\">list,form</field>" in text


def test_model_plan_is_idempotent(tmp_path):
    mdir = _module(tmp_path)
    spec = ModelSpec(name="library.book", module="library", fields=BOOK_FIELDS)
    apply_plan(model_plan(spec, mdir, disk_read))
    before = {p: p.read_text() for p in mdir.rglob("*") if p.is_file()}
    again = model_plan(spec, mdir, disk_read)
    assert again.edits == []
    apply_plan(again)
    assert {p: p.read_text() for p in mdir.rglob("*") if p.is_file()} == before
    init = (mdir / "models/__init__.py").read_text()
    assert init.count("from . import library_book") == 1


def test_model_plan_on_existing_handwritten_module(tmp_path):
    mdir = tmp_path / "legacy"
    (mdir / "models").mkdir(parents=True)
    (mdir / "__init__.py").write_text("from . import models\n")
    (mdir / "models/__init__.py").write_text("from . import thing\n")
    (mdir / "models/thing.py").write_text("")
    (mdir / "__manifest__.py").write_text(
        "{\n    'name': 'Legacy',  # keep me\n    'depends': ['base'],\n"
        "    'data': [\n        'views/thing.xml',\n    ],\n}\n")
    (mdir / "views").mkdir()
    (mdir / "views/thing.xml").write_text("<odoo/>\n")
    apply_plan(model_plan(ModelSpec(name="legacy.item", module="legacy"), mdir, disk_read))
    text = (mdir / "__manifest__.py").read_text()
    assert "# keep me" in text
    data = ast.literal_eval(text)["data"]
    assert data == ["security/ir.model.access.csv", "views/thing.xml",
                    "views/legacy_item_views.xml"]
    assert (mdir / "models/__init__.py").read_text() == (
        "from . import thing\nfrom . import legacy_item\n")


def test_inherit_only_model(tmp_path):
    mdir = _module(tmp_path)
    plan = model_plan(ModelSpec(name="res.partner", module="library", inherit=("res.partner",),
                                fields=(FieldSpec("res.partner", "x_vip", "Boolean"),)),
                      mdir, disk_read)
    apply_plan(plan)
    src = (mdir / "models/res_partner.py").read_text()
    assert "_inherit = 'res.partner'" in src and "_name" not in src
    assert not (mdir / "views/res_partner_views.xml").exists()
    rows = (mdir / "security/ir.model.access.csv").read_text().splitlines()
    assert rows == [ACCESS_CSV_HEADER]
    [m] = parse_python(mdir / "models/res_partner.py", "library").models
    assert m.effective_name == "res.partner" and m.fields[0].name == "x_vip"


def test_abstract_model_has_no_acl_or_views(tmp_path):
    mdir = _module(tmp_path)
    apply_plan(model_plan(ModelSpec(name="library.mixin", module="library",
                                    kind="AbstractModel"), mdir, disk_read))
    assert not (mdir / "views/library_mixin_views.xml").exists()
    assert "library_mixin" not in (mdir / "security/ir.model.access.csv").read_text()
    assert "models.AbstractModel" in (mdir / "models/library_mixin.py").read_text()


def test_transient_model_gets_acl(tmp_path):
    mdir = _module(tmp_path)
    apply_plan(model_plan(ModelSpec(name="library.wiz", module="library",
                                    kind="TransientModel"), mdir, disk_read))
    assert "access_library_wiz_user" in (mdir / "security/ir.model.access.csv").read_text()
    _assert_valid_module(mdir)


def test_model_plan_rejects_invalid_field(tmp_path):
    mdir = _module(tmp_path)
    with pytest.raises(GeneratorError, match="spec-missing-comodel"):
        model_plan(ModelSpec(name="library.book", module="library",
                             fields=(FieldSpec("library.book", "p_id", "Many2one"),)),
                   mdir, disk_read)


# --------------------------------------------------------------------------- #
MULTI = '''\
from odoo import fields, models


class First(models.Model):
    _name = 'x.first'
    _description = 'First'

    name = fields.Char()

    def action_go(self):
        return True


class Second(models.Model):
    _inherit = 'x.second'

    code = fields.Char()


class Third(models.Model):
    _name = 'x.third'
'''


def test_field_plan_targets_right_class(tmp_path):
    f = tmp_path / "models.py"
    f.write_text(MULTI)
    apply_plan(field_plan(FieldSpec("x.second", "partner_id", "Many2one", comodel="res.partner"),
                          f, None, disk_read))
    apply_plan(field_plan(FieldSpec("x.first", "qty", "Integer"), f, None, disk_read))
    apply_plan(field_plan(FieldSpec("x.third", "flag", "Boolean"), f, None, disk_read))
    raw = parse_python(f, "m")
    by = {m.effective_name: [x.name for x in m.fields] for m in raw.models}
    assert by == {"x.first": ["name", "qty"], "x.second": ["code", "partner_id"],
                  "x.third": ["flag"]}
    src = f.read_text()
    # field lands before the first method, not after it
    assert src.index("qty = fields.Integer") < src.index("def action_go")


def test_field_plan_compute_adds_stub_and_api_import(tmp_path):
    f = tmp_path / "m.py"
    f.write_text(MULTI)
    plan = field_plan(FieldSpec("x.first", "total", "Float", compute="_compute_total",
                                depends=("name",), store=True), f, None, disk_read)
    assert [e.kind for e in plan.edits] == [EditKind.REPLACE]
    assert plan.edits[0].range is None
    apply_plan(plan)
    src = f.read_text()
    tree = ast.parse(src)
    assert "from odoo import api, fields, models" in src
    first = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "First")
    assert "_compute_total" in [n.name for n in first.body if isinstance(n, ast.FunctionDef)]


def test_field_plan_duplicate_and_missing_class(tmp_path):
    f = tmp_path / "m.py"
    f.write_text(MULTI)
    with pytest.raises(GeneratorError, match="already exists"):
        field_plan(FieldSpec("x.first", "name", "Char"), f, None, disk_read)
    with pytest.raises(GeneratorError, match="no class"):
        field_plan(FieldSpec("x.nope", "a", "Char"), f, None, disk_read)


def test_field_plan_by_line_hint(tmp_path):
    f = tmp_path / "m.py"
    f.write_text(MULTI)
    line = MULTI.splitlines().index("class Third(models.Model):") + 1
    apply_plan(field_plan(FieldSpec("x.third", "z", "Char"), f, line, disk_read))
    assert "z = fields.Char" in f.read_text().split("class Third")[1]


# --------------------------------------------------------------------------- #
def test_view_plan_extends_and_inherits(tmp_path):
    mdir = _module(tmp_path)
    apply_plan(model_plan(ModelSpec(name="library.book", module="library",
                                    fields=BOOK_FIELDS), mdir, disk_read))
    fields = [(f.name, f.type) for f in BOOK_FIELDS]
    apply_plan(view_plan(ViewSpec("library.book", "kanban", "library"), mdir, disk_read, fields))
    root = etree.parse(str(mdir / "views/library_book_views.xml")).getroot()
    assert "view_library_book_kanban" in [el.get("id") for el in root]
    with pytest.raises(GeneratorError, match="already exists"):
        view_plan(ViewSpec("library.book", "kanban", "library"), mdir, disk_read, fields)

    apply_plan(view_plan(ViewSpec("res.partner", "form", "library",
                                  inherit_xmlid="base.view_partner_form", fields=("x_vip",)),
                         mdir, disk_read, []))
    inh = etree.parse(str(mdir / "views/res_partner_views.xml")).getroot()
    rec = inh.find("record")
    assert rec.find("field[@name='inherit_id']").get("ref") == "base.view_partner_form"
    xp = rec.find(".//xpath")
    assert xp.get("expr") == "//sheet" and xp.find("field").get("name") == "x_vip"
    assert "views/res_partner_views.xml" in ast.literal_eval(
        (mdir / "__manifest__.py").read_text())["data"]
    _assert_valid_module(mdir)


def test_menu_report_controller_wizard(tmp_path):
    mdir = _module(tmp_path)
    apply_plan(model_plan(ModelSpec(name="library.book", module="library", with_menu=False),
                          mdir, disk_read))
    apply_plan(menu_plan(MenuSpec("Library", "library"), mdir, disk_read))
    apply_plan(menu_plan(MenuSpec("Books", "library", parent="menu_library",
                                  model="library.book"), mdir, disk_read))
    menus = etree.parse(str(mdir / "views/menus.xml")).getroot()
    books = menus.find("menuitem[@id='menu_books']")
    assert books.get("parent") == "menu_library"
    assert books.get("action") == "action_library_book"   # reused, not duplicated
    assert menus.find("record") is None

    apply_plan(report_plan(ReportSpec("book_card", "library", "library.book"), mdir, disk_read,
                           model_module="library"))
    rep = etree.parse(str(mdir / "report/book_card_report.xml")).getroot()
    assert rep.find("record").get("model") == "ir.actions.report"
    assert rep.find("template").get("id") == "report_book_card"

    apply_plan(controller_plan(ControllerSpec("library", "library"), mdir, disk_read))
    apply_plan(controller_plan(ControllerSpec("api", "library", route="/api/x"), mdir, disk_read))
    ctl = (mdir / "controllers/main.py").read_text()
    assert "class LibraryController" in ctl and "class ApiController" in ctl
    assert "from . import controllers" in (mdir / "__init__.py").read_text()

    apply_plan(wizard_plan(ModelSpec(name="library.import", module="library"), mdir, disk_read))
    assert (mdir / "wizard/library_import.py").is_file()
    assert "from . import wizard" in (mdir / "__init__.py").read_text()
    wiz = etree.parse(str(mdir / "wizard/library_import_views.xml")).getroot()
    assert wiz.find(".//footer/button[@name='action_confirm']") is not None
    _assert_valid_module(mdir)
    names = {m.effective_name for py in mdir.rglob("*.py")
             for m in parse_python(py, "library").models}
    assert {"library.book", "library.import"} <= names


# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("src,expected", [
    ("{'name': 'x'}", ["a.csv"]),
    ("{\n    'name': 'x',\n}\n", ["a.csv"]),
    ("{\n    'name': 'x'\n}\n", ["a.csv"]),
    ("{'name': 'x', 'data': []}", ["a.csv"]),
    ("{'data': ['views/v.xml']}", ["views/v.xml", "a.csv"]),
    ("{'data': ['views/v.xml',]}", ["views/v.xml", "a.csv"]),
])
def test_manifest_add_data_shapes(src, expected):
    out = manifest_add_data(src, ["a.csv"])
    assert ast.literal_eval(out)["data"] == expected
    assert manifest_add_data(out, ["a.csv"]) == out          # idempotent


def test_manifest_security_goes_first():
    src = "{\n    'data': [\n        'views/a.xml',\n        'views/b.xml',\n    ],\n}\n"
    out = manifest_add_data(src, ["security/ir.model.access.csv", "views/c.xml"])
    assert ast.literal_eval(out)["data"] == [
        "security/ir.model.access.csv", "views/a.xml", "views/b.xml", "views/c.xml"]


# --------------------------------------------------------------------------- #
def test_apply_plan_create_refuses_existing(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("x")
    with pytest.raises(PlanApplyError):
        apply_plan(EditPlan([FileEdit(p, EditKind.CREATE, None, "y")], "s"))
    assert p.read_text() == "x"


def test_apply_plan_insert_and_range_replace(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("line1\nline2\n")
    apply_plan(EditPlan([FileEdit(p, EditKind.INSERT, Location(p, 2, 0, 2, 0), "new\n")], "s"))
    assert p.read_text() == "line1\nnew\nline2\n"
    apply_plan(EditPlan([FileEdit(p, EditKind.REPLACE, Location(p, 1, 0, 1, 5), "L1")], "s"))
    assert p.read_text() == "L1\nnew\nline2\n"


def test_render_plan_shows_tree_and_diff(tmp_path):
    mdir = _module(tmp_path)
    plan = model_plan(ModelSpec(name="library.book", module="library"), mdir, disk_read)
    out = render_plan(plan)
    assert "+ models/library_book.py" in out
    assert "~ __manifest__.py" in out
    assert "+        'views/library_book_views.xml'," in out
    paths = apply_plan(plan, dry_run=True)
    assert paths and not (mdir / "models/library_book.py").exists()
