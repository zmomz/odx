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
from odx.clients.cli import scaffold_cmds as sc
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
def run(watch: bool = typer.Option(False, "--watch", help="Auto-reload on change.")) -> None:
    """Run the Odoo instance."""
    _stub("run" + (" --watch" if watch else ""), "Phase 1 (runtime wrappers)")


@app.command()
def logs(module: str = typer.Argument(None, help="Filter to a module.")) -> None:
    """Tail filtered logs."""
    _stub("logs", "Phase 1 (runtime wrappers)")


@app.command()
def shell() -> None:
    """Open an Odoo shell."""
    _stub("shell", "Phase 1 (runtime wrappers)")


@app.command()
def test(module: str = typer.Argument(None, help="Module to test.")) -> None:
    """Run module tests."""
    _stub("test", "Phase 1 (runtime wrappers)")


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


_DRY = typer.Option(False, "--dry-run", help="Show the plan; write nothing.")
_MODULE = typer.Option(None, "--module", "-m",
                       help="Target module (default: the module you're in, or the only one).")


@new_app.command("module")
def new_module(
    name: str,
    path: Path = typer.Option(None, "--path", help="Addons directory (default: first addons path)."),
    depends: str = typer.Option("base", "--depends", help="Comma list of dependencies."),
    summary: str = typer.Option("", "--summary", help="One-line summary."),
    author: str = typer.Option("", "--author", help="Author."),
    application: bool = typer.Option(False, "--application", help="Mark as an application."),
    dry_run: bool = _DRY,
) -> None:
    """Scaffold a new module.  e.g. [bold]odx new module library --depends base,mail[/bold]"""
    from odx.engine.contracts import ModuleSpec
    proj = sc.project()
    eng = sc.engine(proj)
    spec = ModuleSpec(name=name, addons_path=(path or proj.addons_paths[0]).resolve(),
                      summary=summary, depends=sc.split_csv(depends), author=author,
                      version=f"{proj.version}.1.0.0", application=application)
    sc.emit(sc.plan_or_fail(lambda: eng.new_module(spec)), dry_run)


def _model_cmd(name: str, module: str | None, description: str, inherit: str | None,
               kind: str, views: bool, access: bool, menu: bool, dry_run: bool,
               wizard: bool = False) -> None:
    from odx.engine.contracts import ModelSpec
    proj = sc.project()
    eng = sc.engine(proj)
    spec = ModelSpec(name=name, module=sc.resolve_module(proj, module),
                     description=description, kind=kind, inherit=sc.split_csv(inherit),
                     with_views=views, with_access=access, with_menu=menu)
    make = eng.new_wizard if wizard else eng.new_model
    sc.emit(sc.plan_or_fail(lambda: make(spec)), dry_run)


@new_app.command("model")
def new_model(
    name: str,
    module: str = _MODULE,
    description: str = typer.Option("", "--description", help="_description (default: from name)."),
    inherit: str = typer.Option(None, "--inherit", help="Comma list of _inherit parents."),
    kind: str = typer.Option("Model", "--kind", help="Model | TransientModel | AbstractModel."),
    views: bool = typer.Option(True, "--views/--no-views", help="Generate form/list/search views."),
    access: bool = typer.Option(True, "--access/--no-access", help="Add an ACL row."),
    menu: bool = typer.Option(True, "--menu/--no-menu", help="Add a window action menu."),
    dry_run: bool = _DRY,
) -> None:
    """Scaffold a new model.  e.g. [bold]odx new model library.book[/bold]"""
    _model_cmd(name, module, description, inherit, kind, views, access, menu, dry_run)


@new_app.command("wizard")
def new_wizard(
    name: str,
    module: str = _MODULE,
    description: str = typer.Option("", "--description", help="_description."),
    dry_run: bool = _DRY,
) -> None:
    """Scaffold a transient wizard model.  e.g. [bold]odx new wizard library.book.import[/bold]"""
    _model_cmd(name, module, description, None, "TransientModel", True, True, False,
               dry_run, wizard=True)


