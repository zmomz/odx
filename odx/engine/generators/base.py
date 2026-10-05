"""Shared generator plumbing: the injected reader, the error type, the Jinja2
environment, and conservative *text* editors for the files generators extend
(manifest, ``__init__.py``, access CSV, XML data files, Python classes).

Every editor here is a pure ``str -> str`` function: it never touches disk, and
it is idempotent (re-applying an edit that is already present returns the input
unchanged), which is what makes re-running a generator safe.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

import jinja2

from odx.engine.contracts import EditKind, FileEdit
from odx.engine.generators.naming import humanize

ReadFn = Callable[[Path], "str | None"]
"""Injected reader: current text of a file, or None if it doesn't exist."""

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

ACCESS_CSV_HEADER = "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink"


class GeneratorError(ValueError):
    """A generator cannot produce a correct plan (bad spec, target missing,
    artifact already exists, unparsable file it must edit). Clients render the
    message; nothing has been written."""


def disk_read(path: Path) -> str | None:
    """The default reader engine methods inject: read-only disk access."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# plan building
# --------------------------------------------------------------------------- #
def put(edits: list[FileEdit], path: Path, new_text: str, read: ReadFn,
        current: str | None = None, *, known: bool = False) -> None:
    """Append the right whole-file edit for ``path`` -> ``new_text``:
    CREATE if absent, REPLACE (range=None) if different, nothing if identical.

    ``known=True`` means ``current`` is the already-read text (may be None)."""
    cur = current if known else read(path)
    if cur is None:
        edits.append(FileEdit(path, EditKind.CREATE, None, new_text))
    elif cur != new_text:
        edits.append(FileEdit(path, EditKind.REPLACE, None, new_text))


class Overlay:
    """A reader that sees edits planned so far, so a generator composed of
    several steps (e.g. add_field creating an inheriting class) reads its own
    pending changes instead of stale disk."""

    def __init__(self, read: ReadFn) -> None:
        self._read = read
        self._pending: dict[Path, str] = {}

    def __call__(self, path: Path) -> str | None:
        if path in self._pending:
            return self._pending[path]
        return self._read(path)

    def put(self, edits: list[FileEdit], path: Path, new_text: str) -> None:
        cur = self(path)
        if cur == new_text:
            return
        existing_idx = next((i for i, e in enumerate(edits) if e.file == path), None)
        on_disk = self._read(path)
        kind = EditKind.CREATE if on_disk is None else EditKind.REPLACE
        fe = FileEdit(path, kind, None, new_text)
        if existing_idx is None:
            edits.append(fe)
        else:
            edits[existing_idx] = fe
        self._pending[path] = new_text


# --------------------------------------------------------------------------- #
# Jinja2
# --------------------------------------------------------------------------- #
def _xa(v: object) -> str:
    return _xml_escape(str(v), {'"': "&quot;"})


def _env() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
        undefined=jinja2.StrictUndefined,
    )
    env.filters["py"] = repr
    env.filters["xml"] = lambda v: _xml_escape(str(v))
    env.filters["xa"] = _xa
    env.filters["humanize"] = humanize
    return env


ENV = _env()


def render(template: str, **ctx: object) -> str:
    return ENV.get_template(template).render(**ctx)


def xml_fragment(macro: str, *args: object, **kw: object) -> str:
    """Render one macro of ``xml.j2`` (a record fragment indented for <odoo>)."""
    return str(getattr(ENV.get_template("xml.j2").module, macro)(*args, **kw))


# --------------------------------------------------------------------------- #
# offsets (ast columns are UTF-8 byte offsets)
# --------------------------------------------------------------------------- #
def _offset(text: str, lineno: int, col_bytes: int) -> int:
    lines = text.splitlines(keepends=True)
    start = sum(len(line) for line in lines[: lineno - 1])
    line = lines[lineno - 1] if lineno - 1 < len(lines) else ""
    return start + len(line.encode("utf-8")[:col_bytes].decode("utf-8", errors="ignore"))


def _line_indent(text: str, offset: int) -> str:
    start = text.rfind("\n", 0, offset) + 1
    m = re.match(r"[ \t]*", text[start:])
    return m.group(0) if m else ""


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #
def _manifest_dict(text: str) -> ast.Dict:
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        raise GeneratorError(f"cannot parse __manifest__.py: {e.msg} (line {e.lineno})")
    for stmt in tree.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Dict):
            return stmt.value
    raise GeneratorError("__manifest__.py does not contain a dict literal")


def _is_security(entry: str) -> bool:
    return entry.startswith("security/")


def manifest_add_data(text: str, entries: list[str]) -> str:
    """Insert ``entries`` into the manifest's ``'data'`` list *text*, preserving
    everything else (comments, layout). Security files go before the first
    non-security entry (Odoo loads data in list order, and views/menus need the
    ACL'd model first); everything else is appended. Missing ``'data'`` key ->
    one is added before the closing brace. Idempotent."""
    d = _manifest_dict(text)
    data_node = None
    for k, v in zip(d.keys, d.values):
        if isinstance(k, ast.Constant) and k.value == "data":
            data_node = v
    if data_node is not None and not isinstance(data_node, ast.List):
        raise GeneratorError("manifest 'data' is not a list literal; edit it by hand")

    existing: list[str] = []
    if data_node is not None:
        for e in data_node.elts:
            existing.append(e.value if isinstance(e, ast.Constant) else "<expr>")
    missing: list[str] = []
    for e in entries:
        if e not in existing and e not in missing:
            missing.append(e)
    if not missing:
        return text

    if data_node is None:
        return _manifest_add_data_key(text, d, missing)

    lst_start = _offset(text, data_node.lineno, data_node.col_offset)
    lst_end = _offset(text, data_node.end_lineno, data_node.end_col_offset)
    key_indent = _line_indent(text, lst_start)

    if not data_node.elts:
        inner = key_indent + "    "
        body = "".join(f"{inner}{e!r},\n" for e in missing)
        return text[:lst_start] + "[\n" + body + key_indent + "]" + text[lst_end:]

    first = data_node.elts[0]
    multiline = first.lineno != data_node.lineno
    insertions: list[tuple[int, str]] = []

    # security first: before the first non-security element
    sec = [e for e in missing if _is_security(e)]
    rest = [e for e in missing if not _is_security(e)]
    first_non_sec = next((el for el, val in zip(data_node.elts, existing)
                          if not _is_security(val)), None)
    if sec and first_non_sec is not None:
        off = _offset(text, first_non_sec.lineno, first_non_sec.col_offset)
        ind = _line_indent(text, off)
        sep = f"\n{ind}" if multiline else " "
        insertions.append((off, "".join(f"{e!r},{sep}" for e in sec)))
    else:
        rest = sec + rest

    if rest:
        last = data_node.elts[-1]
        off = _offset(text, last.end_lineno, last.end_col_offset)
        ind = _line_indent(text, _offset(text, last.lineno, last.col_offset))
        tail = text[off:lst_end]
        has_comma = tail.lstrip().startswith(",")
        if has_comma:
            off = off + tail.index(",") + 1
        lead = "" if has_comma else ","
        if multiline:
            chunk = lead + "".join(f"\n{ind}{e!r}," for e in rest)
        else:
            chunk = (lead + "".join(f" {e!r}," for e in rest)).rstrip(",")
        insertions.append((off, chunk))

    for off, chunk in sorted(insertions, reverse=True):
        text = text[:off] + chunk + text[off:]
    return text


def _manifest_add_data_key(text: str, d: ast.Dict, entries: list[str]) -> str:
    close = _offset(text, d.end_lineno, d.end_col_offset) - 1      # the '}'
    if not d.keys:
        body = "".join(f"        {e!r},\n" for e in entries)
        start = _offset(text, d.lineno, d.col_offset)
        return text[:start] + "{\n    'data': [\n" + body + "    ],\n}" + text[close + 1:]
    first_key = d.keys[0]
    key_indent = _line_indent(text, _offset(text, first_key.lineno, first_key.col_offset))
    last_val = d.values[-1]
    after_last = _offset(text, last_val.end_lineno, last_val.end_col_offset)
    between = text[after_last:close]
    # comma after the last value? (ignore comments/whitespace)
    stripped = re.sub(r"#[^\n]*", "", between).strip()
    if not stripped.startswith(","):
        text = text[:after_last] + "," + text[after_last:]
        close += 1
    line_start = text.rfind("\n", 0, close) + 1
    brace_own_line = text[line_start:close].strip() == ""
    items = "".join(f"{key_indent}    {e!r},\n" for e in entries)
    block = f"{key_indent}'data': [\n{items}{key_indent}],\n"
    if brace_own_line:
        return text[:line_start] + block + text[line_start:]
    return text[:close] + "\n" + block + text[close:]


# --------------------------------------------------------------------------- #
# python package __init__
# --------------------------------------------------------------------------- #
def init_add_import(text: str | None, name: str) -> str:
    """Ensure ``from . import <name>`` in an ``__init__.py`` text. Idempotent."""
    text = text or ""
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        raise GeneratorError(f"cannot parse __init__.py: {e.msg} (line {e.lineno})")
    for node in ast.walk(tree):
        if (isinstance(node, ast.ImportFrom) and node.level == 1 and not node.module
                and any(a.name == name for a in node.names)):
            return text
    if text and not text.endswith("\n"):
        text += "\n"
    return text + f"from . import {name}\n"


# --------------------------------------------------------------------------- #
# ir.model.access.csv
# --------------------------------------------------------------------------- #
def csv_add_row(text: str | None, row: str) -> str:
    """Ensure an access row (by its id column) in the CSV text. Idempotent."""
    if not text or not text.strip():
        text = ACCESS_CSV_HEADER + "\n"
    rid = row.split(",", 1)[0].strip()
    for line in text.splitlines():
        if line.split(",", 1)[0].strip() == rid:
            return text
    if not text.endswith("\n"):
        text += "\n"
    return text + row + "\n"


def access_row(model: str, group: str = "base.group_user") -> str:
    from odx.engine.generators.naming import xmlid
    x = xmlid(model)
    return f"access_{x}_user,{model}.user,model_{x},{group},1,1,1,1"


# --------------------------------------------------------------------------- #
# XML data files
# --------------------------------------------------------------------------- #
XML_HEAD = '<?xml version="1.0" encoding="utf-8"?>\n<odoo>\n'
XML_TAIL = "</odoo>\n"


def xml_has_id(text: str | None, rid: str) -> bool:
    if not text:
        return False
    return re.search(r"""\bid\s*=\s*["']""" + re.escape(rid) + r"""["']""", text) is not None


def xml_append(text: str | None, fragments: list[str]) -> str:
    """Append record fragments to an ``<odoo>`` data file (new doc if None)."""
    body = "\n".join(f.rstrip("\n") + "\n" for f in fragments if f.strip())
    if text is None:
        return XML_HEAD + body + XML_TAIL
    if not body:
        return text
    for closing in ("</odoo>", "</data>"):
        idx = text.rfind(closing)
        if idx != -1:
            head = text[:idx].rstrip()
            return head + "\n\n" + body + text[idx:]
    raise GeneratorError("XML file has no closing </odoo> (or </data>) element")
