"""CLI scaffolding commands end-to-end against a tmp project (--yes, no prompts)."""

from __future__ import annotations

import ast
import shlex

import pytest
from lxml import etree
from typer.testing import CliRunner

from odx.clients.cli.main import app

runner = CliRunner()


@pytest.fixture
def proj(tmp_path, monkeypatch):
    (tmp_path / "addons").mkdir()
    (tmp_path / ".odx.toml").write_text('[odoo]\nversion = "17.0"\naddons_paths = ["./addons"]\n')
    monkeypatch.chdir(tmp_path)
    return tmp_path


def odx(cmd: str, **kw):
    return runner.invoke(app, shlex.split(cmd), **kw)


def test_scaffold_flow(proj):
    r = odx("new module library --depends base,mail")
    assert r.exit_code == 0, r.output
    mdir = proj / "addons" / "library"
    assert (mdir / "__manifest__.py").is_file()
    assert "+ __manifest__.py" in r.output

    r = odx("new model library.book")
    assert r.exit_code == 0, r.output
    assert "models/library_book.py" in r.output

    r = odx("add field partner_id --to library.book --comodel res.partner --yes")
    assert r.exit_code == 0, r.output
    assert "+    partner_id = fields.Many2one('res.partner'" in r.output   # diff shown

    r = odx("add field is_available --to library.book --yes")          # guessed Boolean
    assert r.exit_code == 0, r.output

    r = odx("add view kanban --for library.book")
    assert r.exit_code == 0, r.output
    r = odx('add menu "Books" --model library.book')
    assert r.exit_code == 0, r.output
    r = odx("new wizard library.book.import")
    assert r.exit_code == 0, r.output
    r = odx("new report book_card --model library.book")
    assert r.exit_code == 0, r.output
    r = odx("new controller library")
    assert r.exit_code == 0, r.output
    r = odx("inherit view base.view_partner_form --model res.partner --fields email")
    assert r.exit_code == 0, r.output

    src = (mdir / "models/library_book.py").read_text()
    assert "is_available = fields.Boolean" in src
    for py in mdir.rglob("*.py"):
        ast.parse(py.read_text())
    for xml in mdir.rglob("*.xml"):
        etree.parse(str(xml))
    data = ast.literal_eval((mdir / "__manifest__.py").read_text())["data"]
    assert data[0] == "security/ir.model.access.csv"
    assert all((mdir / f).is_file() for f in data)


def test_dry_run_writes_nothing(proj):
    r = odx("new module library --dry-run")
    assert r.exit_code == 0, r.output
    assert "dry run" in r.output
    assert not (proj / "addons" / "library").exists()


def test_add_field_errors_abort(proj):
    assert odx("new module library").exit_code == 0
    assert odx("new model library.book").exit_code == 0
    before = (proj / "addons/library/models/library_book.py").read_text()
    r = odx("add field partner_id --to library.book --yes")             # no comodel
    assert r.exit_code == 1
    assert "field.spec-missing-comodel" in r.output
    assert (proj / "addons/library/models/library_book.py").read_text() == before


def test_add_field_prompts_for_type(proj):
    assert odx("new module library").exit_code == 0
    assert odx("new model library.book").exit_code == 0
    r = odx("add field note --to library.book", input="Text\n")
    assert r.exit_code == 0, r.output
    assert "note = fields.Text" in (proj / "addons/library/models/library_book.py").read_text()


def test_generator_error_is_reported(proj):
    assert odx("new module library").exit_code == 0
    r = odx("new module library")
    assert r.exit_code == 1
    assert "already exists" in r.output


def test_module_inference_needs_choice(proj):
    assert odx("new module a1").exit_code == 0
    assert odx("new module b1").exit_code == 0
    r = odx("new model x.y")
    assert r.exit_code == 1
    assert "--module" in r.output
    assert odx("new model x.y --module b1").exit_code == 0
