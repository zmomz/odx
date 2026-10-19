"""Phase 0 smoke tests: the CLI loads, --help works, init writes valid TOML."""

from __future__ import annotations

import tomllib

from typer.testing import CliRunner

from odx.clients.cli.main import app

runner = CliRunner()


def test_help_runs():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "odx" in result.output


def test_bare_command_shows_cheatsheet():
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "odx init" in result.output


def test_check_on_an_empty_project_is_clean(tmp_path):
    result = runner.invoke(app, ["check", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "no findings" in result.output


def test_all_verbs_have_help():
    for verb in ["init", "run", "logs", "shell", "test", "check", "report",
                 "fix", "why", "impact", "doctor", "new", "add", "inherit",
                 "db", "diff", "upgrade"]:
        result = runner.invoke(app, [verb, "--help"])
        assert result.exit_code == 0, f"`odx {verb} --help` failed"


def test_init_writes_valid_toml(tmp_path):
    result = runner.invoke(
        app,
        ["init", str(tmp_path), "--yes", "--runtime", "docker",
         "--name", "acme", "--odoo-version", "17.0"],
    )
    assert result.exit_code == 0, result.output
    cfg_path = tmp_path / ".odx.toml"
    assert cfg_path.is_file()
    data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    assert data["project"] == "acme"
    assert data["odoo"]["runtime"] == "docker"
    assert data["odoo"]["version"] == "17.0"


def test_init_refuses_overwrite_without_force(tmp_path):
    (tmp_path / ".odx.toml").write_text("project='x'\n", encoding="utf-8")
    result = runner.invoke(app, ["init", str(tmp_path), "--yes"])
    assert result.exit_code == 1


def test_init_detects_docker_from_compose(tmp_path):
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  odoo:\n    image: odoo:18\n", encoding="utf-8"
    )
    result = runner.invoke(app, ["init", str(tmp_path), "--yes"])
    assert result.exit_code == 0
    data = tomllib.loads((tmp_path / ".odx.toml").read_text(encoding="utf-8"))
    assert data["odoo"]["runtime"] == "docker"
