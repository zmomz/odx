from __future__ import annotations

from pathlib import Path

import pytest

from odx.runtime import commands as c
from odx.runtime.settings import RuntimeSettings

ROOT = Path("/proj")
COMPOSE = ["docker", "compose", "-f", "/proj/docker-compose.yml"]


def mk(runtime: str, **kw) -> RuntimeSettings:
    return RuntimeSettings(runtime=runtime, project_root=ROOT, **kw)


def after(argv, flag):
    return argv[list(argv).index(flag) + 1]


# --------------------------------------------------------------------------- #
# odoo-bin per runtime
# --------------------------------------------------------------------------- #
def test_odoo_bin_docker_run_and_exec():
    s = mk("docker")
    assert c.odoo_bin_argv(s, ["--version"]) == tuple(COMPOSE + ["run", "--rm", "odoo", "odoo", "--version"])
    assert c.odoo_bin_argv(s, ["-x"], docker_mode="exec", tty=False) == tuple(
        COMPOSE + ["exec", "-T", "odoo", "odoo", "-x"])
    assert c.odoo_bin_argv(s, [], service_ports=True, tty=False)[4:7] == ("run", "--rm", "--service-ports")
    with pytest.raises(ValueError):
        c.odoo_bin_argv(s, [], docker_mode="up")


def test_odoo_bin_docker_custom_service():
    s = mk("docker", docker_service="web", compose_file="ops/compose.yml")
    argv = c.odoo_bin_argv(s, [])
    assert argv[:4] == ("docker", "compose", "-f", "/proj/ops/compose.yml")
    assert argv[-2:] == ("web", "odoo")


def test_odoo_bin_venv_default_uses_console_script():
    argv = c.odoo_bin_argv(mk("venv"), ["-d", "x"])
    assert argv[0] == "/proj/.venv/bin/odoo"
    assert "--addons-path=/proj/addons" in argv
    assert argv[-2:] == ("-d", "x")


def test_odoo_bin_venv_with_python_and_bin():
    s = mk("venv", python="/v/bin/python", odoo_bin="src/odoo-bin")
    argv = c.odoo_bin_argv(s, [])
    assert argv[:2] == ("/v/bin/python", "/proj/src/odoo-bin")


def test_odoo_bin_source():
    s = mk("source", source_path="../odoo", addons_paths=("addons", "../enterprise"))
    argv = c.odoo_bin_argv(s, ["shell", "-d", "db"])
    assert argv[:3] == ("python3", "/odoo/odoo-bin", "shell")   # subcommand stays first
    assert "--addons-path=/odoo/addons,/proj/addons,/enterprise" in argv
    assert "--db_host=localhost" in argv and "--db_port=5432" in argv and "--db_user=odoo" in argv


def test_odoo_bin_source_config_file():
    s = mk("source", source_path="/odoo", config_file="odoo.conf")
    assert after(c.odoo_bin_argv(s, []), "-c") == "/proj/odoo.conf"


def test_odoo_bin_odoo_sh():
    argv = c.odoo_bin_argv(mk("odoo.sh"), ["-d", "x"])
    assert argv[0] == "odoo-bin"
    assert not any(a.startswith("--db_host") for a in argv)


def test_password_never_on_argv_but_in_env():
    s = mk("source", source_path="/odoo", db_password="s3cret")
    for cmd in (c.run_command(s), c.db_list_command(s), c.db_drop_command(s, "x")):
        assert "s3cret" not in " ".join(cmd.argv)
        assert cmd.env == {"PGPASSWORD": "s3cret"}
    assert c.run_command(mk("docker", db_password="s3cret")).env == {}


# --------------------------------------------------------------------------- #
# daily loop
# --------------------------------------------------------------------------- #
def test_run_docker_watch():
    cmd = c.run_command(mk("docker"), watch=True)
    assert "--service-ports" in cmd.argv
    assert after(cmd.argv, "-d") == "my_dev_db"
    assert "--dev=all" in cmd.argv
    assert not any(a.startswith("--http-port") for a in cmd.argv)   # compose owns ports
    assert cmd.interactive and not cmd.mutating


def test_run_local_http_port_and_no_dev_without_watch():
    cmd = c.run_command(mk("venv", http_port=9069), db="other")
    assert "--http-port=9069" in cmd.argv
    assert after(cmd.argv, "-d") == "other"
    assert not any(a.startswith("--dev") for a in cmd.argv)


def test_run_watch_adds_reload_to_custom_dev():
    cmd = c.run_command(mk("venv", dev="qweb,xml"), watch=True)
    assert "--dev=qweb,xml,reload" in cmd.argv
    cmd = c.run_command(mk("venv", dev="reload"), watch=True)
    assert "--dev=reload" in cmd.argv


def test_shell():
    cmd = c.shell_command(mk("docker"), db="x")
    i = cmd.argv.index("shell")
    assert cmd.argv[i - 1] == "odoo" and cmd.argv[i + 1:i + 3] == ("-d", "x")
    assert cmd.interactive


