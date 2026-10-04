"""Pure argv builders for the daily loop and DB ops (RFC §12, §14).

Nothing here executes anything: every builder returns a `Command` describing
what *would* run (argv, env, cwd, stdin). `executor.execute` runs them. That
split keeps every runtime kind testable without docker/postgres/odoo, and lets
clients show the exact command (`--dry-run`) before running it.

Runtime kinds:
  docker   odoo runs via `docker compose run --rm <service> odoo ...` (goes
           through the image entrypoint, which injects the db connection);
           postgres tools run inside the db service via `docker compose exec`.
  venv     `<venv python> <odoo-bin> ...`; postgres tools run locally.
  source   `python <source_path>/odoo-bin ...`; postgres tools run locally.
  odoo.sh  `odoo-bin ...` on PATH; postgres tools run locally (libpq env).
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable, Sequence

from odx.runtime.settings import RuntimeSettings


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)   # merged over os.environ
    cwd: Path | None = None
    stdin: str | None = None                  # text fed to stdin
    stdin_file: Path | None = None            # file fed to stdin (restore)
    stdout_file: Path | None = None           # stdout redirected to file (dump)
    description: str = ""
    mutating: bool = False
    interactive: bool = False                 # needs the user's terminal (shell, run)

    def display(self) -> str:
        """Shell-ish rendering for `--dry-run` / echo. Never includes env secrets."""
        text = shlex.join(self.argv)
        if self.stdin_file is not None:
            text += f" < {shlex.quote(str(self.stdin_file))}"
        elif self.stdin is not None:
            text += " < <stdin>"
        if self.stdout_file is not None:
            text += f" > {shlex.quote(str(self.stdout_file))}"
        return text


# =========================================================================== #
# odoo-bin
# =========================================================================== #
def _compose_base(s: RuntimeSettings) -> list[str]:
    return ["docker", "compose", "-f", str(s.resolve(s.compose_file))]


def _default_python(s: RuntimeSettings) -> str:
    if s.python:
        return s.python
    if s.runtime == "venv":
        return str(s.project_root / ".venv" / "bin" / "python")
    return "python3"


def _odoo_bin_path(s: RuntimeSettings) -> str:
    if s.odoo_bin:
        return str(s.resolve(s.odoo_bin))
    if s.source_path:
        return str(s.resolve(s.source_path) / "odoo-bin")
    if s.runtime == "venv":
        # pip-installed odoo ships an `odoo` console script in the venv.
        return str(Path(_default_python(s)).parent / "odoo")
    return "odoo-bin"


def _local_odoo_args(s: RuntimeSettings) -> list[str]:
    """Connection/addons flags for non-docker runtimes."""
    args: list[str] = []
    if s.config_file:
        args += ["-c", str(s.resolve(s.config_file))]
    if s.runtime != "odoo.sh":             # odoo.sh injects its own connection
        args += [f"--db_host={s.db_host}", f"--db_port={s.db_port}", f"--db_user={s.db_user}"]
    paths = _addons_path(s)
    if paths:
        args.append(f"--addons-path={','.join(paths)}")
    return args


def _addons_path(s: RuntimeSettings) -> list[str]:
    paths = [str(s.resolve(p)) for p in s.addons_paths]
    if s.source_path:
        core = str(s.resolve(s.source_path) / "addons")
        if core not in paths:
            paths.insert(0, core)
    return paths


def _local_env(s: RuntimeSettings) -> dict[str, str]:
    # libpq (and so Odoo's psycopg2) reads PGPASSWORD: keeps the secret off argv.
    return {"PGPASSWORD": s.db_password} if s.db_password else {}


def odoo_bin_argv(
    settings: RuntimeSettings,
    extra: Sequence[str] = (),
    *,
    docker_mode: str = "run",
    service_ports: bool = False,
    tty: bool = True,
) -> tuple[str, ...]:
    """argv invoking odoo-bin for the configured runtime, followed by `extra`.

    docker_mode: "run" (`docker compose run --rm`, a fresh container through
    the image entrypoint) or "exec" (into the running service container).
    """
    s = settings
    extra = list(extra)
    if s.runtime == "docker":
        base = _compose_base(s)
        if docker_mode == "exec":
            base += ["exec"] + ([] if tty else ["-T"]) + [s.docker_service, "odoo"]
        elif docker_mode == "run":
            base += ["run", "--rm"]
            if service_ports:
                base.append("--service-ports")
            if not tty:
                base.append("-T")
            base += [s.docker_service, "odoo"]
        else:
            raise ValueError(f"docker_mode must be 'run' or 'exec', got {docker_mode!r}")
        return tuple(base + extra)

    if s.runtime == "odoo.sh":
        head = [_odoo_bin_path(s)]
    elif s.runtime == "venv" and not s.odoo_bin and not s.source_path:
        head = [_odoo_bin_path(s)]
    else:
        head = [_default_python(s), _odoo_bin_path(s)]
    # Subcommands (e.g. `shell`) must come right after odoo-bin.
    sub: list[str] = []
    if extra and not extra[0].startswith("-"):
        sub, extra = [extra[0]], extra[1:]
    return tuple(head + sub + _local_odoo_args(s) + extra)


def _odoo_command(
    s: RuntimeSettings, extra: Sequence[str], description: str, *,
    mutating: bool = False, interactive: bool = False, service_ports: bool = False,
) -> Command:
    argv = odoo_bin_argv(s, extra, service_ports=service_ports, tty=interactive)
    env = {} if s.runtime == "docker" else _local_env(s)
    return Command(argv=argv, env=env, cwd=s.project_root, description=description,
                   mutating=mutating, interactive=interactive)


def _demo_flags(s: RuntimeSettings, demo: bool) -> list[str]:
    if s.odoo_major >= 19:                 # 19 flipped the default: demo is opt-in
        return ["--with-demo"] if demo else []
    return [] if demo else ["--without-demo=all"]


def run_command(s: RuntimeSettings, *, watch: bool = False, db: str | None = None) -> Command:
    extra = ["-d", db or s.db_name]
    if s.runtime != "docker":              # in docker the compose port mapping owns this
        extra.append(f"--http-port={s.http_port}")
    if watch:
        dev = s.dev or "all"
        if dev != "all" and "reload" not in dev.split(","):
            dev += ",reload"
        extra.append(f"--dev={dev}")
    return _odoo_command(s, extra, f"run Odoo on {db or s.db_name}" + (" (watch)" if watch else ""),
                         interactive=True, service_ports=True)


def shell_command(s: RuntimeSettings, *, db: str | None = None) -> Command:
    extra = ["shell", "-d", db or s.db_name, "--no-http"]
    return _odoo_command(s, extra, f"odoo shell on {db or s.db_name}", interactive=True)


def odoo_test_command(
    s: RuntimeSettings, module: str, *, db: str | None = None,
    install: bool = False, tags: str | None = None,
) -> Command:
    extra = [
        "-d", db or s.db_name,
        "-i" if install else "-u", module,
        "--test-enable", "--stop-after-init",
        f"--test-tags={tags or '/' + module}",
        "--no-http" if s.odoo_major >= 13 else "--no-xmlrpc",
    ]
    return _odoo_command(s, extra, f"test {module} on {db or s.db_name}",
                         mutating=True)


def install_command(
    s: RuntimeSettings, db: str, modules: Iterable[str], *, demo: bool = False,
    log_level: str = "info",
) -> Command:
    mods = ",".join(modules)
    extra = ["-d", db, "-i", mods, "--stop-after-init", f"--log-level={log_level}",
             "--no-http", *_demo_flags(s, demo)]
    return _odoo_command(s, extra, f"install {mods} into {db}", mutating=True)


# =========================================================================== #
# postgres
# =========================================================================== #
def _pg(
    s: RuntimeSettings, tool: str, args: Sequence[str], description: str, *,
    mutating: bool, stdin: str | None = None, stdin_file: Path | None = None,
    stdout_file: Path | None = None, maintenance_db: bool = False,
) -> Command:
    if s.runtime == "docker":
        # Inside the db container: local socket, the image's trust auth.
        argv = _compose_base(s) + ["exec", "-T", s.docker_db_service, tool, "-U", s.db_user]
        env: dict[str, str] = {}
    elif s.runtime == "odoo.sh":
        argv = [tool]                       # odoo.sh exports PG* env vars
        env = {}
    else:
        argv = [tool, "-h", s.db_host, "-p", str(s.db_port), "-U", s.db_user]
        env = _local_env(s)
    if maintenance_db:
        argv += ["-d", "postgres"]
    return Command(argv=tuple(argv) + tuple(args), env=env, cwd=s.project_root,
                   stdin=stdin, stdin_file=stdin_file, stdout_file=stdout_file,
                   description=description, mutating=mutating)


def db_list_command(s: RuntimeSettings) -> Command:
    sql = ("SELECT datname FROM pg_database WHERE NOT datistemplate "
           "AND datname <> 'postgres' ORDER BY datname")
    return _pg(s, "psql", ["-At", "-c", sql], "list databases",
               mutating=False, maintenance_db=True)


def db_create_command(s: RuntimeSettings, name: str, *, template: str | None = None) -> Command:
    args = (["-T", template] if template else []) + [name]
    return _pg(s, "createdb", args, f"create empty database {name}", mutating=True)


def db_new_command(
    s: RuntimeSettings, name: str, *, bare: bool = False, demo: bool = False,
) -> Command:
    """New database: an initialised Odoo db (base installed) unless `bare`."""
    if bare:
        return db_create_command(s, name)
    cmd = install_command(s, name, ["base"], demo=demo)
    return replace(cmd, description=f"create Odoo database {name} (base)")


def db_drop_command(s: RuntimeSettings, name: str) -> Command:
    return _pg(s, "dropdb", ["--if-exists", name], f"drop database {name}", mutating=True)


def db_copy_command(s: RuntimeSettings, src: str, dst: str) -> Command:
    cmd = db_create_command(s, dst, template=src)
    return replace(cmd, description=f"copy database {src} -> {dst} (filestore not copied)")


def db_dump_command(s: RuntimeSettings, name: str, output: Path) -> Command:
    return _pg(s, "pg_dump", ["-Fc", "--no-owner", name], f"dump {name} to {output}",
               mutating=False, stdout_file=Path(output))


def db_restore_commands(s: RuntimeSettings, name: str, dump_file: Path) -> list[Command]:
    """Restore a custom-format dump into a NEW database `name` (create, then restore)."""
    create = db_create_command(s, name)
    restore = _pg(s, "pg_restore", ["--no-owner", "--exit-on-error", "-d", name],
                  f"restore {dump_file} into {name}", mutating=True,
                  stdin_file=Path(dump_file))
    return [create, restore]


# --------------------------------------------------------------------------- #
# anonymize
# --------------------------------------------------------------------------- #
# Kept: __system__ (id 1), admin (id 2, login 'admin'), their partners and all
# company partners. Columns that differ across Odoo versions are guarded.
ANONYMIZE_SQL = """\
BEGIN;

