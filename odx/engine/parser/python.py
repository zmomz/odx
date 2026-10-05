"""Python parser: .py files -> RawModule (models + fields), recording Location
(C4) and a Resolution state on every model and field.

**Graduated from `tests/spike/parser.py`** — the *extraction logic* is what the
Phase 0.5 spike validated (100% parse, 99.42% models named). The *output layer*
is new and mandatory from the first commit (Phase 0.6 review):

  - every RawModel/RawField carries a precise `Location` — retrofitting C4 is the
    expensive-to-reverse mistake it warns against;
  - every model/field carries `Resolution` (STATIC / DYNAMIC / PARTIAL) with a
    `notes` reason — the whole point of the Phase 0.5 dividend is that dynamic
    patterns become a first-class value routed to the runtime bridge, not a
    silently-dropped field or a wrong answer.

Discipline preserved from the spike: never crash on a file — a SyntaxError
becomes `RawModule.parse_error`, not an exception.
"""

from __future__ import annotations

import ast
from pathlib import Path

from odx.engine.contracts import Location, Resolution
from odx.engine.model.raw import (
    RawField,
    RawMethod,
    RawModel,
    RawModule,
    RawSqlConstraint,
)

# Odoo field constructors (odoo.fields.*).
FIELD_TYPES = {
    "Char", "Text", "Html", "Integer", "Float", "Monetary", "Boolean",
    "Date", "Datetime", "Selection", "Many2one", "One2many", "Many2many",
    "Binary", "Image", "Reference", "Json", "Properties",
    "PropertiesDefinition", "Many2oneReference", "Id",
}
RELATIONAL = {"Many2one", "One2many", "Many2many", "Many2oneReference"}
MODEL_BASES = {"Model", "AbstractModel", "TransientModel", "BaseModel"}
# Attributes we capture literally so a later override that omits them can inherit
# them (field-override attribute merge, resolver.py `_merge_field`). This spans
# more than comodel: `string`, `help`, `domain`, `groups`, `tracking` are exactly
# the attributes the old last-wins merge silently dropped and that no scoreboard
# metric tracks — so they must be captured to survive an override at all.
_ATTR_KEYS = ("required", "store", "related", "compute", "readonly", "translate",
              "index", "default", "string", "help", "domain", "groups", "tracking",
              "ondelete")
# Expression-valued kwargs kept as source text for the semantic index (§7):
# domains, compute/inverse/search method names, related paths, groups, ...
_EXPR_KEYS = ("domain", "context", "compute", "inverse", "search", "related",
              "currency_field", "selection", "default", "groups", "comodel_name",
              "inverse_name", "relation", "column1", "column2", "string", "store",
              "required", "readonly", "states")


# --------------------------------------------------------------------------- #
# Location helpers
# --------------------------------------------------------------------------- #
def _loc(node: ast.AST, file: Path) -> Location:
    return Location(
        file,
        getattr(node, "lineno", 0),
        getattr(node, "col_offset", 0),
        getattr(node, "end_lineno", getattr(node, "lineno", 0)) or getattr(node, "lineno", 0),
        getattr(node, "end_col_offset", getattr(node, "col_offset", 0)) or 0,
    )


def _loc_classname(node: ast.ClassDef, file: Path) -> Location:
    """Precise range over the class *name* (for editor squiggles), not the whole
    body — `ast` puts ClassDef.lineno on the `class` keyword."""
    name_col = node.col_offset + len("class ")
    return Location(file, node.lineno, name_col, node.lineno, name_col + len(node.name))