def test_odoo_test_command():
    cmd = c.odoo_test_command(mk("docker"), "sale_ext")
    a = cmd.argv
    assert after(a, "-u") == "sale_ext"
    assert "--test-enable" in a and "--stop-after-init" in a
    assert "--test-tags=/sale_ext" in a
    assert "-T" in a                              # non-interactive container
    cmd = c.odoo_test_command(mk("venv"), "m", install=True, tags="/m:TestX", db="t")
    assert after(cmd.argv, "-i") == "m" and "--test-tags=/m:TestX" in cmd.argv
    assert after(cmd.argv, "-d") == "t"


def test_install_command_demo_flags_by_version():
    a = c.install_command(mk("docker"), "tmp", ["a", "b"]).argv
    assert after(a, "-i") == "a,b" and after(a, "-d") == "tmp"
    assert "--stop-after-init" in a and "--log-level=info" in a
    assert "--without-demo=all" in a
    assert "--without-demo=all" not in c.install_command(mk("docker"), "t", ["a"], demo=True).argv
    a19 = c.install_command(mk("docker", odoo_version="19.0"), "t", ["a"]).argv
    assert not any("demo" in x for x in a19)
    assert "--with-demo" in c.install_command(mk("docker", odoo_version="19.0"), "t", ["a"], demo=True).argv


# --------------------------------------------------------------------------- #
# postgres
# --------------------------------------------------------------------------- #
def test_db_list_docker_and_local():
    d = c.db_list_command(mk("docker"))
    assert d.argv[:8] == tuple(COMPOSE + ["exec", "-T", "db", "psql"])
    assert after(d.argv, "-U") == "odoo" and after(d.argv, "-d") == "postgres"
    assert "pg_database" in after(d.argv, "-c")
    assert not d.mutating
    l = c.db_list_command(mk("venv", db_host="pg", db_port=6543, db_user="me"))
    assert l.argv[:7] == ("psql", "-h", "pg", "-p", "6543", "-U", "me")


def test_db_list_odoo_sh_relies_on_env():
    assert c.db_list_command(mk("odoo.sh")).argv[0] == "psql"
    assert "-h" not in c.db_list_command(mk("odoo.sh")).argv


def test_db_new_bare_and_odoo():
    bare = c.db_new_command(mk("venv"), "x", bare=True)
    assert bare.argv[0] == "createdb" and bare.argv[-1] == "x" and bare.mutating
    init = c.db_new_command(mk("docker"), "x")
    assert after(init.argv, "-i") == "base" and after(init.argv, "-d") == "x"
    assert "create Odoo database x" in init.description


def test_db_drop_copy():
    drop = c.db_drop_command(mk("docker", docker_db_service="pg"), "x")
    assert drop.argv[4:8] == ("exec", "-T", "pg", "dropdb")
    assert drop.argv[-2:] == ("--if-exists", "x") and drop.mutating
    copy = c.db_copy_command(mk("venv"), "a", "b")
    assert copy.argv[0] == "createdb" and copy.argv[-3:] == ("-T", "a", "b")
    assert "filestore" in copy.description


def test_db_dump_and_restore():
    dump = c.db_dump_command(mk("docker"), "a", Path("out.dump"))
    assert "pg_dump" in dump.argv and "-Fc" in dump.argv and dump.argv[-1] == "a"
    assert dump.stdout_file == Path("out.dump")
    assert dump.display().endswith("> out.dump")
    create, restore = c.db_restore_commands(mk("docker"), "b", Path("in.dump"))
    assert "createdb" in create.argv and create.argv[-1] == "b"
    assert "pg_restore" in restore.argv and after(restore.argv, "-d") == "b"
    assert restore.stdin_file == Path("in.dump") and restore.mutating
    assert restore.display().endswith("< in.dump")


def test_anonymize():
    cmd = c.db_anonymize_command(mk("docker"), "a")
    assert "psql" in cmd.argv and after(cmd.argv, "-d") == "a"
    assert cmd.mutating and cmd.stdin == c.anonymize_sql()
    sql = c.anonymize_sql()
    for needle in ("UPDATE res_partner", "UPDATE res_users", "login <> 'admin'",
                   "UPDATE mail_message", "body", "email", "phone", "BEGIN;", "COMMIT;"):
        assert needle in sql
    assert "<stdin>" in cmd.display()


# --------------------------------------------------------------------------- #
# logs
# --------------------------------------------------------------------------- #
def test_logs_docker():
    cmd = c.logs_command(mk("docker"), tail=50)
    assert cmd.argv[4] == "logs" and "-f" in cmd.argv and "--tail=50" in cmd.argv
    assert cmd.argv[-1] == "odoo"
    assert "-f" not in c.logs_command(mk("docker"), follow=False).argv[4:]


def test_logs_logfile_and_none():
    cmd = c.logs_command(mk("venv", logfile="var/odoo.log"))
    assert cmd.argv[0] == "tail" and cmd.argv[-1] == "/proj/var/odoo.log" and "-F" in cmd.argv
    assert c.logs_command(mk("venv")) is None
