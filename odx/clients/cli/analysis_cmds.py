"""CLI glue for the engine verbs: check / report / fix / why / impact /
doctor / diff db / upgrade (RFC §14, §15).

Rendering only (RFC §13): the engine returns Diagnostics / HealthReport /
UsageReport / EditPlan; this file turns them into tables, JSON, GitHub
annotations or SARIF, and maps severities to exit codes.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from odx.clients import serialize
from odx.clients.ci import github_annotations, sarif
from odx.engine.api import Engine
from odx.engine.config import find_config
from odx.engine.contracts import Diagnostic, HealthReport, Scope, Severity, UsageReport

console = Console()
err_console = Console(stderr=True)

FORMATS = ("text", "json", "github", "sarif")
_STYLE = {Severity.ERROR: "red", Severity.WARNING: "yellow",
          Severity.INFO: "cyan", Severity.HINT: "dim"}
_RANK = {Severity.ERROR: 3, Severity.WARNING: 2, Severity.INFO: 1, Severity.HINT: 0}


def project_root(path: Path | None = None) -> Path:
    start = (path or Path.cwd()).resolve()
    cfg = find_config(start)
    return cfg.parent if cfg else start


def engine(path: Path | None = None) -> Engine:
    root = project_root(path)
    with err_console.status("[dim]indexing addons…[/dim]", spinner="dots"):
        eng = Engine.load(root if path is None else Path(path).resolve())
        eng.api                                     # build the pipeline now
    return eng


def scope(eng: Engine, modules: str | None, all_modules: bool) -> Scope:
    if modules:
        mods = tuple(m.strip() for m in modules.split(",") if m.strip())
        return Scope(label="modules", modules=mods)
    if all_modules:
        return Scope(label="all")
    return Scope(label="project")


def _rel(p: Path, root: Path) -> str:
    try:
        return str(Path(p).resolve().relative_to(root))
    except ValueError:
        return str(p)


# --------------------------------------------------------------------------- #
# diagnostics
# --------------------------------------------------------------------------- #
def render_diagnostics(diags: list[Diagnostic], fmt: str, root: Path) -> None:
    if fmt == "json":
        typer.echo(serialize.dumps(diags, relative_to=root))
        return
    if fmt == "github":
        out = github_annotations(diags, root)
        if out:
            typer.echo(out)
        return
    if fmt == "sarif":
        typer.echo(json.dumps(sarif(diags, root), indent=2))
        return
    if not diags:
        console.print("[green]✓ no findings[/green]")
        return
    by_file: dict[str, list[Diagnostic]] = defaultdict(list)
    for d in diags:
        by_file[_rel(d.location.file, root) if d.location else "(project)"].append(d)
    for f, ds in by_file.items():
        console.print(f"[bold]{f}[/bold]", highlight=False)
        for d in ds:
            st = _STYLE.get(d.severity, "white")
            line = f"{d.location.start_line}" if d.location and d.location.start_line else "-"
            console.print(f"  [dim]{line:>5}[/dim]  [{st}]{d.severity.value:<7}[/{st}] "
                          f"{d.message}  [dim]{d.code}[/dim]", highlight=False, soft_wrap=True)
            if d.suggested_fix is not None:
                console.print(f"         [dim]fix: {d.suggested_fix.summary} "
                              "(run `odx fix`)[/dim]", highlight=False)
    counts = Counter(d.severity.value for d in diags)
    summary = ", ".join(f"{counts[s.value]} {s.value}" for s in Severity if counts[s.value])
    console.print(f"\n{len(diags)} finding(s): {summary}")


def exit_for(diags: list[Diagnostic], fail_on: str) -> None:
    threshold = {"error": 3, "warning": 2, "info": 1, "hint": 0, "never": 99}.get(fail_on, 3)
    if any(_RANK.get(d.severity, 0) >= threshold for d in diags):
        raise typer.Exit(1)


def run_check(path: Path | None, only: str | None, modules: str | None, all_modules: bool,
              fmt: str, fail_on: str) -> None:
    if fmt not in FORMATS:
        err_console.print(f"[red]error:[/red] --format must be one of {', '.join(FORMATS)}")
        raise typer.Exit(2)
    eng = engine(path)
    diags = eng.check(scope(eng, modules, all_modules),
                      [o for o in (only or "").split(",") if o.strip()] or None)
    render_diagnostics(diags, fmt, project_root(path))
    exit_for(diags, fail_on)


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def render_report(rep: HealthReport, fmt: str, root: Path) -> None:
    if fmt == "json":
        typer.echo(serialize.dumps(rep, relative_to=root))
        return
    color = {"A": "green", "B": "green", "C": "yellow", "D": "yellow", "F": "red"}[rep.grade]
    head = Table.grid(padding=(0, 2))
    head.add_row(f"[bold {color}]{rep.grade}[/bold {color}]",
                 f"[bold]{rep.score}[/bold]/100",
                 " · ".join(f"{v} {k}" for k, v in rep.stats.items()
                            if k in ("modules", "models", "views")))
    console.print(Panel(head, title="odx report", border_style=color))
    t = Table(title="findings by category", show_edge=False)
    t.add_column("category")
    t.add_column("count", justify="right")
    for cat, n in sorted(rep.by_category.items(), key=lambda x: -x[1]):
        t.add_row(cat, str(n))
    if rep.by_category:
        console.print(t)
    sev = ", ".join(f"{n} {k}" for k, n in rep.by_severity.items())
    console.print(f"severity: {sev or 'none'}")
    res = rep.resolution
    if res:
        console.print("field resolution: " + ", ".join(f"{v} {k}" for k, v in sorted(res.items()))
                      + "  [dim](dynamic = runtime-derived, not a defect)[/dim]")
    console.print(f"references indexed: {rep.stats.get('references', 0)} "
                  f"({rep.stats.get('references_unresolved', 0)} unresolved, "
                  f"{rep.stats.get('references_dynamic', 0)} dynamic)")
    if rep.runtime is not None:
        st = "[green]clean install ✓[/green]" if rep.runtime.ok else "[red]install failed ✗[/red]"
        console.print(f"runtime probe ({rep.runtime.database}): {st}")
    if rep.recommendations:
        console.print("\n[bold]recommendations[/bold]")
        for r in rep.recommendations:
            console.print(f"  • {r}", highlight=False)


# --------------------------------------------------------------------------- #
# why / impact — one query, two verbosities (RFC §14 rule 6)
# --------------------------------------------------------------------------- #
def render_usage(u: UsageReport, verbose: bool, fmt: str, root: Path) -> None:
    if fmt == "json":
        typer.echo(serialize.dumps(u, relative_to=root))
        return
    if u.model is None or (u.field and not u.known):
        err_console.print(f"[yellow]{u.message}[/yellow]")
        raise typer.Exit(1)
    what = f"{u.model}.{u.field}" if u.field else u.model
    where = f" — defined in {', '.join(u.defined_in)}" if u.defined_in else ""
    loc = f" ({_rel(u.definition.file, root)}:{u.definition.start_line})" if u.definition else ""
    console.print(f"[bold]{what}[/bold]{where}{loc}", highlight=False)
    if not u.references:
        console.print("  [dim]no references found in the indexed addons[/dim]")
        return
    parts = [f"{n} {k}" for k, n in sorted(u.by_kind.items(), key=lambda x: -x[1])]
    console.print("  " + " · ".join(parts), highlight=False)
    if not verbose:
        console.print("  [dim]`odx impact` for every location[/dim]")
        return
    by_kind: dict[str, list] = defaultdict(list)
    for r in u.references:
        by_kind[r.kind].append(r)
    for kind, refs in sorted(by_kind.items(), key=lambda x: -len(x[1])):
        console.print(f"\n[bold cyan]{kind}[/bold cyan] ({len(refs)})")
        for r in refs:
            mark = "" if r.resolution.value == "static" else f" [yellow][{r.resolution.value}][/yellow]"
            console.print(f"  {_rel(r.location.file, root)}:{r.location.start_line}  "
                          f"[dim]{r.detail} · {r.module}[/dim]{mark}", highlight=False,
                          soft_wrap=True)
