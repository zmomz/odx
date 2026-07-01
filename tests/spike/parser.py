"""Spike parser: a .py file -> raw Odoo model/field definitions, via `ast`.

Rough on purpose. It records what it *can* extract statically and, crucially,
flags the patterns it *cannot* (dynamic _name/_inherit, non-literal comodels,
fields built in loops, module-level monkeypatches). Those flags feed the
Phase 0.5 failure catalog — the actual deliverable of the spike.

Never crashes on a file: a syntax error becomes a recorded ParseFailure.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field as dc_field
from pathlib import Path

# Odoo field constructors we recognize (odoo.fields.*).
FIELD_TYPES = {
    "Char", "Text", "Html", "Integer", "Float", "Monetary", "Boolean",
    "Date", "Datetime", "Selection", "Many2one", "One2many", "Many2many",
    "Binary", "Image", "Reference", "Json", "Properties",
    "PropertiesDefinition", "Many2oneReference", "Id",
}
RELATIONAL = {"Many2one", "One2many", "Many2many", "Many2oneReference"}

# Base classes that mark a class as an Odoo model.
MODEL_BASES = {"Model", "AbstractModel", "TransientModel", "BaseModel"}


@dataclass
class RawField:
    name: str
    ftype: str
    comodel: str | None = None          # literal comodel for relational fields
    inverse_name: str | None = None     # one2many inverse (literal)
    dynamic_comodel: bool = False       # relational but comodel not a literal
    inverse_given: bool = False         # an inverse arg was passed at all
    dynamic_inverse: bool = False       # inverse arg passed but not a literal
    attrs: dict[str, object] = dc_field(default_factory=dict)
    line: int = 0

    @property
    def is_computed(self) -> bool:
        return "compute" in self.attrs or "related" in self.attrs


@dataclass
class RawModel:
    module: str
    file: Path
    line: int
    kind: str                            # Model | AbstractModel | TransientModel
    name: str | None = None              # _name literal, if present
    inherit: list[str] = dc_field(default_factory=list)      # resolved literals
    inherit_dynamic: bool = False        # _inherit present but not a literal
    inherits: dict[str, str] = dc_field(default_factory=dict)  # delegation
    name_dynamic: bool = False           # _name present but not a literal
    fields: list[RawField] = dc_field(default_factory=list)
    # dynamic-pattern flags for the catalog:
    has_fields_in_loop: bool = False
    has_setattr_fields: bool = False

    @property
    def effective_name(self) -> str | None:
        """The model this class contributes to.

        - `_name` set  -> that name (a new/redefined model).
        - only `_inherit` (single) -> extends that model in place.
        - `_inherit` list, no `_name` -> extends the first (multi-mixin merge).
        """
        if self.name:
            return self.name
        if self.inherit:
            return self.inherit[0]
        return None


@dataclass
class ParseFailure:
    file: Path
    error: str


@dataclass
class FileResult:
    file: Path
    module: str
    models: list[RawModel] = dc_field(default_factory=list)
    monkeypatch_fields: int = 0          # module-level `Cls.x = fields.Y(...)`
    failure: ParseFailure | None = None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _str_const(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _str_list(node: ast.AST) -> list[str] | None:
    """['a', 'b'] / ('a',) of string constants -> list; else None."""
    if isinstance(node, (ast.List, ast.Tuple)):
        out = []
        for elt in node.elts:
            s = _str_const(elt)
            if s is None:
                return None
            out.append(s)
        return out
    return None


def _is_field_call(node: ast.AST) -> str | None:
    """If `node` is a fields.X(...) or X(...) constructor call, return X."""
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


def _extract_field(name: str, call: ast.Call, ftype: str) -> RawField:
    kw = _kwargs(call)
    rf = RawField(name=name, ftype=ftype, line=call.lineno)

    # comodel: first positional str, or comodel_name=
    comodel_node = None
    if ftype in RELATIONAL:
        if call.args:
            comodel_node = call.args[0]
        elif "comodel_name" in kw:
            comodel_node = kw["comodel_name"]
        if comodel_node is not None:
            lit = _str_const(comodel_node)
            if lit is not None:
                rf.comodel = lit
            else:
                rf.dynamic_comodel = True
        # one2many inverse: second positional or inverse_name=
        if ftype == "One2many":
            inv_node = call.args[1] if len(call.args) > 1 else kw.get("inverse_name")
            if inv_node is not None:
                rf.inverse_given = True
                rf.inverse_name = _str_const(inv_node)
                if rf.inverse_name is None:
                    rf.dynamic_inverse = True

    for key in ("required", "store", "related", "compute", "readonly", "translate"):
        if key in kw:
            v = kw[key]
            rf.attrs[key] = v.value if isinstance(v, ast.Constant) else "<expr>"
    # depends for compute detection lives on the method; here we note compute presence
    return rf


def _class_is_model(node: ast.ClassDef) -> str | None:
    """Return the model kind if this ClassDef is an Odoo model, else None."""
    for base in node.bases:
        attr = base.attr if isinstance(base, ast.Attribute) else (
            base.id if isinstance(base, ast.Name) else None
        )
        if attr in MODEL_BASES:
            # normalise: BaseModel is rare; treat like Model
            return "Model" if attr == "BaseModel" else attr
    return None


def _parse_class(node: ast.ClassDef, module: str, file: Path) -> RawModel:
    rm = RawModel(module=module, file=file, line=node.lineno,
                  kind=_class_is_model(node) or "Model")

    for stmt in node.body:
        # dynamic field creation patterns
        if isinstance(stmt, (ast.For, ast.AsyncFor)):
            # crude: a for-loop in class body often builds fields dynamically
            for sub in ast.walk(stmt):
                if _is_field_call(sub):
                    rm.has_fields_in_loop = True
                    break
            continue

        if isinstance(stmt, ast.Assign):
            # attribute meta / field assignments. Handle chained assignments too,
            # e.g. `_name = _description = 'x'` (common in Odoo test fixtures) and
            # `a = b = fields.Char()`.
            name_targets = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
            for tgt in name_targets:
                val = stmt.value
                if tgt == "_name":
                    s = _str_const(val)
                    if s is not None:
                        rm.name = s
                    else:
                        rm.name_dynamic = True
                    continue
                if tgt == "_inherit":
                    s = _str_const(val)
                    lst = _str_list(val)
                    if s is not None:
                        rm.inherit = [s]
                    elif lst is not None:
                        rm.inherit = lst
                    else:
                        rm.inherit_dynamic = True
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
                # field assignment?
                ft = _is_field_call(val)
                if ft:
                    rm.fields.append(_extract_field(tgt, val, ft))
                    continue

        # setattr(self/cls, 'x', fields.Y(...)) dynamic field
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            fn = stmt.value.func
            if isinstance(fn, ast.Name) and fn.id == "setattr":
                for a in stmt.value.args:
                    if _is_field_call(a):
                        rm.has_setattr_fields = True

    return rm


def parse_file(path: Path, module: str) -> FileResult:
    result = FileResult(file=path, module=module)
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as e:
        result.failure = ParseFailure(path, f"SyntaxError: {e.msg} (line {e.lineno})")
        return result
    except Exception as e:  # noqa: BLE001 - the spike must never crash on a file
        result.failure = ParseFailure(path, f"{type(e).__name__}: {e}")
        return result

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and _class_is_model(node):
            result.models.append(_parse_class(node, module, path))

    # module-level monkeypatch: `SomeClass.field = fields.X(...)`
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Attribute) and _is_field_call(node.value):
                    result.monkeypatch_fields += 1

    return result
