"""Migration (`odx upgrade check|apply --to <version>`, RFC §19 Phase 7).

Per-version delta rulesets are DATA (`RULES`): each rule names the version step
it belongs to, what it matches (a Python AST pattern, an XML element/attribute,
a manifest key) and, when the rewrite is mechanical and safe, a fixer. `check`
reports every rule hit between the project's version and the target as a
Diagnostic at the exact Location; `apply` composes the safe fixers into one
EditPlan (never writes — C3). The differentiator over generic migrators is that
rules run over the RESOLVED project (models, effective views, index), so e.g. a
removed field is reported where it is *used*, not just where it is declared.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from odx.engine.contracts import (
    Diagnostic,
    EditKind,
    EditPlan,
    FileEdit,
    Location,
    Scope,
    Severity,
    UpgradeReport,
)
from odx.engine.generators.base import disk_read


def _major(v: str | None) -> int:
    try:
        return int(str(v).split(".")[0])
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class PyRule:
    """A deprecated/removed Python API: method name defined on a model, a call
    attribute, a decorator, or a field kwarg."""
    since: int                      # first version where it breaks / is deprecated
    code: str
    severity: Severity
    message: str
    method_def: str | None = None   # model method named like this (override of removed API)
    call_attr: str | None = None    # `x.<attr>(` call
    decorator: str | None = None    # `@api.<decorator>`
    field_kwarg: str | None = None  # `fields.X(<kwarg>=...)`
    kwarg_rename: str | None = None # safe fix: rename the kwarg


@dataclass(frozen=True)
class XmlRule:
    since: int
    code: str
    severity: Severity
    message: str
    attribute: str | None = None    # element attribute present (e.g. attrs, states)
    tag: str | None = None          # element tag in a view arch (e.g. tree)
    record_field: tuple[str, str] | None = None   # (record model, field name)


PY_RULES: list[PyRule] = [
    PyRule(17, "upgrade.name-get-removed", Severity.ERROR,
           "`name_get` is no longer called in Odoo 17; implement `_compute_display_name` instead.",
           method_def="name_get"),
    PyRule(17, "upgrade.fields-view-get-removed", Severity.ERROR,
           "`fields_view_get` was replaced by `get_view` / `_get_view` in Odoo 16-17.",
           method_def="fields_view_get"),
    PyRule(17, "upgrade.name-get-call", Severity.WARNING,
           "`.name_get()` is deprecated in Odoo 17; read `display_name` instead.",
           call_attr="name_get"),
    PyRule(16, "upgrade.api-multi", Severity.ERROR,
           "`@api.multi` no longer exists (removed in 13); remove the decorator.",
           decorator="multi"),
    PyRule(16, "upgrade.api-one", Severity.ERROR,
           "`@api.one` no longer exists; iterate over `self` explicitly.", decorator="one"),
    PyRule(17, "upgrade.api-returns", Severity.WARNING,
           "`@api.returns` is deprecated; return the recordset directly.", decorator="returns"),
    PyRule(18, "upgrade.group-operator-renamed", Severity.WARNING,
           "Field attribute `group_operator` was renamed `aggregator` in Odoo 18.",
           field_kwarg="group_operator", kwarg_rename="aggregator"),
    PyRule(18, "upgrade.user-has-groups", Severity.WARNING,
           "`user_has_groups()` is deprecated in Odoo 18; use `self.env.user.has_group()`.",
           call_attr="user_has_groups"),
    PyRule(18, "upgrade.name-search-override", Severity.WARNING,
           "Overriding `_name_search` changed in Odoo 18 (`_search_display_name` is the "
           "new hook); review this override.", method_def="_name_search"),
    PyRule(17, "upgrade.check-access-rights", Severity.HINT,
           "`check_access_rights` is superseded by `check_access` / `has_access` in 18.",
           call_attr="check_access_rights"),
]

XML_RULES: list[XmlRule] = [
    XmlRule(17, "upgrade.attrs-removed", Severity.ERROR,
            "`attrs` is not supported since Odoo 17; use invisible/readonly/required "
            "Python expressions (`odx upgrade apply` converts simple domains).",
            attribute="attrs"),
    XmlRule(17, "upgrade.states-removed", Severity.ERROR,
            "`states` is not supported since Odoo 17; use an `invisible` expression on `state`.",
            attribute="states"),
    XmlRule(18, "upgrade.tree-to-list", Severity.WARNING,
            "`<tree>` views are `<list>` in Odoo 18 (`odx upgrade apply` renames them).",
            tag="tree"),
    XmlRule(18, "upgrade.cron-numbercall-removed", Severity.ERROR,
            "`ir.cron.numbercall` was removed in Odoo 18.", record_field=("ir.cron", "numbercall")),
    XmlRule(18, "upgrade.cron-doall-removed", Severity.ERROR,
            "`ir.cron.doall` was removed in Odoo 18.", record_field=("ir.cron", "doall")),
    XmlRule(17, "upgrade.act-window-view-type", Severity.ERROR,
            "`view_type` on window actions was removed (Odoo 13+).",
            record_field=("ir.actions.act_window", "view_type")),
]


# --------------------------------------------------------------------------- #
# attrs/states -> Odoo 17 python expressions (safe subset)
# --------------------------------------------------------------------------- #
_OPS = {"=": "==", "==": "==", "!=": "!=", "<>": "!=", "<": "<", ">": ">", "<=": "<=",
        ">=": ">=", "in": "in", "not in": "not in"}


def domain_to_expr(domain_src: str) -> str | None:
    """`[('state','=','draft'), ('x','!=',False)]` -> `state == 'draft' and x`.
    Handles implicit AND, '|' / '&' / '!' prefix operators and the operators
    above; returns None when the domain uses anything else (left for a human)."""
    try:
        dom = ast.literal_eval(domain_src)
    except (ValueError, SyntaxError):
        try:
            tree = ast.parse(domain_src, mode="eval")
        except SyntaxError:
            return None
        if not isinstance(tree.body, (ast.List, ast.Tuple)):
            return None
        dom = []
        for elt in tree.body.elts:
            if isinstance(elt, ast.Constant):
                dom.append(elt.value)
            elif isinstance(elt, (ast.Tuple, ast.List)) and len(elt.elts) == 3:
                f, op = elt.elts[0], elt.elts[1]
                if not (isinstance(f, ast.Constant) and isinstance(op, ast.Constant)):
                    return None
                dom.append((f.value, op.value, ("__expr__", ast.unparse(elt.elts[2]))))
            else:
                return None
    if isinstance(dom, tuple) and len(dom) == 3 and isinstance(dom[0], str):
        dom = [dom]
    if not isinstance(dom, (list, tuple)):
        return None
    items = list(dom)

    def leaf(t) -> str | None:
        if not (isinstance(t, (list, tuple)) and len(t) == 3):
            return None
        f, op, v = t
        if not isinstance(f, str) or op not in _OPS:
            return None
        if isinstance(v, tuple) and len(v) == 2 and v[0] == "__expr__":
            val = v[1]
        else:
            val = repr(v)
        if v is False and op in ("=", "=="):
            return f"not {f}"
        if v is False and op in ("!=", "<>"):
            return f
        if v is True and op in ("=", "=="):
            return f
        return f"{f} {_OPS[op]} {val}"

    def parse(i: int) -> tuple[str | None, int]:
        if i >= len(items):
            return None, i
        t = items[i]
        if t in ("|", "&"):
            a, j = parse(i + 1)
            b, k = parse(j)
            if a is None or b is None:
                return None, k
            return f"({a} {'or' if t == '|' else 'and'} {b})", k
        if t == "!":
            a, j = parse(i + 1)
            return (None if a is None else f"not ({a})"), j
        return leaf(t), i + 1

    parts: list[str] = []
    i = 0
    while i < len(items):
        e, i = parse(i)
        if e is None:
            return None
        parts.append(e)
    if not parts:
        return "False"
    out = " and ".join(parts)
    if out.startswith("(") and out.endswith(")") and len(parts) == 1 and out.count("(") == 1:
        out = out[1:-1]
    return out


def attrs_to_attributes(attrs_src: str) -> dict[str, str] | None:
    try:
        tree = ast.parse(attrs_src, mode="eval")
    except SyntaxError:
        return None
    if not isinstance(tree.body, ast.Dict):
        return None
    out = {}
    for k, v in zip(tree.body.keys, tree.body.values):
        if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
            return None
        expr = domain_to_expr(ast.unparse(v))
        if expr is None:
            return None
        out[k.value] = expr
    return out


_START_TAG = re.compile(r"<[A-Za-z_][\w:.-]*(?:\s+[^<>]*?)?/?>", re.S)
_ATTR = re.compile(r"""(?P<sp>\s+)(?P<k>[\w:.-]+)\s*=\s*(?P<q>["'])(?P<v>.*?)(?P=q)""", re.S)


def _escape_attr(v: str, quote: str) -> str:
    v = v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return v.replace('"', "&quot;") if quote == '"' else v.replace("'", "&apos;")


def _convert_tag(tag: str) -> str:
    import html
    attrs = {m.group("k"): m for m in _ATTR.finditer(tag)}
    if "attrs" not in attrs and "states" not in attrs:
        return tag
    new: dict[str, str] = {}
    if "attrs" in attrs:
        conv = attrs_to_attributes(html.unescape(attrs["attrs"].group("v")))
        if conv is None:
            return tag                       # leave for a human
        new.update(conv)
    if "states" in attrs:
        states = [x.strip() for x in html.unescape(attrs["states"].group("v")).split(",") if x.strip()]
        if states:
            st = f"state not in {tuple(states)!r}" if len(states) > 1 else f"state != {states[0]!r}"
            new["invisible"] = f"({new['invisible']}) or {st}" if "invisible" in new else st
    # merge with modifiers already on the tag
    for k in list(new):
        if k in attrs and k not in ("attrs", "states"):
            old = html.unescape(attrs[k].group("v")).strip()
            if old and old not in ("0", "False"):
                new[k] = f"({old}) or ({new[k]})"
    q = '"'
    out = tag
    for k in ("attrs", "states", *new.keys()):
        m = attrs.get(k)
        if m is not None:
            out = out.replace(m.group(0), "", 1)
    close = "/>" if out.rstrip().endswith("/>") else ">"
    body = out.rstrip()[: -len(close)].rstrip()
    extra = "".join(f" {k}={q}{_escape_attr(v, q)}{q}" for k, v in new.items())
    return body + extra + close


def convert_attrs_in_text(text: str) -> str:
    """Rewrite each start tag carrying `attrs=`/`states=` into Odoo 17 modifier
    attributes, merging with modifiers already present (never duplicating an
    attribute). Unconvertible domains are left untouched for a human."""
    return _START_TAG.sub(lambda m: _convert_tag(m.group(0)), text)


_TREE_OPEN = re.compile(r"<tree(?=[\s>/])")
_TREE_CLOSE = re.compile(r"</tree\s*>")
_VIEW_MODE = re.compile(r"(<field\s+name=[\"']view_mode[\"'][^>]*>)([^<]*)(</field>)")


def tree_to_list_in_text(text: str) -> str:
    text = _TREE_OPEN.sub("<list", text)
    text = _TREE_CLOSE.sub("</list>", text)

    def vm(m: re.Match) -> str:
        modes = ",".join("list" if x.strip() == "tree" else x.strip() for x in m.group(2).split(","))
        return m.group(1) + modes + m.group(3)
    return _VIEW_MODE.sub(vm, text)


# --------------------------------------------------------------------------- #
@dataclass
class _Hit:
    diag: Diagnostic
    fixer: Callable[[str], str] | None = None
    file: Path | None = None


def _source_version(api, scope: Scope, declared: str | None) -> str:
    if declared:
        return declared
    for m in api.project_modules():
        ms = api.module(m)
        v = ms.manifest.raw.get("version") if ms else None
        if isinstance(v, str) and v.count(".") >= 4:
            return ".".join(v.split(".")[:2])
    return "17.0"


def _scan(api, target: str, scope: Scope, declared: str | None) -> tuple[str, list[_Hit]]:
    src = _source_version(api, scope, declared)
    lo, hi = _major(src), _major(target)
    hits: list[_Hit] = []

    def active(since: int) -> bool:
        return lo < since <= hi

    py_rules = [r for r in PY_RULES if active(r.since)]
    xml_rules = [r for r in XML_RULES if active(r.since)]
    for name, ms in api.ws.modules.items():
        if not api.in_scope(scope, module=name):
            continue
        # manifest version prefix must name the target series
        mf = ms.path / "__manifest__.py"
        v = ms.manifest.raw.get("version")
        if hi > lo and isinstance(v, str) and v.count(".") >= 4 and _major(v) != hi:
            new_v = f"{target.split('.')[0]}.0." + ".".join(v.split(".")[2:])
            hits.append(_Hit(Diagnostic(
                Severity.WARNING, "upgrade.manifest-version",
                f"`{name}` version {v} must start with {target.split('.')[0]}.0 for Odoo {target}.",
                location=Location(mf, 1, 0, 1, 0)),
                lambda t, v=v, nv=new_v: t.replace(repr(v), repr(nv)).replace(f'"{v}"', f'"{nv}"'),
                mf))
        # python
        for rmod in ms.python:
            if rmod.parse_error or not py_rules:
                continue
            try:
                tree = ast.parse(Path(rmod.file).read_text(encoding="utf-8", errors="replace"))
            except (SyntaxError, OSError, ValueError):
                continue
            for node in ast.walk(tree):
                for r in py_rules:
                    loc = None
                    if r.method_def and isinstance(node, ast.FunctionDef) and node.name == r.method_def:
                        loc = node
                    elif (r.call_attr and isinstance(node, ast.Call)
                          and isinstance(node.func, ast.Attribute) and node.func.attr == r.call_attr):
                        loc = node
                    elif r.decorator and isinstance(node, ast.FunctionDef):
                        for d in node.decorator_list:
                            dn = d.func if isinstance(d, ast.Call) else d
                            if isinstance(dn, ast.Attribute) and dn.attr == r.decorator:
                                loc = d
                    elif (r.field_kwarg and isinstance(node, ast.Call)
                          and any(k.arg == r.field_kwarg for k in node.keywords)):
                        loc = node
                    if loc is None:
                        continue
                    fixer = None
                    if r.kwarg_rename:
                        old, new = r.field_kwarg, r.kwarg_rename
                        fixer = (lambda t, old=old, new=new:
                                 re.sub(rf"\b{old}(\s*=)", rf"{new}\1", t))
                    hits.append(_Hit(Diagnostic(
                        r.severity, r.code, r.message,
                        location=Location(rmod.file, loc.lineno, loc.col_offset,
                                          getattr(loc, "end_lineno", loc.lineno) or loc.lineno,
                                          getattr(loc, "end_col_offset", 0) or 0)),
                        fixer, rmod.file))
        # xml
        for x in ms.xml:
            if x.parse_error or not xml_rules:
                continue
            for r in x.records:
                el = r.element
                if el is None:
                    continue
                for rule in xml_rules:
                    if rule.record_field and r.model == rule.record_field[0] \
                            and rule.record_field[1] in r.fields:
                        hits.append(_Hit(Diagnostic(rule.severity, rule.code, rule.message,
                                                    location=r.fields[rule.record_field[1]].location)))
                for sub in el.iter():
                    if not isinstance(sub.tag, str):
                        continue
                    for rule in xml_rules:
                        hit = ((rule.attribute and sub.get(rule.attribute) is not None
                                and not sub.tag == "attribute")
                               or (rule.tag and sub.tag == rule.tag and r.model == "ir.ui.view"))
                        if rule.attribute == "attrs" and sub.tag == "attribute" and sub.get("name") == "attrs":
                            hit = True
                        if not hit:
                            continue
                        fixer = None
                        if rule.code in ("upgrade.attrs-removed", "upgrade.states-removed"):
                            fixer = convert_attrs_in_text
                        elif rule.code == "upgrade.tree-to-list":
                            fixer = tree_to_list_in_text
                        line = sub.sourceline or 0
                        hits.append(_Hit(Diagnostic(rule.severity, rule.code, rule.message,
                                                    location=Location(x.file, line, 0, line, 0)),
                                         fixer, x.file))
    return src, hits


def upgrade_check(api, target: str, scope: Scope, declared: str | None = None) -> UpgradeReport:
    src, hits = _scan(api, target, scope, declared)
    diags = [h.diag for h in hits]
    stats: dict[str, int] = {}
    for d in diags:
        stats[d.code] = stats.get(d.code, 0) + 1
    stats["fixable"] = sum(1 for h in hits if h.fixer is not None)
    return UpgradeReport(source=src, target=target, diagnostics=diags, stats=stats)


def upgrade_apply(api, target: str, scope: Scope, declared: str | None = None) -> EditPlan:
    src, hits = _scan(api, target, scope, declared)
    per_file: dict[Path, list[Callable[[str], str]]] = {}
    for h in hits:
        if h.fixer is None or h.file is None:
            continue
        fns = per_file.setdefault(h.file, [])
        if h.fixer not in fns or h.fixer.__name__ == "<lambda>":
            if h.fixer in (convert_attrs_in_text, tree_to_list_in_text) and h.fixer in fns:
                continue
            fns.append(h.fixer)
    edits: list[FileEdit] = []
    for f, fns in per_file.items():
        cur = disk_read(f)
        if cur is None:
            continue
        text = cur
        for fn in fns:
            text = fn(text)
        if text != cur:
            edits.append(FileEdit(f, EditKind.REPLACE, None, text))
    n_manual = sum(1 for h in hits if h.fixer is None)
    return EditPlan(edits=edits, summary=(
        f"upgrade {src} -> {target}: {len(edits)} file(s) rewritten"
        + (f"; {n_manual} finding(s) need manual work (`odx upgrade check`)" if n_manual else "")))