@new_app.command("report")
def new_report(
    name: str,
    model: str = typer.Option(..., "--model", help="Model the report prints."),
    module: str = _MODULE,
    title: str = typer.Option("", "--title", help="Report title (default: from name)."),
    dry_run: bool = _DRY,
) -> None:
    """Scaffold a QWeb report.  e.g. [bold]odx new report book_card --model library.book[/bold]"""
    from odx.engine.generators import ReportSpec
    proj = sc.project()
    eng = sc.engine(proj)
    spec = ReportSpec(name=name, module=sc.resolve_module(proj, module), model=model,
                      title=title)
    sc.emit(sc.plan_or_fail(lambda: eng.new_report(spec)), dry_run)


@new_app.command("controller")
def new_controller(
    name: str,
    module: str = _MODULE,
    route: str = typer.Option(None, "--route", help="URL (default: /<module>/<name>)."),
    auth: str = typer.Option("user", "--auth", help="user | public | none."),
    dry_run: bool = _DRY,
) -> None:
    """Scaffold an HTTP controller.  e.g. [bold]odx new controller library[/bold]"""
    from odx.engine.generators import ControllerSpec
    proj = sc.project()
    eng = sc.engine(proj)
    spec = ControllerSpec(name=name, module=sc.resolve_module(proj, module), route=route,
                          auth=auth)
    sc.emit(sc.plan_or_fail(lambda: eng.new_controller(spec)), dry_run)