CREATE TEMP TABLE _odx_keep_partner AS
    SELECT partner_id AS id FROM res_users WHERE id <= 2 OR login = 'admin'
    UNION SELECT partner_id FROM res_company WHERE partner_id IS NOT NULL;

UPDATE res_partner p
   SET name  = CASE WHEN p.is_company THEN 'Company ' ELSE 'Partner ' END || p.id,
       email = CASE WHEN p.email IS NULL THEN NULL ELSE 'partner' || p.id || '@example.invalid' END,
       phone = NULL
 WHERE p.id NOT IN (SELECT id FROM _odx_keep_partner WHERE id IS NOT NULL);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'res_partner' AND column_name = 'mobile') THEN
        EXECUTE 'UPDATE res_partner SET mobile = NULL
                  WHERE id NOT IN (SELECT id FROM _odx_keep_partner WHERE id IS NOT NULL)';
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'res_partner' AND column_name = 'complete_name') THEN
        EXECUTE 'UPDATE res_partner SET complete_name = name
                  WHERE id NOT IN (SELECT id FROM _odx_keep_partner WHERE id IS NOT NULL)';
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'res_partner' AND column_name = 'display_name') THEN
        EXECUTE 'UPDATE res_partner SET display_name = name
                  WHERE id NOT IN (SELECT id FROM _odx_keep_partner WHERE id IS NOT NULL)';
    END IF;