# --------------------------------------------------------------------------- #
# small ast helpers (from the validated spike)
# --------------------------------------------------------------------------- #
def _str_const(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _is_true_const(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _str_list(node: ast.AST) -> list[str] | None:
    if isinstance(node, (ast.List, ast.Tuple)):
        out = []
        for elt in node.elts:
            s = _str_const(elt)
            if s is None:
                return None
            out.append(s)
        return out
    return None


def _field_type(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in FIELD_TYPES:
        return func.attr
    if isinstance(func, ast.Name) and func.id in FIELD_TYPES:
        return func.id
    return None


def _kwargs(call: ast.Call) -> dict[str, ast.AST]:
    return {kw.arg: kw.value for kw in call.keywords if kw.arg}


def _class_kind(node: ast.ClassDef) -> str | None:
    for base in node.bases:
        attr = base.attr if isinstance(base, ast.Attribute) else (
            base.id if isinstance(base, ast.Name) else None)
        if attr in MODEL_BASES:
            return "Model" if attr == "BaseModel" else attr
    return None


# --------------------------------------------------------------------------- #
# field extraction — sets comodel/inverse + Resolution + notes
# --------------------------------------------------------------------------- #
def _extract_field(name: str, target: ast.AST, call: ast.Call, ftype: str,
                   file: Path) -> RawField:
    kw = _kwargs(call)
    rf = RawField(name=name, ftype=ftype, location=_loc(target, file))

    for key in _ATTR_KEYS:
        if key in kw:
            v = kw[key]
            rf.attrs[key] = v.value if isinstance(v, ast.Constant) else "<expr>"
    for key in _EXPR_KEYS:
        if key in kw:
            rf.exprs[key] = _unparse(kw[key])
    # Selection(selection_list_or_method, ...) passes the selection positionally
    if ftype in ("Selection", "Reference") and call.args and "selection" not in rf.exprs:
        rf.exprs["selection"] = _unparse(call.args[0])

    if ftype in RELATIONAL:
        # Many2oneReference's first positional arg is `string`, not a comodel
        # (its comodel comes from a sibling `model_field` at runtime).
        positional = call.args if ftype != "Many2oneReference" else []
        comodel_node = positional[0] if positional else kw.get("comodel_name")
        if comodel_node is not None:
            lit = _str_const(comodel_node)
            if lit is not None:
                rf.comodel = lit                         # STATIC
            else:
                rf.resolution = Resolution.DYNAMIC       # comodel from runtime
                rf.notes.append("dynamic-comodel")
        elif "related" in rf.attrs:
            rf.resolution = Resolution.PARTIAL           # comodel inferred via related
            rf.notes.append("comodel-via-related")
        else:
            rf.resolution = Resolution.PARTIAL           # Reference-style / inferred
            rf.notes.append("comodel-absent")

        if ftype == "One2many":
            inv = call.args[1] if len(call.args) > 1 else kw.get("inverse_name")
            if inv is not None:
                lit = _str_const(inv)
                if lit is not None:
                    rf.inverse_name = lit
                else:
                    rf.resolution = Resolution.DYNAMIC
                    rf.notes.append("dynamic-inverse")
    return rf


def _unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - defensive; unparse is total in 3.11
        return "<expr>"


# --------------------------------------------------------------------------- #
# methods — decorators (@api.depends/constrains/onchange/...) + env literals
# --------------------------------------------------------------------------- #
def _decorator_name(dec: ast.AST) -> tuple[str | None, list[ast.AST]]:
    """`@api.depends('a')` -> ("depends", [args]); `@api.model` -> ("model", [])."""
    args: list[ast.AST] = []
    if isinstance(dec, ast.Call):
        args = list(dec.args)
        dec = dec.func
    if isinstance(dec, ast.Attribute):
        return dec.attr, args
    if isinstance(dec, ast.Name):
        return dec.id, args
    return None, args


def _parse_method(fn: ast.FunctionDef | ast.AsyncFunctionDef, file: Path) -> RawMethod:
    name_col = fn.col_offset + len("async def " if isinstance(fn, ast.AsyncFunctionDef) else "def ")
    m = RawMethod(name=fn.name,
                  location=Location(file, fn.lineno, name_col, fn.lineno, name_col + len(fn.name)))
    for dec in fn.decorator_list:
        dname, dargs = _decorator_name(dec)
        if dname is None:
            continue
        lits: list[str] = []
        for a in dargs:
            sc = _str_const(a)
            if sc is not None:
                lits.append(sc)
            else:
                lst = _str_list(a)
                if lst is not None:
                    lits.extend(lst)
        m.decorators[dname] = lits
    for node in ast.walk(fn):
        # self.env['x.y']  (also env['x.y'] on any receiver ending in .env)
        if isinstance(node, ast.Subscript):
            v = node.value
            if isinstance(v, ast.Attribute) and v.attr == "env":
                lit = _str_const(node.slice)
                if lit is not None:
                    m.env_models.append((lit, _loc(node, file)))
        elif isinstance(node, ast.Call):
            f = node.func
            # self.env.ref('module.xmlid')
            if (isinstance(f, ast.Attribute) and f.attr == "ref"
                    and isinstance(f.value, ast.Attribute) and f.value.attr == "env"
                    and node.args):
                lit = _str_const(node.args[0])
                if lit is not None:
                    m.xmlid_refs.append((lit, _loc(node, file)))
            elif isinstance(f, ast.Name) and f.id == "super":
                m.calls_super = True
    return m


def _sql_constraints(val: ast.AST, file: Path) -> list[RawSqlConstraint]:
    out: list[RawSqlConstraint] = []
    if not isinstance(val, (ast.List, ast.Tuple)):
        return out
    for elt in val.elts:
        if isinstance(elt, (ast.Tuple, ast.List)) and len(elt.elts) >= 2:
            name = _str_const(elt.elts[0])
            definition = _str_const(elt.elts[1])
            msg = elt.elts[2] if len(elt.elts) > 2 else None
            if name is None or definition is None:
                continue
            out.append(RawSqlConstraint(
                name=name, definition=definition,
                message=(_str_const(msg) or _unparse(msg)) if msg is not None else "",
                location=_loc(elt, file)))
    return out


# --------------------------------------------------------------------------- #
# class -> RawModel
# --------------------------------------------------------------------------- #
def _parse_class(node: ast.ClassDef, module: str, file: Path) -> RawModel:
    rm = RawModel(name=None, location=_loc_classname(node, file),
                  kind=_class_kind(node) or "Model", class_name=node.name)
    name_dynamic = False

    for stmt in node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            rm.methods.append(_parse_method(stmt, file))
            continue
        if isinstance(stmt, (ast.For, ast.AsyncFor)):
            if any(_field_type(sub) for sub in ast.walk(stmt)):
                rm.notes.append("fields-in-loop")       # some fields not static
            continue

        if isinstance(stmt, ast.Assign):
            name_targets = [t for t in stmt.targets if isinstance(t, ast.Name)]
            for target in name_targets:
                tgt = target.id
                val = stmt.value
                if tgt == "_name":
                    s = _str_const(val)
                    if s is not None:
                        rm.name = s
                    else:
                        name_dynamic = True
                    continue
                if tgt == "_inherit":
                    s, lst = _str_const(val), _str_list(val)
                    if s is not None:
                        rm.inherit = [s]
                    elif lst is not None:
                        rm.inherit = lst
                    else:
                        rm.resolution = Resolution.DYNAMIC
                        rm.notes.append("dynamic-inherit")
                    continue
                if tgt == "_sql_constraints":
                    rm.sql_constraints = _sql_constraints(val, file)
                    continue
                if tgt in ("_order", "_rec_name", "_description"):
                    sv = _str_const(val)
                    if sv is not None:
                        setattr(rm, {"_order": "order", "_rec_name": "rec_name",
                                     "_description": "description"}[tgt], sv)
                    continue
                if tgt == "_inherits":
                    if isinstance(val, ast.Dict):
                        d = {}
                        for k, v in zip(val.keys, val.values):
                            ks, vs = _str_const(k), _str_const(v)
                            if ks and vs:
                                d[ks] = vs
                        rm.inherits = d
                    continue
                ft = _field_type(val)
                if ft:
                    rf = _extract_field(tgt, target, val, ft, file)
                    rm.fields.append(rf)
                    # `Many2one(comodel, delegate=True)` is the modern spelling of
                    # `_inherits = {comodel: field}` (Odoo reflects it into _inherits;
                    # see odoo/models.py "reflect fields with delegate=True"). Model
                    # it as delegation so the delegate's fields resolve onto this
                    # model — without it, an inverse pointing at a delegated field
                    # reads as dangling (a false accusation). Only when the comodel
                    # is a literal; a runtime comodel already made the field DYNAMIC.
                    if (ft == "Many2one" and rf.comodel is not None
                            and _is_true_const(_kwargs(val).get("delegate"))):
                        rm.inherits.setdefault(rf.comodel, tgt)

        elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            fn = stmt.value.func
            if isinstance(fn, ast.Name) and fn.id == "setattr":
                if any(_field_type(a) for a in stmt.value.args):
                    rm.notes.append("setattr-field")

    # roll up the model's Resolution (identity first, then completeness)
    if name_dynamic and not rm.effective_name:
        rm.resolution = Resolution.DYNAMIC
        rm.notes.append("dynamic-name")
    elif name_dynamic:
        rm.notes.append("dynamic-name")                 # identity still known via _inherit
    if rm.resolution is Resolution.STATIC and (
            "fields-in-loop" in rm.notes or "setattr-field" in rm.notes):
        rm.resolution = Resolution.PARTIAL              # model known, fields incomplete
    return rm


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def parse_source(src: str, file: Path, module: str) -> RawModule:
    result = RawModule(file=file, module=module)
    try:
        tree = ast.parse(src, filename=str(file))
    except SyntaxError as e:
        result.parse_error = f"SyntaxError: {e.msg} (line {e.lineno})"
        return result
    except (ValueError, RecursionError) as e:  # never crash on a file
        result.parse_error = f"{type(e).__name__}: {e}"
        return result

    # Model classes: a direct models.Model/AbstractModel/TransientModel base, OR a
    # base that is itself a model class of this file (`class B(A)` where A is a
    # model — e.g. website's WebsitePublishedMultiMixin(WebsitePublishedMixin)).
    kinds: dict[str, str] = {}
    by_class: dict[str, RawModel] = {}
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    changed = True
    while changed:
        changed = False
        for node in classes:
            if node.name in kinds:
                continue
            k = _class_kind(node)
            if k is None:
                for b in node.bases:
                    bn = b.id if isinstance(b, ast.Name) else None
                    if bn and bn in kinds:
                        k = kinds[bn]
                        break
            if k is not None:
                kinds[node.name] = k
                changed = True
    for node in classes:
        if node.name in kinds:
            rm = _parse_class(node, module, file)
            rm.kind = kinds[node.name]
            result.models.append(rm)
            by_class.setdefault(node.name, rm)
    _module_level_fields(tree, by_class, file)
    return result


def _module_level_fields(tree: ast.Module, by_class: dict[str, RawModel], file: Path) -> None:
    """`setattr(IrRule, 'global', global_)` after `global_ = fields.Boolean(...)`
    — how Odoo declares a field whose name is a Python keyword."""
    pending: dict[str, tuple[ast.AST, ast.Call, str]] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                and isinstance(stmt.targets[0], ast.Name):
            ft = _field_type(stmt.value)
            if ft:
                pending[stmt.targets[0].id] = (stmt.targets[0], stmt.value, ft)
        elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            c = stmt.value
            if isinstance(c.func, ast.Name) and c.func.id == "setattr" and len(c.args) == 3:
                cls, name, val = c.args
                fname = _str_const(name)
                if not (isinstance(cls, ast.Name) and cls.id in by_class and fname):
                    continue
                if isinstance(val, ast.Name) and val.id in pending:
                    _tgt, call, ft = pending[val.id]
                elif _field_type(val):
                    call, ft = val, _field_type(val)
                else:
                    continue
                rf = _extract_field(fname, c, call, ft, file)
                by_class[cls.id].fields.append(rf)


def parse_python(path: Path, module: str) -> RawModule:
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        r = RawModule(file=path, module=module)
        r.parse_error = f"{type(e).__name__}: {e}"
        return r
    return parse_source(src, path, module)
