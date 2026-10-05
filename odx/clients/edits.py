"""Client-side EditPlan application & rendering (shared by CLI and MCP).

Plain text only (no rich) so any client can reuse it. Implements the EditPlan
semantics documented in ``odx.engine.generators``:

* CREATE            — new file with ``new_text``; refused if it already exists.
* REPLACE, no range — whole-file replacement.
* REPLACE, range    — replace the span (1-based lines, 0-based columns).
* INSERT, range     — insert at the range start.
"""

from __future__ import annotations

import difflib
import os
from pathlib import Path

from odx.engine.contracts import EditKind, EditPlan, FileEdit, Location


class PlanApplyError(RuntimeError):
    """The tree no longer matches what the plan assumed (nothing was written)."""


def _resolve(path: Path, root: Path | None) -> Path:
    p = Path(path)
    return p if p.is_absolute() or root is None else Path(root) / p


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None


def _offset(text: str, line: int, col: int) -> int:
    lines = text.splitlines(keepends=True)
    if line - 1 > len(lines):
        raise PlanApplyError(f"line {line} is past the end of the file")
    return sum(len(x) for x in lines[: line - 1]) + col


def _apply_range(text: str, rng: Location, new_text: str, insert: bool) -> str:
    start = _offset(text, rng.start_line, rng.start_col)
    if insert:
        return text[:start] + new_text + text[start:]
    end = _offset(text, rng.end_line, rng.end_col)
    return text[:start] + new_text + text[end:]


def result_text(edit: FileEdit, current: str | None) -> str:
    """The file content after ``edit`` (validates the edit against ``current``)."""
    if edit.kind is EditKind.CREATE:
        if current is not None:
            raise PlanApplyError(f"refusing to CREATE {edit.file}: it already exists")
        return edit.new_text
    if current is None:
        if edit.kind is EditKind.REPLACE and edit.range is None:
            return edit.new_text                # whole-file replace of a vanished file
        raise PlanApplyError(f"cannot {edit.kind.value} {edit.file}: file does not exist")
    if edit.kind is EditKind.REPLACE:
        if edit.range is None:
            return edit.new_text
        return _apply_range(current, edit.range, edit.new_text, insert=False)
    if edit.range is None:
        raise PlanApplyError(f"INSERT into {edit.file} needs a range")
    return _apply_range(current, edit.range, edit.new_text, insert=True)


def apply_plan(plan: EditPlan, root: Path | None = None, dry_run: bool = False) -> list[Path]:
    """Apply every edit; returns the touched paths. All edits are validated
    before anything is written, so a stale plan writes nothing."""
    staged: list[tuple[Path, str]] = []
    for e in plan.edits:
        path = _resolve(e.file, root)
        staged.append((path, result_text(e, _read(path))))
    if dry_run:
        return [p for p, _ in staged]
    for path, text in staged:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return [p for p, _ in staged]


_MARK = {EditKind.CREATE: "+", EditKind.REPLACE: "~", EditKind.INSERT: "~"}


def render_plan(plan: EditPlan, root: Path | None = None, diff: bool = True,
                cwd: Path | None = None) -> str:
    """Plain-text rendering: summary, file tree (``+`` created, ``~`` modified),
    then a unified diff per modified file."""
    out = [f"Plan: {plan.summary}"]
    if not plan.edits:
        out.append("  (nothing to do — already up to date)")
        return "\n".join(out) + "\n"
    paths = [_resolve(e.file, root) for e in plan.edits]
    base = Path(os.path.commonpath([str(p.parent) for p in paths]))
    shown_base = base
    if cwd is not None:
        try:
            shown_base = base.relative_to(cwd)
        except ValueError:
            pass
    out.append(f"  {shown_base}/")
    for e, p in sorted(zip(plan.edits, paths), key=lambda t: str(t[1])):
        rel = p.relative_to(base)
        note = "" if e.kind is not EditKind.INSERT else " (insert)"
        out.append(f"    {_MARK[e.kind]} {rel}{note}")
    if diff:
        for e, p in zip(plan.edits, paths):
            if e.kind is EditKind.CREATE:
                continue
            cur = _read(p)
            try:
                new = result_text(e, cur)
            except PlanApplyError as exc:
                out.append(f"\n! {exc}")
                continue
            rel = str(p.relative_to(base))
            d = difflib.unified_diff(
                (cur or "").splitlines(keepends=True), new.splitlines(keepends=True),
                fromfile=f"a/{rel}", tofile=f"b/{rel}")
            text = "".join(line if line.endswith("\n") else line + "\n" for line in d)
            if text:
                out.append("")
                out.append(text.rstrip("\n"))
    return "\n".join(out) + "\n"
