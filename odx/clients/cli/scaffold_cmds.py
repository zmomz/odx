"""Shared helpers for the mutating scaffold commands (new / add / inherit).

Keeps ``main.py`` hunks small: project discovery, engine construction, module
inference, and the "show the plan, then apply" step (grammar rule 5).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape as E

from odx.clients.cli import config as cfgmod
from odx.clients.edits import PlanApplyError, apply_plan, render_plan
from odx.engine.api import Engine, GeneratorError
from odx.engine.contracts import Diagnostic, EditPlan, Severity

console = Console()
err_console = Console(stderr=True)


@dataclass
class Project:
    root: Path
    addons_paths: list[Path]
    version: str


def project(cwd: Path | None = None) -> Project:
    """Root = directory holding ``.odx.toml`` (else cwd); addons paths from
    ``[odoo].addons_paths`` relative to it (existing ones; else the root)."""
    cwd = (cwd or Path.cwd()).resolve()
    cfg_path = cfgmod.find_config(cwd)
    if cfg_path is None:
        return Project(cwd, [cwd], "17.0")
    root = cfg_path.parent
    try:
        data = cfgmod.load_config(cfg_path)
    except Exception:                                   # malformed config: be lenient
        data = {}
    odoo = data.get("odoo", {}) if isinstance(data, dict) else {}
    paths = [(root / p).resolve() for p in odoo.get("addons_paths", []) or []]
    paths = [p for p in paths if p.is_dir()] or [root]
    return Project(root, paths, str(odoo.get("version") or "17.0"))


def engine(proj: Project) -> Engine:
    eng = Engine.load(proj.root)
    eng.set_addons_paths(proj.addons_paths)
    eng.odoo_version = proj.version
    return eng


def fail(msg: str, code: int = 1) -> None:
    err_console.print(f"[red]error:[/red] {E(msg)}", highlight=False)
    raise typer.Exit(code)


def module_of_cwd(cwd: Path | None = None) -> str | None:
    return Engine.module_containing(cwd or Path.cwd())


def resolve_module(proj: Project, module: str | None) -> str:
    """--module, else the module containing cwd, else the only module in the
    project; otherwise ask the user to choose (an error that says what to do)."""
    if module:
        return module
    here = module_of_cwd()
    if here:
        return here
    names = engine(proj).discover_modules()
    if len(names) == 1:
        return names[0]
    if not names:
        fail("no Odoo module found here. Create one with `odx new module <name>`.")
    fail(f"several modules found ({', '.join(names)}); pass --module <name>.")
    return ""  # unreachable


def split_csv(value: str | None) -> tuple[str, ...]:
    return tuple(v.strip() for v in (value or "").split(",") if v.strip())


def parse_options(value: str | None) -> tuple[tuple[str, str], ...]:
    """``"draft:Draft,done:Done"`` -> ``(("draft", "Draft"), ("done", "Done"))``."""
    out = []
    for item in split_csv(value):
        key, _, label = item.partition(":")
        out.append((key.strip(), (label or key).strip()))
    return tuple(out)


def show_diagnostics(diags: list[Diagnostic]) -> bool:
    """Print diagnostics; True if any is an error."""
    styles = {Severity.ERROR: "red", Severity.WARNING: "yellow"}
    for d in diags:
        style = styles.get(d.severity, "cyan")
        err_console.print(f"[{style}]{d.severity.value}[/{style}] {E(d.message)} "
                          f"[dim]({E(d.code)})[/dim]", highlight=False)
    return any(d.severity is Severity.ERROR for d in diags)


def plan_or_fail(make) -> EditPlan:
    try:
        return make()
    except GeneratorError as e:
        fail(str(e))
    raise AssertionError  # unreachable


def emit(plan: EditPlan, dry_run: bool) -> None:
    """Rule 5: print what will be / was done, then apply unless --dry-run."""
    console.print(render_plan(plan, cwd=Path.cwd().resolve()), markup=False,
                  highlight=False, soft_wrap=True, end="")
    if not plan.edits:
        return
    if dry_run:
        console.print("[dim](dry run — nothing written)[/dim]")
        return
    try:
        written = apply_plan(plan)
    except (PlanApplyError, OSError) as e:
        fail(f"could not apply plan: {e}")
    console.print(f"[green]✓[/green] {len(written)} file(s) written")
