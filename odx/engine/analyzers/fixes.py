"""Safe, mechanical fixes (`odx fix`, Diagnostic.suggested_fix).

A fix is a pure text transform of ONE file. Each Diagnostic carries its own
single-fix EditPlan; `compose_fixes` folds every transform for the same file
in order into one REPLACE, so several fixes to one manifest/CSV don't clobber
each other. The engine still writes nothing (C3).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Callable

from odx.engine.contracts import Diagnostic, EditKind, EditPlan, FileEdit
from odx.engine.generators.base import (
    GeneratorError,
    access_row,
    csv_add_row,
    disk_read,
    manifest_add_data,
)

Transform = Callable[[str | None], str]
# registry: id(EditPlan) -> [(file, transform)] so compose_fixes can re-fold
_TRANSFORMS: dict[int, list[tuple[Path, Transform]]] = {}


def _plan(summary: str, steps: list[tuple[Path, Transform]]) -> EditPlan | None:
    edits: list[FileEdit] = []
    for path, fn in steps:
        cur = disk_read(path)
        try:
            new = fn(cur)
        except GeneratorError:
            return None
        if cur == new:
            continue
        edits.append(FileEdit(path, EditKind.REPLACE if cur is not None else EditKind.CREATE,
                              None, new))
    if not edits:
        return None
    plan = EditPlan(edits=edits, summary=summary)
    _TRANSFORMS[id(plan)] = steps
    return plan


def _manifest_path(api, module: str) -> Path | None:
    ms = api.module(module)
    if ms is None:
        return None
    for n in ("__manifest__.py", "__openerp__.py"):
        if (ms.path / n).is_file():
            return ms.path / n
    return None


def manifest_add_depends(text: str, dep: str) -> str:
    """Add `dep` to the manifest's 'depends' list (text edit, idempotent)."""
    tree = ast.parse(text)
    d = next((s.value for s in tree.body
              if isinstance(s, ast.Expr) and isinstance(s.value, ast.Dict)), None)
    if d is None:
        raise GeneratorError("manifest has no dict literal")
    lines = text.splitlines(keepends=True)

    def off(lineno: int, col: int) -> int:
        return sum(len(x) for x in lines[:lineno - 1]) + len(
            lines[lineno - 1].encode("utf-8")[:col].decode("utf-8", "ignore"))
    for k, v in zip(d.keys, d.values):
        if isinstance(k, ast.Constant) and k.value == "depends":
            if not isinstance(v, (ast.List, ast.Tuple)):
                raise GeneratorError("'depends' is not a literal list")
            if any(isinstance(e, ast.Constant) and e.value == dep for e in v.elts):
                return text
            end = off(v.end_lineno, v.end_col_offset) - 1          # the closing bracket
            if not v.elts:
                return text[:end] + repr(dep) + text[end:]
            last = v.elts[-1]
            lend = off(last.end_lineno, last.end_col_offset)
            between = text[lend:end]
            if v.elts[0].lineno != v.lineno:                        # multiline
                indent = lines[last.lineno - 1][:len(lines[last.lineno - 1]) - len(lines[last.lineno - 1].lstrip())]
                if between.strip().startswith(","):
                    comma = lend + between.index(",") + 1
                    return text[:comma] + f"\n{indent}{dep!r}," + text[comma:]
                return text[:lend] + f",\n{indent}{dep!r}," + text[lend:]
            if between.strip().startswith(","):
                comma = lend + between.index(",") + 1
                return text[:comma] + f" {dep!r}" + text[comma:]
            return text[:lend] + f", {dep!r}" + text[lend:]
    # no depends key: add one after the opening brace
    start = off(d.lineno, d.col_offset) + 1
    return text[:start] + f"\n    'depends': [{dep!r}]," + text[start:]


def add_dependency_fix(api, module: str, dep: str) -> EditPlan | None:
    mp = _manifest_path(api, module)
    if mp is None:
        return None
    return _plan(f"add '{dep}' to {module} depends",
                 [(mp, lambda t, dep=dep: manifest_add_depends(t or "", dep))])


def missing_acl_fix(api, model: str, module: str | None) -> EditPlan | None:
    if not module:
        return None
    ms = api.module(module)
    mp = _manifest_path(api, module)
    if ms is None or mp is None:
        return None
    csv_path = ms.path / "security" / "ir.model.access.csv"
    return _plan(f"grant base.group_user access to {model}", [
        (csv_path, lambda t, m=model: csv_add_row(t, access_row(m))),
        (mp, lambda t: manifest_add_data(t or "", ["security/ir.model.access.csv"])),
    ])


def compose_fixes(api, diagnostics: list[Diagnostic]) -> EditPlan:
    """Fold every diagnostic's fix transforms per file into one EditPlan."""
    per_file: dict[Path, list[Transform]] = {}
    order: list[Path] = []
    summaries: list[str] = []
    for d in diagnostics:
        plan = d.suggested_fix
        if plan is None:
            continue
        steps = _TRANSFORMS.get(id(plan))
        if not steps:
            continue
        summaries.append(plan.summary)
        for path, fn in steps:
            if path not in per_file:
                per_file[path] = []
                order.append(path)
            per_file[path].append(fn)
    edits: list[FileEdit] = []
    for path in order:
        cur = disk_read(path)
        text = cur
        for fn in per_file[path]:
            try:
                text = fn(text)
            except GeneratorError:
                continue
        if text is not None and text != cur:
            edits.append(FileEdit(path, EditKind.REPLACE if cur is not None else EditKind.CREATE,
                                  None, text))
    uniq = list(dict.fromkeys(summaries))
    return EditPlan(edits=edits, summary="; ".join(uniq) if uniq else "nothing to fix")
