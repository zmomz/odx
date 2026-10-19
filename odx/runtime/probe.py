"""Fresh-install probe (RFC §15 `doctor`, runtime half).

Spin a throwaway database, install the modules clean with --stop-after-init,
capture the log, drop the database. Interpretation of the log (what broke) is
the engine/API's job; this only produces the raw `InstallProbe`.
"""

from __future__ import annotations

import secrets
from typing import Callable, Iterable

from odx.engine.contracts import InstallProbe
from odx.runtime.commands import Command, db_drop_command, install_command
from odx.runtime.executor import ExecResult, execute
from odx.runtime.settings import RuntimeSettings

Runner = Callable[..., ExecResult]

PROBE_DB_PREFIX = "odx_doctor_"


def probe_db_name() -> str:
    return PROBE_DB_PREFIX + secrets.token_hex(4)


def install_probe(
    settings: RuntimeSettings,
    modules: Iterable[str],
    *,
    db_name: str | None = None,
    keep_db: bool = False,
    demo: bool = False,
    runner: Runner = execute,
) -> InstallProbe:
    """Install `modules` into a fresh database and report the raw outcome.

    `runner(cmd, capture=True) -> ExecResult` is injectable for tests. The
    throwaway db is dropped afterwards (even when the install fails) unless
    `keep_db`. Odoo creates the database itself on `-d <new> -i ...`.
    """
    mods = tuple(dict.fromkeys(m.strip() for m in modules if m and m.strip()))
    if not mods:
        raise ValueError("install_probe needs at least one module")
    db = db_name or probe_db_name()

    cmd: Command = install_command(settings, db, mods, demo=demo)
    try:
        result = runner(cmd, capture=True)
    finally:
        if not keep_db:
            drop = runner(db_drop_command(settings, db), capture=True)
    log = result.stdout + result.stderr
    if not keep_db and not drop.ok:
        log += f"\n[odx] warning: could not drop probe database {db}: {drop.output.strip()}\n"
    return InstallProbe(
        database=db,
        modules=mods,
        returncode=result.returncode,
        log=log,
        command=cmd.argv,
    )