@add_app.command("field")
def add_field(
    name: str,
    to: str = typer.Option(..., "--to", help="Target model."),
    type_: str = typer.Option(None, "--type", help="Field type (default: guessed from the name, confirmed)."),
    comodel: str = typer.Option(None, "--comodel", help="Relational target model."),
    inverse: str = typer.Option(None, "--inverse", help="One2many inverse field."),
    relation: str = typer.Option(None, "--relation", help="Many2many relation table."),
    required: bool = typer.Option(False, "--required", help="required=True."),
    string: str = typer.Option(None, "--string", help="Label."),
    selection: str = typer.Option(None, "--selection", help='Selection options "key:Label,key2:Label 2".'),
    default: str = typer.Option(None, "--default", help="Default as a Python literal, e.g. \"'draft'\"."),
    currency_field: str = typer.Option(None, "--currency-field", help="Monetary currency field."),
    compute: str = typer.Option(None, "--compute", help="Compute method name."),
    depends: str = typer.Option(None, "--depends", help="Comma list of @api.depends paths."),
    store: bool = typer.Option(False, "--store", help="Store a computed field."),
    help_: str = typer.Option(None, "--help-text", help="Field tooltip (help=)."),
    module: str = typer.Option(None, "--module", "-m", help="Module that gets the edit."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Accept guesses; don't prompt."),
    dry_run: bool = _DRY,
) -> None:
    """Add a field to a model (field-type matrix, §16).

    e.g. [bold]odx add field partner_id --to library.book --comodel res.partner[/bold]
    """
    from odx.engine.contracts import FieldSpec
    from odx.engine.generators import guess_type, normalize_type
    proj = sc.project()
    eng = sc.engine(proj)
    ftype = type_
    if not ftype:
        guesses = guess_type(name)
        if yes:
            ftype = guesses[0]
        else:
            ftype = typer.prompt(f"Field type (suggested: {', '.join(guesses)})",
                                 default=guesses[0])
    ftype = normalize_type(ftype) or ftype
    if not yes and ftype in ("Many2one", "One2many", "Many2many") and not comodel:
        comodel = typer.prompt("Comodel (target model)")
    if not yes and ftype == "One2many" and not inverse:
        inverse = typer.prompt(f"Inverse Many2one field on {comodel}")
    options = sc.parse_options(selection)
    if not yes and ftype == "Selection" and not options:
        options = sc.parse_options(typer.prompt('Options ("key:Label,key2:Label 2")'))
    spec = FieldSpec(model=to, name=name, type=ftype, module=module, string=string,
                     comodel=comodel, inverse_name=inverse, relation=relation,
                     required=required, default=default, help=help_, options=options,
                     currency_field=currency_field, compute=compute,
                     depends=sc.split_csv(depends), store=store)
    if sc.show_diagnostics(eng.validate_field(spec)):
        raise typer.Exit(1)
    sc.emit(sc.plan_or_fail(lambda: eng.add_field(spec)), dry_run)


@add_app.command("view")
def add_view(
    kind: str,
    for_: str = typer.Option(..., "--for", help="Target model."),
    module: str = _MODULE,
    fields: str = typer.Option(None, "--fields", help="Comma list of fields (default: all known)."),
    dry_run: bool = _DRY,
) -> None:
    """Add a view for a model.  e.g. [bold]odx add view kanban --for library.book[/bold]"""
    from odx.engine.contracts import ViewSpec
    proj = sc.project()
    eng = sc.engine(proj)
    spec = ViewSpec(model=for_, kind=kind, module=sc.resolve_module(proj, module),
                    fields=sc.split_csv(fields))
    sc.emit(sc.plan_or_fail(lambda: eng.add_view(spec)), dry_run)


@add_app.command("menu")
def add_menu(
    label: str,
    under: str = typer.Option(None, "--under", help="Parent menu xmlid (default: top level)."),
    model: str = typer.Option(None, "--model", help="Open this model's list (creates an action)."),
    action: str = typer.Option(None, "--action", help="Existing action xmlid."),
    module: str = _MODULE,
    dry_run: bool = _DRY,
) -> None:
    """Add a menu item.  e.g. [bold]odx add menu "Books" --under library.menu_root --model library.book[/bold]"""
    from odx.engine.contracts import MenuSpec
    proj = sc.project()
    eng = sc.engine(proj)
    spec = MenuSpec(label=label, module=sc.resolve_module(proj, module), parent=under,
                    model=model, action=action)
    sc.emit(sc.plan_or_fail(lambda: eng.add_menu(spec)), dry_run)


@inherit_app.command("view")
def inherit_view(
    xmlid: str,
    model: str = typer.Option(None, "--model", help="Model of the view (default: looked up)."),
    kind: str = typer.Option("form", "--kind", help="form | list | kanban | search."),
    fields: str = typer.Option(None, "--fields", help="Comma list of fields to insert."),
    module: str = _MODULE,
    dry_run: bool = _DRY,
) -> None:
    """Scaffold an inherited view.  e.g. [bold]odx inherit view base.view_partner_form --fields x_note[/bold]"""
    from odx.engine.contracts import ViewSpec
    proj = sc.project()
    eng = sc.engine(proj)
    spec = ViewSpec(model=model or "", kind=kind, module=sc.resolve_module(proj, module),
                    inherit_xmlid=xmlid, fields=sc.split_csv(fields))
    sc.emit(sc.plan_or_fail(lambda: eng.add_view(spec)), dry_run)


@db_app.command("list")
def db_list() -> None:
    """List databases."""
    _stub("db list", "Phase 1 (runtime wrappers)")


@db_app.command("new")
def db_new(name: str) -> None:
    """Create a database."""
    _stub(f"db new {name}", "Phase 1 (runtime wrappers)", writes=True)


@db_app.command("drop")
def db_drop(name: str) -> None:
    """Drop a database."""
    _stub(f"db drop {name}", "Phase 1 (runtime wrappers)", writes=True)


@db_app.command("copy")
def db_copy(src: str, dst: str) -> None:
    """Copy a database."""
    _stub(f"db copy {src} {dst}", "Phase 1 (runtime wrappers)", writes=True)


@db_app.command("dump")
def db_dump(name: str) -> None:
    """Dump a database."""
    _stub(f"db dump {name}", "Phase 6 (runtime introspection)", writes=True)


@db_app.command("restore")
def db_restore(name: str) -> None:
    """Restore a database."""
    _stub(f"db restore {name}", "Phase 6 (runtime introspection)", writes=True)


@db_app.command("anonymize")
def db_anonymize(name: str) -> None:
    """Anonymize a database."""
    _stub(f"db anonymize {name}", "Phase 6 (runtime introspection)", writes=True)


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
