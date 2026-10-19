"""Expression readers for the semantic index (RFC §7).

Odoo references fields through *expressions* as much as through declarations:
domains (`[('partner_id.country_id', '=', x)]`), view modifiers
(`invisible="state != 'draft'"`), `@api.depends('order_line.price')`, QWeb
(`t-field="doc.partner_id.name"`), server-action code (`record.write(...)`). This
module turns that source text into field *paths* — pure functions over strings,
no knowledge of models. The index builder resolves the paths against the
resolved models.

Never raises: unparseable text yields no paths (the caller decides whether that
is worth recording), mirroring the parser's never-crash discipline.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

# Names that appear in view modifiers / domain values but are evaluation-context
# variables, not fields of the record being displayed.
CONTEXT_NAMES = frozenset({
    "context", "uid", "user", "parent", "active_id", "active_ids", "active_model",
    "current_company_id", "allowed_company_ids", "company_id_context",
    "True", "False", "None", "true", "false", "time", "datetime", "relativedelta",
    "context_today", "today", "now", "ref", "self", "record", "records", "env",
    "len", "bool", "str", "int", "float", "abs", "max", "min", "sum", "any", "all",
    "set", "list", "dict", "tuple", "isinstance", "hasattr", "getattr", "round",
    "AND", "OR", "Command", "id_parent", "object", "doc", "docs", "o",
    "current_date", "current_datetime", "tz", "company_ids",
})

DOMAIN_OPERATORS = frozenset({
    "=", "!=", "<>", "<", ">", "<=", ">=", "=?", "=like", "=ilike", "like", "not like",
    "ilike", "not ilike", "in", "not in", "child_of", "parent_of", "any", "not any",
})

_ID_PATH = re.compile(r"^[A-Za-z_][\w]*(\.[A-Za-z_][\w]*)*$")
_INLINE = re.compile(r"\{\{(.+?)\}\}", re.S)
_FORMAT = re.compile(r"#\{(.+?)\}", re.S)


def _parse_eval(src: str) -> ast.AST | None:
    try:
        return ast.parse(src.strip(), mode="eval")
    except (SyntaxError, ValueError, RecursionError):
        return None


def _parse_exec(src: str) -> ast.AST | None:
    try:
        return ast.parse(src, mode="exec")
    except (SyntaxError, ValueError, RecursionError):
        try:
            import textwrap
            return ast.parse(textwrap.dedent(src), mode="exec")
        except (SyntaxError, ValueError, RecursionError):
            return None


def literal_str(src: str | None) -> str | None:
    """`"'sale.order'"` (unparsed literal) -> `sale.order`; non-literal -> None."""
    if not src:
        return None
    try:
        v = ast.literal_eval(src)
    except (ValueError, SyntaxError, TypeError, RecursionError, MemoryError):
        return None
    return v if isinstance(v, str) else None


def literal_strs(src: str | None) -> list[str]:
    if not src:
        return []
    try:
        v = ast.literal_eval(src)
    except (ValueError, SyntaxError, TypeError, RecursionError, MemoryError):
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, (list, tuple)):
        return [x for x in v if isinstance(x, str)]
    return []


def is_path(s: str | None) -> bool:
    return bool(s) and _ID_PATH.match(s) is not None


# --------------------------------------------------------------------------- #
# domains
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DomainRefs:
    paths: tuple[str, ...]          # left-hand field paths on the domain's model
    names: tuple[str, ...]          # free names on the right (fields of the *evaluation* record)
    parsed: bool = True


def domain_refs(src: str | None) -> DomainRefs:
    """Field paths in a domain: the first element of every 3-tuple/list leaf,
    plus bare names used as values (in a view these are fields of the record the
    view displays). Domains are Python literals or expressions mixing them."""
    if not src or not src.strip():
        return DomainRefs((), ())
    tree = _parse_eval(src)
    if tree is None:
        return DomainRefs((), (), parsed=False)
    paths: list[str] = []
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Tuple, ast.List)) and len(node.elts) == 3:
            first, op = node.elts[0], node.elts[1]
            if (isinstance(first, ast.Constant) and isinstance(first.value, str)
                    and is_path(first.value)
                    and isinstance(op, ast.Constant) and isinstance(op.value, str)
                    and op.value.lower() in DOMAIN_OPERATORS):
                paths.append(first.value)
                for val in node.elts[2:]:
                    names.extend(_free_names(val))
    return DomainRefs(tuple(dict.fromkeys(paths)), tuple(dict.fromkeys(names)))


def _free_names(node: ast.AST) -> list[str]:
    """Names read in an expression that may be record fields: bare `Name`s that
    are not context variables and not the receiver of a call/attribute chain on
    a context variable (`context.get(...)`, `parent.x`)."""
    out: list[str] = []
    skip: set[int] = set()
    for sub in ast.walk(node):
        # comprehension targets are local
        if isinstance(sub, ast.comprehension):
            for t in ast.walk(sub.target):
                skip.add(id(t))
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            skip.add(id(sub.func))
        if isinstance(sub, ast.Lambda):
            for a in sub.args.args:
                skip.add(id(a))
    local = {t.id for sub in ast.walk(node) if isinstance(sub, ast.comprehension)
             for t in ast.walk(sub.target) if isinstance(t, ast.Name)}
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and id(sub) not in skip:
            if sub.id not in CONTEXT_NAMES and sub.id not in local:
                out.append(sub.id)
    return out


# --------------------------------------------------------------------------- #
# view modifiers (Odoo 17: python expressions; <=16: attrs dict of domains)
# --------------------------------------------------------------------------- #
def modifier_names(src: str | None) -> tuple[list[str], bool]:
    """Field names read by a modifier expression (`invisible`, `readonly`,
    `required`, `column_invisible`, `decoration-*`). Returns (names, parsed)."""
    if src is None:
        return [], True
    s = src.strip()
    if not s or s in ("1", "0", "True", "False", "true", "false"):
        return [], True
    tree = _parse_eval(s)
    if tree is None:
        return [], False
    return list(dict.fromkeys(_free_names(tree))), True


def attrs_refs(src: str | None) -> DomainRefs:
    """Legacy `attrs="{'invisible': [('state','=','draft')]}"` — a dict of
    domains whose left-hand paths are fields of the displayed record."""
    if not src:
        return DomainRefs((), ())
    tree = _parse_eval(src)
    if tree is None or not isinstance(tree.body, ast.Dict):
        return DomainRefs((), (), parsed=tree is not None)
    paths: list[str] = []
    names: list[str] = []
    for v in tree.body.values:
        d = domain_refs(ast.unparse(v))
        paths.extend(d.paths)
        names.extend(d.names)
    return DomainRefs(tuple(dict.fromkeys(paths)), tuple(dict.fromkeys(names)))


def context_group_by(src: str | None) -> list[str]:
    """`{'group_by': 'partner_id'}` / `['a', 'b:month']` -> field names."""
    if not src:
        return []
    tree = _parse_eval(src)
    if tree is None or not isinstance(tree.body, ast.Dict):
        return []
    out: list[str] = []
    for k, v in zip(tree.body.keys, tree.body.values):
        if isinstance(k, ast.Constant) and k.value == "group_by":
            vals = [v] if not isinstance(v, (ast.List, ast.Tuple)) else list(v.elts)
            for x in vals:
                if isinstance(x, ast.Constant) and isinstance(x.value, str):
                    for part in x.value.split(","):
                        name = part.strip().split(":")[0]
                        if is_path(name):
                            out.append(name)
    return out


# --------------------------------------------------------------------------- #
# attribute paths rooted at bound variables (QWeb, server actions, mail
# templates): `doc.partner_id.name` with doc bound to sale.order ->
# ("doc", ["partner_id", "name"])
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class VarPath:
    var: str
    path: tuple[str, ...]
    line: int = 0                    # 1-based line within the parsed source (exec mode)


# methods/attributes of recordsets that are not fields
RECORDSET_API = frozenset({
    "ids", "env", "_name", "_context", "_fields", "browse", "search", "search_count",
    "read", "write", "create", "unlink", "copy", "exists", "ensure_one", "filtered",
    "filtered_domain", "mapped", "sorted", "with_context", "with_user", "with_company",
    "sudo", "name_get", "display_name", "get", "update", "keys", "items", "values",
    "message_post", "raw_value", "value", "_origin", "id", "strftime", "isoformat",
    "replace", "upper", "lower", "strip", "split", "format", "join", "startswith",
    "endswith", "_compute_display_name", "read_group", "_read_group", "toggle_active",
    "action_archive", "action_unarchive", "grouped", "concat", "union", "with_env",
    "invalidate_recordset", "flush_recordset", "modified", "new", "default_get",
    "fields_get", "check_access_rights", "check_access_rule", "has_group",
})


def var_paths(src: str | None, variables: frozenset[str] | set[str],
              mode: str = "eval") -> list[VarPath]:
    """Attribute chains whose root Name is one of `variables`.

    The chain stops at the first call (`doc.partner_id.name_get()` -> path
    `partner_id`, `name_get` dropped since it's called) — the caller resolves
    each segment against fields and stops at the first non-field.
    """
    if not src or not variables:
        return []
    tree = _parse_eval(src) if mode == "eval" else _parse_exec(src)
    if tree is None:
        return []
    out: list[VarPath] = []
    called: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            called.add(id(node.func))
    seen_inner: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or id(node) in seen_inner:
            continue
        chain: list[str] = []
        cur: ast.AST = node
        nodes: list[ast.Attribute] = []
        while isinstance(cur, ast.Attribute):
            nodes.append(cur)
            chain.append(cur.attr)
            cur = cur.value
        if not isinstance(cur, ast.Name) or cur.id not in variables:
            continue
        for n in nodes[1:]:
            seen_inner.add(id(n))
        chain.reverse()
        nodes.reverse()
        # drop a trailing called attribute (method call), keep the field prefix
        if nodes and id(nodes[-1]) in called:
            chain = chain[:-1]
        if chain:
            out.append(VarPath(cur.id, tuple(chain), getattr(node, "lineno", 0)))
    return out


def inline_expressions(text: str | None) -> list[str]:
    """`{{ object.partner_id.name }}` and `#{...}` inline expressions in text."""
    if not text:
        return []
    return [m.strip() for m in _INLINE.findall(text)] + \
           [m.strip() for m in _FORMAT.findall(text)]


def sql_identifiers(definition: str) -> list[str]:
    """Bare identifiers in a SQL constraint definition (`unique(a, b)`,
    `CHECK(qty >= 0)`), minus SQL keywords — candidate column names."""
    words = re.findall(r"[A-Za-z_][\w]*", definition or "")
    kw = {"unique", "check", "primary", "key", "not", "null", "and", "or", "is",
          "exclude", "using", "gist", "with", "where", "in", "foreign", "references",
          "true", "false", "coalesce", "length", "lower", "upper", "deferrable",
          "initially", "deferred", "immediate", "on", "delete", "cascade", "between",
          "abs", "date", "now", "interval", "nulls", "distinct", "like", "ilike"}
    return [w for w in dict.fromkeys(words) if w.lower() not in kw]


_REF_CALL = re.compile(r"""\bref\(\s*['"]([\w.]+)['"]\s*\)""")
_INTERP = re.compile(r"%\(([\w.]+)\)[ds]")


def ref_calls(src: str | None) -> list[str]:
    """xml-ids used as `ref('x')` inside an eval/code attribute."""
    return _REF_CALL.findall(src or "")


def interpolations(src: str | None) -> list[str]:
    """xml-ids used as `%(x)d` / `%(x)s` in data-file attributes."""
    return _INTERP.findall(src or "")


def groups_list(src: str | None) -> list[str]:
    """`groups="base.group_user,!base.group_portal"` -> xml-ids (negation and the
    legacy `-` prefix stripped)."""
    out = []
    for g in (src or "").split(","):
        g = g.strip().lstrip("!-").strip()
        if g:
            out.append(g)
    return out
