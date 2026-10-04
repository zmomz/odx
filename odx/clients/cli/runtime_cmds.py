"""CLI glue for the runtime verbs (run/logs/shell/test/db) — RFC §14.

Kept out of main.py so the command bodies there stay one-liners. Everything
domain-ish lives in odx.runtime; this file only loads settings, echoes the
command (rule 5: show what it does), confirms mutations, and maps exit codes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import typer
from rich.console import Console

from odx.clients.cli import config as cfgmod
from odx.runtime.commands import Command
from odx.runtime.executor import ExecResult, LogFilter, execute
from odx.runtime.settings import RuntimeSettings, SettingsError

console = Console()
err_console = Console(stderr=True)


def load_settings(start: Path | None = None) -> RuntimeSettings:
    """Find .odx.toml from cwd upward; exit 2 with a next step if missing/broken."""
    path = cfgmod.find_config(start or Path.cwd())
    if path is None:
        err_console.print(
            f"[red]error:[/red] no {cfgmod.CONFIG_NAME} found here or in any parent "
            f"directory — run [bold]`odx init`[/bold] first."
        )
        raise typer.Exit(2)
    try:
        return RuntimeSettings.from_mapping(path.parent, cfgmod.load_config(path))
    except (SettingsError, ValueError) as exc:
        err_console.print(f"[red]error:[/red] {path}: {exc}")
        raise typer.Exit(2) from None


def echo_command(cmd: Command, *, dry_run: bool) -> None:
    prefix = "would run" if dry_run else "$"
    # plain echo: no rich markup/wrapping, so the argv is copy-pasteable.
    typer.echo(f"{prefix}: {cmd.display()}" if dry_run else f"{prefix} {cmd.display()}",
               err=not dry_run)


def confirm_mutation(what: str, *, yes: bool, dry_run: bool) -> None:
    if yes or dry_run:
        return
    if not typer.confirm(f"{what} — this cannot be undone. Continue?", default=False):
        err_console.print("[yellow]aborted[/yellow]")
        raise typer.Exit(1)


def _fail(res: ExecResult) -> None:
    if res.message:
        err_console.print(f"[red]error:[/red] {res.message}", soft_wrap=True)
    if res.returncode != 0:
        raise typer.Exit(res.returncode)


def run_commands(
    cmds: Sequence[Command], *, dry_run: bool, capture: bool = False,
) -> list[ExecResult]:
    """Echo then execute each command; stop (and exit) at the first failure."""
    results: list[ExecResult] = []
    for cmd in cmds:
        echo_command(cmd, dry_run=dry_run)
        res = execute(cmd, dry_run=dry_run, capture=capture)
        results.append(res)
        if capture and res.stderr and not res.ok and not res.message:
            typer.echo(res.stderr.rstrip(), err=True)
        _fail(res)
    return results


def run_one(cmd: Command, *, dry_run: bool, capture: bool = False) -> ExecResult:
    return run_commands([cmd], dry_run=dry_run, capture=capture)[0]


def stream_logs(
    cmd: Command, *, scope: str | None, level: str | None, dry_run: bool,
) -> None:
    echo_command(cmd, dry_run=dry_run)
    if dry_run:
        if scope or level:
            typer.echo(f"filter: scope={scope or '*'} level>={(level or 'any').upper()}")
        return
    res = execute(cmd, on_line=typer.echo, line_filter=LogFilter(scope, level))
    _fail(res)
