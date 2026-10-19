"""The public API (RFC §11) over a real pipeline: query methods return plain
contracts; read methods never touch disk (the no-write law, RFC §22)."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from odx.engine.api import Engine
from odx.engine.contracts import FieldSpec, ModelInfo, Resolution, Scope

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "planted"


@pytest.fixture(scope="module")
def eng() -> Engine:
    return Engine.load(FIXTURE, addons_paths=[FIXTURE])


def test_get_model_folds_resolution_and_provenance(eng):
    m = eng.get_model("library.book")
    assert isinstance(m, ModelInfo) and m.defined_in == "lib_core" and m.complete
    assert m.modules[0] == "lib_core" and {"lib_ext", "lib_other"} <= set(m.modules)
    f = {x.name: x for x in m.fields}
    assert f["author_country_id"].attributes["comodel"] == "res.country"
    assert f["reviewer_ids"].resolution is Resolution.UNRESOLVED      # inverse dangles
    assert "inverse-dangling" in f["reviewer_ids"].notes
    assert f["shelf_code"].defined_in == "lib_other"


def test_field_exists_is_tri_state_underneath(eng):
    assert eng.field_exists("library.book", "title")
    assert eng.field_exists("library.book", "display_name")            # magic
    assert eng.field_state("library.book", "nope") is Resolution.UNRESOLVED
    assert eng.field_state("library.book", "x_custom") is Resolution.DYNAMIC
    assert eng.field_state("not.a.model", "x") is Resolution.DYNAMIC


def test_delegated_fields_are_visible(eng):
    # res.users _inherits res.partner -> email via delegation
    assert eng.field_exists("res.users", "email")
    assert eng.get_model("res.users").delegates == ["res.partner"]


def test_references_to_returns_reference_contracts(eng):
    refs = eng.references_to("library.book", "state")
    assert refs and all(r.model == "library.book" and r.field == "state" for r in refs)
    only = eng.references_to("library.book", "state", kinds=["view-modifier"])
    assert only and {r.kind for r in only} == {"view-modifier"}


def test_access_rules_mapped_to_models(eng):
    rules = eng.access_rules("library.book")
    assert rules and rules[0].group == "base.group_user" and rules[0].perm_read


def test_resolve_target_and_usage(eng):
    assert eng.resolve_target("library.book.author_id") == ("library.book", "author_id")
    assert eng.resolve_target("library.book") == ("library.book", None)
    u = eng.usage("library.book.state")
    assert u.known and u.by_kind.get("view-modifier") and u.definition is not None
    path = eng.usage("library.book.author_id.partner_id")      # path -> final hop
    assert path.model == "library.author" and path.field == "partner_id"
    meth = eng.usage("library.book.action_done")
    assert meth.known and meth.by_kind == {"view-button": 1}
    assert not eng.usage("nope.model").known


def test_validate_field_engine_backed(eng):
    codes = {d.code for d in eng.validate_field(
        FieldSpec(model="library.book", name="x_ids", type="One2many",
                  comodel="library.author", inverse_name="nope"))}
    assert "field.spec-inverse-missing" in codes
    codes = {d.code for d in eng.validate_field(
        FieldSpec(model="library.book", name="title", type="Char"))}
    assert "field.spec-name-collision" in codes
    codes = {d.code for d in eng.validate_field(
        FieldSpec(model="library.book", name="z_id", type="Many2one", comodel="no.such"))}
    assert "field.spec-unknown-comodel" in codes


def _tree_hash(root: Path) -> str:
    h = hashlib.sha1()
    for p in sorted(root.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def test_no_write_law(tmp_path):
    root = tmp_path / "addons"
    shutil.copytree(FIXTURE, root)
    before = _tree_hash(root)
    eng = Engine.load(root, addons_paths=[root])
    eng.check(Scope("all"))
    eng.report(Scope("all"))
    eng.usage("library.book.state")
    eng.fix(Scope("all"))                    # describes edits, never applies them
    eng.upgrade_check("18.0", Scope("all"))
    eng.upgrade_apply("18.0", Scope("all"))
    assert _tree_hash(root) == before


def test_config_supplies_project_and_context_paths(tmp_path):
    proj = tmp_path / "proj"
    shutil.copytree(FIXTURE / "lib_ext", proj / "addons" / "lib_ext")
    ctx = tmp_path / "ctx"
    for m in ("base", "lib_core", "lib_other"):
        shutil.copytree(FIXTURE / m, ctx / m)
    (proj / ".odx.toml").write_text(
        f'[odoo]\naddons_paths = ["./addons"]\ncontext_paths = ["{ctx}"]\n')
    eng = Engine.load(proj)
    assert eng.modules(project_only=True) == ["lib_ext"]
    diags = eng.check()                       # default scope = project modules only
    assert diags and all("lib_ext" in str(d.location.file) for d in diags if d.location)
