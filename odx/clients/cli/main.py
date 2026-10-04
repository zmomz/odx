"""The `odx` CLI — Typer app wiring the fixed verb set (RFC §14).

Grammar (a contract):
    odx <verb> <noun> [target] [--flags]        (rule 1, no exceptions)
    read-only vs mutating is a hard law         (rule 2)
    nouns are Odoo's vocabulary                 (rule 3)
    bare command = useful default               (rule 4)
    every mutating command shows what it did    (rule 5)
    no synonyms — one verb per intent           (rule 6)

Phase 0: every verb is registered with a teaching `--help`; bodies are stubs
that state which phase implements them, except `init`, which works now.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from odx.clients.cli import config as cfgmod
from odx.clients.cli import runtime_cmds as rt
from odx.runtime import commands as rcmd
from odx.runtime import detect_runtime

console = Console()
err_console = Console(stderr=True)

HELP = """\
[bold]odx[/bold] — Odoo Developer Semantic Engine

A semantic tool for real Odoo codebases: scaffold, check, and understand your
addons without booting Odoo. CLI is the first of several thin clients over one
engine.

[dim]Grammar:[/dim] odx <verb> <noun> [target] [--flags]
"""

app = typer.Typer(
    name="odx",
    help=HELP,
    no_args_is_help=False,          # bare `odx` shows a curated cheat-sheet (rule 4)
    rich_markup_mode="rich",
    add_completion=True,            # shell completion (§14 UX requirement)
    context_settings={"help_option_names": ["-h", "--help"]},
)


# --------------------------------------------------------------------------- #
# stub helper — Phase 0 commands announce their backing phase instead of crashing
# --------------------------------------------------------------------------- #
def _stub(what: str, phase: str, writes: bool = False) -> None:
    tag = "[yellow]mutating[/yellow]" if writes else "[green]read-only[/green]"
    console.print(
        Panel(
            f"[bold]{what}[/bold]  ({tag})\n\n"
            f"[dim]Not implemented yet — lands in {phase}.[/dim]\n"
            f"This is a Phase 0 skeleton: the grammar is real, the body is a stub.",
            title="odx",
            border_style="cyan",
        )
    )


# --------------------------------------------------------------------------- #
# bare `odx` — curated cheat-sheet of ~5 common commands (rule 4)
# --------------------------------------------------------------------------- #
@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    t = Table(box=None, pad_edge=False, show_header=False)
    t.add_column(style="bold cyan")
    t.add_column()
    t.add_row("odx init", "set up this project (detect runtime, write config)")
    t.add_row("odx check", "fast static analysis of your addons")
    t.add_row("odx new model <name>", "scaffold a model")
    t.add_row("odx why <field|model>", "where is this used?")
    t.add_row("odx run --watch", "run Odoo with auto-reload")
    console.print(Panel(t, title="odx — common commands", border_style="cyan",
                        subtitle="[dim]odx --help for the full grammar[/dim]"))


# ===========================================================================
# init — the one command that works in Phase 0 (config write)
# ===========================================================================
@app.command()
def init(
    path: Path = typer.Argument(Path("."), help="Project root to initialise."),
    runtime: str | None = typer.Option(
        None, "--runtime",
        help="Force runtime: docker | venv | source | odoo.sh (else auto-detect).",
    ),
    version: str = typer.Option("18.0", "--odoo-version", help="Target Odoo version."),
    project_name: str | None = typer.Option(None, "--name", help="Project name."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Accept detected defaults; don't prompt."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing .odx.toml."),
) -> None:
    """Set up the project: detect the runtime and write [bold].odx.toml[/bold].

    Never silently assumes — detection is confirmed interactively unless
    [bold]--yes[/bold] is given (RFC §21).
    """
    root = path.resolve()
    if not root.is_dir():
        err_console.print(f"[red]error:[/red] {root} is not a directory.")
        raise typer.Exit(2)

    target = root / cfgmod.CONFIG_NAME
    if target.exists() and not force:
        err_console.print(
            f"[red]error:[/red] {target} already exists. "
            f"Re-run with [bold]--force[/bold] to overwrite."
        )
        raise typer.Exit(1)

    detected = runtime or detect_runtime(root)
    chosen = detected or "docker"

    name = project_name or root.name

    if not yes:
        if detected:
            console.print(f"Detected runtime: [bold cyan]{detected}[/bold cyan]")
        else:
            console.print("[yellow]Could not detect a runtime.[/yellow]")
        chosen = typer.prompt("Runtime", default=chosen)
        name = typer.prompt("Project name", default=name)
        version = typer.prompt("Odoo version", default=version)

    cfg = cfgmod.ProjectConfig(
        project=name,
        odoo=cfgmod.OdooConfig(version=version, runtime=chosen),
    )
    written = cfgmod.write_config(root, cfg)
    console.print(f"[green]✓[/green] wrote [bold]{written}[/bold]")
    console.print(f"  project=[cyan]{name}[/cyan] runtime=[cyan]{chosen}[/cyan] "
                  f"odoo=[cyan]{version}[/cyan]")


# ===========================================================================
# daily loop (runtime) — Phase 1
# ===========================================================================
@app.command()
def run(
    watch: bool = typer.Option(False, "--watch", help="Auto-reload on change (--dev)."),
    db: str = typer.Option(None, "--db", "-d", help="Database (default: [database] default)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the command instead of running it."),
) -> None:
    """Run the Odoo instance.

    [dim]Example:[/dim] odx run --watch
    """
    s = rt.load_settings()
    rt.run_one(rcmd.run_command(s, watch=watch, db=db), dry_run=dry_run)


@app.command()
def logs(
    module: str = typer.Argument(None, help="Filter to a module (default: [logs] scope)."),
    level: str = typer.Option(None, "--level", "-l", help="Minimum level: debug|info|warning|error."),
    tail: int = typer.Option(200, "--tail", "-n", help="Lines of history to show first."),
    follow: bool = typer.Option(True, "--follow/--no-follow", help="Keep streaming."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the command instead of running it."),
) -> None:
    """Tail filtered logs.

    [dim]Example:[/dim] odx logs my_module --level warning
    """
    s = rt.load_settings()
    cmd = rcmd.logs_command(s, follow=follow, tail=tail)
    if cmd is None:
        rt.err_console.print("[red]error:[/red] no log file configured — set [bold]\\[logs] file[/bold] "
                             "in .odx.toml, or read the output of [bold]odx run[/bold].")
        raise typer.Exit(2)
    rt.stream_logs(cmd, scope=module or s.logs_scope, level=level, dry_run=dry_run)


@app.command()
def shell(
    db: str = typer.Option(None, "--db", "-d", help="Database (default: [database] default)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the command instead of running it."),
) -> None:
    """Open an Odoo shell."""
    s = rt.load_settings()
    rt.run_one(rcmd.shell_command(s, db=db), dry_run=dry_run)


@app.command()
def test(
    module: str = typer.Argument(None, help="Module to test (default: [logs] scope)."),
    db: str = typer.Option(None, "--db", "-d", help="Database (default: [database] default)."),
    install: bool = typer.Option(False, "--install", help="Install (-i) instead of update (-u)."),
    tags: str = typer.Option(None, "--tags", help="--test-tags spec (default: /<module>)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the command instead of running it."),
) -> None:
    """Run module tests.

    [dim]Example:[/dim] odx test my_module --tags /my_module:TestSale
    """
    s = rt.load_settings()
    module = module or s.logs_scope
    if not module:
        rt.err_console.print("[red]error:[/red] which module? e.g. [bold]odx test my_module[/bold]")
        raise typer.Exit(2)
    rt.run_one(rcmd.odoo_test_command(s, module, db=db, install=install, tags=tags), dry_run=dry_run)


# ===========================================================================
# analysis (engine, read-only) — Phases 3–4
# ===========================================================================
@app.command()
def check(
    only: str = typer.Option(None, "--only", help="Comma list, e.g. views,access,security."),
) -> None:
    """Fast static analysis; report only. [green](read-only)[/green]"""
    _stub("check", "Phase 4 (analyzers)")


@app.command()
def report(
    with_runtime: bool = typer.Option(False, "--with-runtime", help="Include runtime probe."),
) -> None:
    """Scored aggregate health report (§15). [green](read-only)[/green]"""
    _stub("report", "Phase 4 (analyzers)")


@app.command()
def fix(target: str = typer.Argument(None, help="What to fix, e.g. access.")) -> None:
    """Apply safe changes. [yellow](mutating)[/yellow]"""
    _stub("fix", "Phase 4 (analyzers)", writes=True)


@app.command()
def why(target: str = typer.Argument(..., help="A field (model.field) or model.")) -> None:
    """Where is this used? (terse). [green](read-only)[/green]"""
    _stub(f"why {target}", "Phase 3/6 (index + polish)")


@app.command()
def impact(target: str = typer.Argument(..., help="A field (model.field) or model.")) -> None:
    """Where is this used? (detailed report). [green](read-only)[/green]"""
    _stub(f"impact {target}", "Phase 3/6 (index + polish)")


# ===========================================================================
# runtime-composed (engine + runtime) — Phase 6
# ===========================================================================
@app.command()
def doctor() -> None:
    """Runtime fresh-install probe (§15). [green](read-only)[/green]"""
    _stub("doctor", "Phase 6 (runtime introspection)")


# ===========================================================================
# noun sub-apps: new / add / inherit / db / diff / upgrade
# ===========================================================================
new_app = typer.Typer(help="Make something new. [yellow](mutating)[/yellow]")
add_app = typer.Typer(help="Add to something existing. [yellow](mutating)[/yellow]")
inherit_app = typer.Typer(help="Scaffold an inherited artifact. [yellow](mutating)[/yellow]")
db_app = typer.Typer(help="Database operations. [dim](varies)[/dim]")
diff_app = typer.Typer(help="Compare code vs a live instance. [green](read-only)[/green]")
upgrade_app = typer.Typer(help="Migration helpers. [dim](varies)[/dim]")

app.add_typer(new_app, name="new")
app.add_typer(add_app, name="add")
app.add_typer(inherit_app, name="inherit")
app.add_typer(db_app, name="db")
app.add_typer(diff_app, name="diff")
app.add_typer(upgrade_app, name="upgrade")


@new_app.command("module")
def new_module(name: str) -> None:
    """Scaffold a new module."""
    _stub(f"new module {name}", "Phase 2 (scaffolding)", writes=True)


@new_app.command("model")
def new_model(name: str) -> None:
    """Scaffold a new model."""
    _stub(f"new model {name}", "Phase 2 (scaffolding)", writes=True)


@new_app.command("wizard")
def new_wizard(name: str) -> None:
    """Scaffold a transient wizard model."""
    _stub(f"new wizard {name}", "Phase 2 (scaffolding)", writes=True)


@new_app.command("report")
def new_report(name: str) -> None:
    """Scaffold a QWeb report."""
    _stub(f"new report {name}", "Phase 2 (scaffolding)", writes=True)


@new_app.command("controller")
def new_controller(name: str) -> None:
    """Scaffold an HTTP controller."""
    _stub(f"new controller {name}", "Phase 2 (scaffolding)", writes=True)


@add_app.command("field")
def add_field(
    name: str,
    to: str = typer.Option(..., "--to", help="Target model."),
) -> None:
    """Add a field to a model (field-type matrix, §16)."""
    _stub(f"add field {name} --to {to}", "Phase 2/4 (scaffolding + validation)", writes=True)


@add_app.command("view")
def add_view(
    kind: str,
    for_: str = typer.Option(..., "--for", help="Target model."),
) -> None:
    """Add a view for a model."""
    _stub(f"add view {kind} --for {for_}", "Phase 2 (scaffolding)", writes=True)


@add_app.command("menu")
def add_menu(
    label: str,
    under: str = typer.Option(..., "--under", help="Parent menu."),
) -> None:
    """Add a menu item."""
    _stub(f'add menu "{label}" --under {under}', "Phase 2 (scaffolding)", writes=True)


@inherit_app.command("view")
def inherit_view(xmlid: str) -> None:
    """Scaffold an inherited view for an existing view xmlid."""
    _stub(f"inherit view {xmlid}", "Phase 2 (scaffolding)", writes=True)


@db_app.command("list")
def db_list(dry_run: bool = typer.Option(False, "--dry-run", help="Print the command instead of running it.")) -> None:
    """List databases. [green](read-only)[/green]"""
    s = rt.load_settings()
    res = rt.run_one(rcmd.db_list_command(s), dry_run=dry_run, capture=True)
    for line in res.stdout.splitlines():
        if line.strip():
            typer.echo(line.strip())


@db_app.command("new")
def db_new(
    name: str,
    bare: bool = typer.Option(False, "--bare", help="Empty postgres db (no Odoo base install)."),
    demo: bool = typer.Option(False, "--demo", help="Load demo data."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the command instead of running it."),
) -> None:
    """Create a database (Odoo-initialised unless --bare). [yellow](mutating)[/yellow]"""
    s = rt.load_settings()
    rt.run_one(rcmd.db_new_command(s, name, bare=bare, demo=demo), dry_run=dry_run)
    if not dry_run:
        console.print(f"[green]✓[/green] created database [bold]{name}[/bold]")


@db_app.command("drop")
def db_drop(name: str, yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask for confirmation."), dry_run: bool = typer.Option(False, "--dry-run", help="Print the command instead of running it.")) -> None:
    """Drop a database. [yellow](mutating)[/yellow]"""
    s = rt.load_settings()
    rt.confirm_mutation(f"Drop database {name}", yes=yes, dry_run=dry_run)
    rt.run_one(rcmd.db_drop_command(s, name), dry_run=dry_run)
    if not dry_run:
        console.print(f"[green]✓[/green] dropped database [bold]{name}[/bold]")


@db_app.command("copy")
def db_copy(src: str, dst: str, dry_run: bool = typer.Option(False, "--dry-run", help="Print the command instead of running it.")) -> None:
    """Copy a database (postgres template copy; filestore not copied). [yellow](mutating)[/yellow]"""
    s = rt.load_settings()
    rt.run_one(rcmd.db_copy_command(s, src, dst), dry_run=dry_run)
    if not dry_run:
        console.print(f"[green]✓[/green] copied [bold]{src}[/bold] → [bold]{dst}[/bold]")


@db_app.command("dump")
def db_dump(
    name: str,
    output: Path = typer.Option(None, "--output", "-o", help="Dump file (default: <name>.dump)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the command instead of running it."),
) -> None:
    """Dump a database (pg_dump custom format) to a file."""
    s = rt.load_settings()
    out = output or Path(f"{name}.dump")
    rt.run_one(rcmd.db_dump_command(s, name, out), dry_run=dry_run)
    if not dry_run:
        console.print(f"[green]✓[/green] wrote [bold]{out}[/bold]")


@db_app.command("restore")
def db_restore(
    name: str,
    dump_file: Path = typer.Argument(..., help="pg_dump custom-format file."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask for confirmation."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the command instead of running it."),
) -> None:
    """Restore a dump into a new database. [yellow](mutating)[/yellow]"""
    s = rt.load_settings()
    if not dry_run and not dump_file.is_file():
        rt.err_console.print(f"[red]error:[/red] {dump_file} does not exist.")
        raise typer.Exit(2)
    rt.confirm_mutation(f"Restore {dump_file} into new database {name}", yes=yes, dry_run=dry_run)
    rt.run_commands(rcmd.db_restore_commands(s, name, dump_file), dry_run=dry_run)
    if not dry_run:
        console.print(f"[green]✓[/green] restored into [bold]{name}[/bold]")


@db_app.command("anonymize")
def db_anonymize(name: str, yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask for confirmation."), dry_run: bool = typer.Option(False, "--dry-run", help="Print the command instead of running it.")) -> None:
    """Scrub personal data in place (partners, user logins, messages). [yellow](mutating)[/yellow]"""
    s = rt.load_settings()
    rt.confirm_mutation(f"Anonymize database {name} in place", yes=yes, dry_run=dry_run)
    rt.run_one(rcmd.db_anonymize_command(s, name), dry_run=dry_run)
    if not dry_run:
        console.print(f"[green]✓[/green] anonymized [bold]{name}[/bold]")


@diff_app.command("db")
def diff_db() -> None:
    """Diff code against a live DB. [green](read-only)[/green]"""
    _stub("diff db", "Phase 6 (runtime introspection)")


@upgrade_app.command("check")
def upgrade_check(to: str = typer.Option(..., "--to", help="Target version.")) -> None:
    """Check migration readiness. [green](read-only)[/green]"""
    _stub(f"upgrade check --to {to}", "Phase 7 (migration)")


@upgrade_app.command("apply")
def upgrade_apply(to: str = typer.Option(..., "--to", help="Target version.")) -> None:
    """Apply migration edits. [yellow](mutating)[/yellow]"""
    _stub(f"upgrade apply --to {to}", "Phase 7 (migration)", writes=True)


if __name__ == "__main__":
    app()
