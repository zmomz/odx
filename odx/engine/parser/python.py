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
from odx.engine.model.raw import RawField, RawModel, RawModule

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

    if ftype in RELATIONAL:
        comodel_node = call.args[0] if call.args else kw.get("comodel_name")
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


# --------------------------------------------------------------------------- #
# class -> RawModel
# --------------------------------------------------------------------------- #
def _parse_class(node: ast.ClassDef, module: str, file: Path) -> RawModel:
    rm = RawModel(name=None, location=_loc_classname(node, file),
                  kind=_class_kind(node) or "Model")
    name_dynamic = False

    for stmt in node.body:
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

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and _class_kind(node):
            result.models.append(_parse_class(node, module, file))
    return result


def parse_python(path: Path, module: str) -> RawModule:
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        r = RawModule(file=path, module=module)
        r.parse_error = f"{type(e).__name__}: {e}"
        return r
    return parse_source(src, path, module)