END $$;

UPDATE res_users
   SET login = 'user' || id || '@example.invalid'
 WHERE id > 2 AND login <> 'admin';

UPDATE mail_message
   SET body = '<p>[redacted]</p>',
       email_from = NULL
 WHERE body IS NOT NULL OR email_from IS NOT NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'mail_mail') THEN
        EXECUTE 'UPDATE mail_mail SET body_html = NULL, email_to = NULL, email_cc = NULL';
    END IF;
END $$;

COMMIT;
"""


def anonymize_sql() -> str:
    return ANONYMIZE_SQL


def db_anonymize_command(s: RuntimeSettings, name: str) -> Command:
    return _pg(s, "psql", ["-v", "ON_ERROR_STOP=1", "-q", "-d", name],
               f"anonymize {name} (partners, user logins, messages)", mutating=True,
               stdin=anonymize_sql())


# =========================================================================== #
# logs
# =========================================================================== #
def logs_command(s: RuntimeSettings, *, follow: bool = True, tail: int = 200) -> Command | None:
    """How to read Odoo's log; None when it only goes to `odx run`'s stdout."""
    if s.runtime == "docker":
        argv = _compose_base(s) + ["logs", "--no-log-prefix", f"--tail={tail}"]
        if follow:
            argv.append("-f")
        argv.append(s.docker_service)
        return Command(argv=tuple(argv), cwd=s.project_root,
                       description=f"logs of service {s.docker_service}")
    if s.logfile:
        argv = ["tail", "-n", str(tail)] + (["-F"] if follow else []) + [str(s.resolve(s.logfile))]
        return Command(argv=tuple(argv), cwd=s.project_root, description=f"tail {s.logfile}")
    return None
