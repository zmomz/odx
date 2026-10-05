"""Incrementality (RFC §8, §22): change one file -> only it is re-parsed, and the
invalidation names exactly the models/modules it affects."""

from __future__ import annotations

import shutil
from pathlib import Path

from odx.engine.api import Engine
from odx.engine.contracts import Scope

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "planted"


def _engine(tmp_path):
    root = tmp_path / "addons"
    shutil.copytree(FIXTURE, root)
    eng = Engine.load(root, addons_paths=[root])
    eng.api
    return root, eng


def test_unchanged_file_is_skipped(tmp_path):
    root, eng = _engine(tmp_path)
    f = root / "lib_core" / "models" / "library.py"
    eng.reload([f])
    inv = eng._incremental.invalidation
    assert inv.skipped == [f.resolve()] and not inv.files


def test_python_change_dirties_model_and_its_inheritors(tmp_path):
    root, eng = _engine(tmp_path)
    f = root / "lib_core" / "models" / "library.py"
    f.write_text(f.read_text().replace("    isbn = fields.Char()",
                                       "    isbn = fields.Char()\n    pages = fields.Integer()"))
    eng.reload([f])
    inv = eng._incremental.invalidation
    assert inv.files == [f.resolve()]
    assert {"library.book", "library.author"} <= inv.models
    assert "res.partner" not in inv.models and "res.country" not in inv.models
    assert eng.field_exists("library.book", "pages")
    # the analyzers can re-run over just the affected modules
    assert set(inv.scope().modules) >= {"lib_core", "lib_ext"}


def test_fixing_a_view_clears_its_diagnostic(tmp_path):
    root, eng = _engine(tmp_path)
    assert any(d.code == "view.unknown-field" for d in eng.check(Scope("all")))
    v = root / "lib_ext" / "views" / "ext_views.xml"
    v.write_text(v.read_text().replace('<field name="pages_count"/>', ""))
    eng.reload([v])
    assert not any(d.code == "view.unknown-field" for d in eng.check(Scope("all")))


def test_removed_class_still_dirties_its_model(tmp_path):
    root, eng = _engine(tmp_path)
    f = root / "lib_other" / "models" / "book.py"
    f.write_text("from odoo import models\n")
    eng.reload([f])
    assert "library.book" in eng._incremental.invalidation.models
    assert not eng.field_exists("library.book", "shelf_code")


def test_full_reload(tmp_path):
    root, eng = _engine(tmp_path)
    eng.reload()
    assert eng.get_model("library.book") is not None
