"""CLI over the engine verbs: check / report / fix / why / impact / doctor /
diff db / upgrade — rendering + exit codes, against the planted fixture."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from odx.clients.cli.main import app
from odx.engine.contracts import DbField, DbModel, DbSnapshot

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "planted"
runner = CliRunner()


@pytest.fixture()
def proj(tmp_path):
    root = tmp_path / "proj"
    shutil.copytree(FIXTURE, root / "addons")
    (root / ".odx.toml").write_text('project = "p"\n[odoo]\naddons_paths = ["./addons"]\n')
    return root


def odx(*args):
    return runner.invoke(app, list(args))


def test_check_text_lists_findings_and_fails(proj):
    r = odx("check", "--path", str(proj))
    assert r.exit_code == 1
    assert "view.unknown-field" in r.output and "pages_count" in r.output


def test_check_json_is_machine_readable(proj):
    r = odx("check", "--path", str(proj), "--format", "json", "--only", "views")
    data = json.loads(r.output)
    assert data and all(d["code"].startswith("view.") for d in data)
    assert data[0]["location"]["file"].startswith("addons/")


def test_check_github_and_sarif(proj):
    r = odx("check", "--path", str(proj), "-f", "github", "--only", "views")
    assert r.output.startswith("::error file=addons/lib_ext/views/ext_views.xml,line=")
    r = odx("check", "--path", str(proj), "-f", "sarif")
    s = json.loads(r.output)
    assert s["version"] == "2.1.0" and s["runs"][0]["results"]


def test_check_fail_on_never_and_module_scope(proj):
    assert odx("check", "--path", str(proj), "--fail-on", "never").exit_code == 0
    r = odx("check", "--path", str(proj), "--module", "lib_core")
    assert r.exit_code == 0 and "no findings" in r.output


def test_why_and_impact(proj):
    r = odx("why", "library.book.state", "--path", str(proj))
    assert r.exit_code == 0 and "view-modifier" in r.output and "odx impact" in r.output
    r = odx("impact", "library.book.state", "--path", str(proj))
    assert r.exit_code == 0 and "library_views.xml" in r.output
    r = odx("why", "library.book.nope", "--path", str(proj))
    assert r.exit_code == 1


def test_report(proj):
    r = odx("report", "--path", str(proj))
    assert r.exit_code == 0 and "recommendations" in r.output
    data = json.loads(odx("report", "--path", str(proj), "-f", "json").output)
    assert data["grade"] in "ABCDF" and data["stats"]["models"] >= 5


def test_fix_dry_run_then_apply(proj):
    r = odx("fix", "access", "--path", str(proj), "--dry-run")
    assert r.exit_code == 0 and "ir.model.access.csv" in r.output and "library.loan" in r.output
    csv = proj / "addons" / "lib_ext" / "security" / "ir.model.access.csv"
    assert not csv.exists()
    r = odx("fix", "access", "--path", str(proj))
    assert r.exit_code == 0 and csv.exists()
    after = odx("check", "--path", str(proj), "--only", "access", "-f", "json")
    assert not [d for d in json.loads(after.output) if d["code"] == "access.missing-acl"]


def test_doctor_from_log(proj, tmp_path):
    log = tmp_path / "install.log"
    log.write_text(
        "2024-01-01 10:00:00,000 1 INFO db odoo.modules.loading: loading lib_ext\n"
        "2024-01-01 10:00:01,000 1 ERROR db odoo.modules.registry: Failed to load registry\n"
        "Traceback (most recent call last):\n"
        '  File "lib_ext/views/ext_views.xml", line 9\n'
        "odoo.tools.convert.ParseError: Field `pages_count` does not exist\n")
    r = odx("doctor", "--path", str(proj), "--log", str(log), "--module", "lib_ext")
    assert r.exit_code == 1 and "doctor.parse-error" in r.output


def test_diff_db_from_snapshot(proj, tmp_path):
    from odx.runtime.rpc import snapshot_to_json
    snap = DbSnapshot(database="dev",
                      models={"library.book": DbModel("library.book", fields={
                          "title": DbField("title", "char"), "isbn": DbField("isbn", "integer"),
                          "legacy": DbField("legacy", "char", modules=("lib_core",))})},
                      installed_modules={"base": "installed", "lib_core": "installed"})
    path = snapshot_to_json(snap, tmp_path / "snap.json")
    r = odx("diff", "db", "--snapshot", str(path), "--path", str(proj), "-f", "json")
    codes = {d["code"] for d in json.loads(r.output)}
    assert {"drift.field-type-changed", "drift.field-not-in-db",
            "drift.field-not-in-code", "drift.model-not-in-db"} <= codes


def test_upgrade_check_and_apply(proj):
    view = proj / "addons" / "lib_core" / "views" / "library_views.xml"
    r = odx("upgrade", "check", "--to", "18.0", "--path", str(proj), "-f", "json")
    codes = {d["code"] for d in json.loads(r.output)}
    assert {"upgrade.tree-to-list", "upgrade.manifest-version"} <= codes
    r = odx("upgrade", "apply", "--to", "18.0", "--path", str(proj))
    assert r.exit_code == 0
    text = view.read_text()
    assert "<list" in text and "<tree" not in text and "list,form" in text
    assert "'18.0.1.0.0'" in (proj / "addons" / "lib_core" / "__manifest__.py").read_text()


def test_report_with_runtime_folds_in_the_probe(proj, monkeypatch):
    from odx.engine.contracts import InstallProbe
    import odx.runtime.probe as probe_mod

    def fake_probe(settings, modules, **kw):
        return InstallProbe(database="odx_doctor_x", modules=tuple(modules), returncode=1,
                            log="2024-01-01 10:00:00,000 1 ERROR db odoo.modules.loading: "
                                "Module lib_ext: External ID not found in the system: x.y\n")
    monkeypatch.setattr(probe_mod, "install_probe", fake_probe)
    (proj / ".odx.toml").write_text('project = "p"\n[odoo]\nruntime = "source"\n'
                                    'addons_paths = ["./addons"]\nsource_path = "./odoo"\n'
                                    '[database]\ndefault = "dev"\n')
    import os
    old = os.getcwd()
    os.chdir(proj)
    try:
        r = odx("report", "--with-runtime", "-f", "json")
    finally:
        os.chdir(old)
    data = json.loads(r.output)
    assert data["runtime"]["ok"] is False
    assert data["score"] <= 59
    assert any(d["code"] == "doctor.xmlid-not-found" for d in data["runtime"]["diagnostics"])
