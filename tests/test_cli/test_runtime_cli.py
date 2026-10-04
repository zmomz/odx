"""CLI wiring for the runtime verbs, exercised with --dry-run (nothing executes)."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from odx.clients.cli.main import app

runner = CliRunner()


@pytest.fixture
def project(tmp_path, monkeypatch):
    res = runner.invoke(app, ["init", str(tmp_path), "--yes", "--runtime", "docker"])
    assert res.exit_code == 0, res.output
    monkeypatch.chdir(tmp_path)
    return tmp_path


def dry(*args):
    res = runner.invoke(app, [*args, "--dry-run"])
    assert res.exit_code == 0, res.output
    return res.output


def test_missing_config_exits_2(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["run", "--dry-run"])
    assert res.exit_code == 2
    assert "odx init" in res.output


def test_config_found_from_subdirectory(project, monkeypatch):
    sub = project / "addons" / "m"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    assert "docker compose" in dry("shell")


def test_run(project):
    out = dry("run", "--watch")
    assert "would run:" in out
    assert "run --rm --service-ports odoo odoo -d my_dev_db --dev=all" in out


def test_shell_with_db(project):
    assert "odoo shell -d other" in dry("shell", "--db", "other")


def test_test(project):
    out = dry("test", "sale_ext")
    assert "-u sale_ext --test-enable --stop-after-init --test-tags=/sale_ext" in out
    assert "-i sale_ext" in dry("test", "sale_ext", "--install")


def test_test_without_module_errors(project):
    res = runner.invoke(app, ["test", "--dry-run"])
    assert res.exit_code == 2 and "which module" in res.output


def test_logs(project):
    out = dry("logs", "sale", "--level", "warning", "--tail", "10")
    assert "logs --no-log-prefix --tail=10 -f odoo" in out
    assert "scope=sale" in out and "WARNING" in out


def test_logs_without_logfile_on_venv(project):
    cfg = project / ".odx.toml"
    cfg.write_text(cfg.read_text().replace('runtime = "docker"', 'runtime = "venv"'))
    res = runner.invoke(app, ["logs", "--dry-run"])
    assert res.exit_code == 2 and "[logs] file" in res.output


def test_db_commands(project):
    assert "exec -T db psql" in dry("db", "list")
    assert "-i base" in dry("db", "new", "x")
    assert "createdb -U odoo x" in dry("db", "new", "x", "--bare")
    assert "dropdb -U odoo --if-exists x" in dry("db", "drop", "x")
    assert "createdb -U odoo -T a b" in dry("db", "copy", "a", "b")
    assert "pg_dump" in dry("db", "dump", "a") and "> a.dump" in dry("db", "dump", "a")
    out = dry("db", "restore", "b", "in.dump")
    assert "createdb -U odoo b" in out and "pg_restore" in out and "< in.dump" in out
    assert "psql -U odoo -v ON_ERROR_STOP=1 -q -d a" in dry("db", "anonymize", "a")


def test_source_runtime_db_list(project):
    cfg = project / ".odx.toml"
    cfg.write_text(cfg.read_text().replace('runtime = "docker"', 'runtime = "source"'))
    out = dry("db", "list")
    assert "psql -h localhost -p 5432 -U odoo" in out
    assert "odoo" in out and "password" not in out


@pytest.mark.parametrize("args", [["db", "drop", "x"], ["db", "anonymize", "x"]])
def test_mutating_db_commands_confirm(project, args):
    res = runner.invoke(app, args, input="n\n")
    assert res.exit_code == 1
    assert "aborted" in res.output


def test_restore_missing_file_errors(project):
    res = runner.invoke(app, ["db", "restore", "b", "nope.dump", "--yes"])
    assert res.exit_code == 2 and "does not exist" in res.output


def test_missing_binary_is_reported_not_raised(project, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    res = runner.invoke(app, ["db", "drop", "x", "--yes"])
    assert res.exit_code == 127
    assert res.exception is None or isinstance(res.exception, SystemExit)
    assert "not found" in res.output
