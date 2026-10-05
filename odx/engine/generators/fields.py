"""The field-type -> attributes matrix (RFC §16), as data, plus the three things
built on it:

* ``guess_type(name)``   — name-based *suggestions* (the engine never assumes a
  type; a client confirms the guess or prompts);
* ``validate_spec(spec)`` — spec-local validations -> ``Diagnostic`` list
  (engine-backed checks such as "comodel must exist" join in Phase 4);
* ``render_field(spec)`` — the ``name = fields.X(...)`` source, emitting only
  that type's legal kwargs.

Attribute names in the matrix are ``FieldSpec`` attribute names.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from odx.engine.contracts import Diagnostic, FieldSpec, Severity
from odx.engine.generators.naming import humanize, is_identifier, is_model_name


@dataclass(frozen=True)
class FieldTypeRule:
    required: tuple[str, ...] = ()       # must be set on the FieldSpec
    optional: tuple[str, ...] = ()       # legal follow-ups / prompts
    validations: tuple[str, ...] = ()    # engine-enforced rule tags (§16 col. 4)


# Legal for every type: label/help and the compute family (§16 "compute (any base)").
COMMON_ATTRS: tuple[str, ...] = ("string", "help", "compute", "depends", "store")

FIELD_MATRIX: dict[str, FieldTypeRule] = {
    "Char": FieldTypeRule(optional=("size", "required", "default", "translate")),
    "Text": FieldTypeRule(optional=("required", "default", "translate")),
    "Html": FieldTypeRule(optional=("required", "default", "translate")),
    "Integer": FieldTypeRule(optional=("required", "default")),
    "Float": FieldTypeRule(optional=("digits", "required", "default")),
    "Monetary": FieldTypeRule(
        required=("currency_field",),
        optional=("digits", "required", "default"),
        validations=("currency-field-present",),
    ),
    "Boolean": FieldTypeRule(optional=("default",)),
    "Date": FieldTypeRule(optional=("required", "default")),
    "Datetime": FieldTypeRule(optional=("required", "default")),
    "Selection": FieldTypeRule(
        required=("options",),
        optional=("required", "default"),
        validations=("default-in-options",),
    ),
    "Many2one": FieldTypeRule(
        required=("comodel",),
        optional=("ondelete", "required", "default"),
        validations=("comodel-exists",),
    ),
    "One2many": FieldTypeRule(
        required=("comodel", "inverse_name"),
        optional=(),
        validations=("comodel-exists", "inverse-exists"),
    ),
    "Many2many": FieldTypeRule(
        required=("comodel",),
        optional=("relation", "required"),
        validations=("comodel-exists",),
    ),
    "Binary": FieldTypeRule(optional=("required",)),
}

RELATIONAL_TYPES = frozenset({"Many2one", "One2many", "Many2many"})
X2MANY_TYPES = frozenset({"One2many", "Many2many"})
ONDELETE_VALUES = ("set null", "restrict", "cascade")

# Every non-common FieldSpec attribute we know how to check for legality, with the
# value that means "unset".
_SPEC_ATTR_DEFAULTS: dict[str, object] = {
    "comodel": None, "inverse_name": None, "relation": None, "required": False,
    "default": None, "options": (), "currency_field": None, "digits": None,
    "ondelete": None, "translate": False, "size": None,
}


def normalize_type(t: str | None) -> str | None:
    """Case-insensitive lookup: ``many2one`` -> ``Many2one``; unknown -> None."""
    if not t:
        return None
    low = t.strip().lower()
    for k in FIELD_MATRIX:
        if k.lower() == low:
            return k
    return None


# --------------------------------------------------------------------------- #
# name-based guessing (§16): suggest, never assume
# --------------------------------------------------------------------------- #
_MONEY_WORDS = ("amount", "price", "total", "cost", "subtotal")


def guess_type(name: str) -> list[str]:
    """Ordered type suggestions for a field name; the first is the best guess.

    Always non-empty (``["Char"]`` when nothing matches).
    """
    n = name.strip().lower()
    if n.endswith("_ids"):
        return ["One2many", "Many2many"]
    if n.endswith("_id"):
        return ["Many2one"]
    if n.startswith(("is_", "has_")):
        return ["Boolean"]
    if n.endswith("_datetime") or n == "datetime":
        return ["Datetime"]
    if n.endswith("_date") or n == "date":
        return ["Date", "Datetime"]
    if any(w in n.split("_") for w in _MONEY_WORDS):
        return ["Monetary", "Float"]
    return ["Char"]


# --------------------------------------------------------------------------- #
# spec-local validation -> Diagnostics
# --------------------------------------------------------------------------- #
def _d(sev: Severity, code: str, msg: str, spec: FieldSpec) -> Diagnostic:
    return Diagnostic(severity=sev, code=code, message=msg, model=spec.model or None,
                      field=spec.name or None)


def _literal(src: str) -> tuple[bool, object]:
    try:
        return True, ast.literal_eval(src)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return False, None


def validate_spec(spec: FieldSpec) -> list[Diagnostic]:
    """Spec-local validations. ERROR-severity diagnostics mean the plan must not
    be applied; WARNINGs are surfaced but don't block."""
    E, W = Severity.ERROR, Severity.WARNING
    out: list[Diagnostic] = []

    if not is_identifier(spec.name or ""):
        out.append(_d(E, "field.spec-invalid-name",
                      f"'{spec.name}' is not a valid Python identifier; use snake_case "
                      f"letters, digits and underscores.", spec))
    if not is_model_name(spec.model or ""):
        out.append(_d(E, "field.spec-invalid-model",
                      f"'{spec.model}' is not a valid model name (e.g. 'library.book').",
                      spec))

    ftype = normalize_type(spec.type)
    if ftype is None:
        out.append(_d(E, "field.spec-unknown-type",
                      f"Unknown field type '{spec.type}'. Known: "
                      f"{', '.join(FIELD_MATRIX)}.", spec))
        return out
    rule = FIELD_MATRIX[ftype]

    # -- required attributes ------------------------------------------------ #
    if ftype in RELATIONAL_TYPES:
        if not spec.comodel:
            out.append(_d(E, "field.spec-missing-comodel",
                          f"{ftype} '{spec.name}' needs a comodel (--comodel res.partner).",
                          spec))
        elif not is_model_name(spec.comodel):
            out.append(_d(E, "field.spec-invalid-comodel",
                          f"Comodel '{spec.comodel}' is not a valid model name.", spec))
    if ftype == "One2many" and not spec.inverse_name:
        out.append(_d(E, "field.spec-missing-inverse",
                      f"One2many '{spec.name}' needs an inverse Many2one field on "
                      f"'{spec.comodel or '<comodel>'}' (--inverse <field>).", spec))
    elif spec.inverse_name and not is_identifier(spec.inverse_name):
        out.append(_d(E, "field.spec-invalid-inverse",
                      f"Inverse '{spec.inverse_name}' is not a valid field name.", spec))
    if ftype == "Selection" and not spec.options:
        out.append(_d(E, "field.spec-missing-options",
                      f"Selection '{spec.name}' needs options (key:Label pairs).", spec))
    if ftype == "Monetary" and not spec.currency_field:
        out.append(_d(W, "field.monetary-no-currency",
                      f"Monetary '{spec.name}' has no currency_field; Odoo falls back to "
                      f"'currency_id', which must exist on '{spec.model}'.", spec))

    # -- default ------------------------------------------------------------- #
    if spec.default is not None:
        ok, value = _literal(spec.default)
        if not ok:
            try:
                ast.parse(spec.default, mode="eval")
            except SyntaxError:
                out.append(_d(E, "field.spec-invalid-default",
                              f"Default {spec.default!r} is not a Python expression.", spec))
        elif ftype == "Selection" and spec.options:
            keys = [k for k, _ in spec.options]
            if value not in keys:
                out.append(_d(E, "field.spec-invalid-default",
                              f"Default {value!r} is not one of the selection keys "
                              f"{keys}.", spec))

    if ftype == "Many2one" and spec.ondelete and spec.ondelete not in ONDELETE_VALUES:
        out.append(_d(E, "field.spec-invalid-ondelete",
                      f"ondelete must be one of {', '.join(ONDELETE_VALUES)}.", spec))

    # -- compute ----------------------------------------------------------- #
    if spec.compute is not None and not is_identifier(spec.compute):
        out.append(_d(E, "field.spec-invalid-compute",
                      f"Compute method '{spec.compute}' is not a valid identifier.", spec))
    if spec.compute and spec.store and not spec.depends:
        out.append(_d(W, "field.compute-store-no-depends",
                      f"Stored compute '{spec.name}' has no depends: it will never "
                      f"recompute. Add --depends.", spec))
    if spec.depends and not spec.compute:
        out.append(_d(W, "field.spec-depends-without-compute",
                      "depends given without a compute method; it will be ignored.", spec))

    # -- attributes illegal for this type ---------------------------------- #
    legal = set(rule.required) | set(rule.optional)
    for attr, unset in _SPEC_ATTR_DEFAULTS.items():
        if attr in legal:
            continue
        if getattr(spec, attr) not in (unset, None, (), False):
            out.append(_d(W, "field.spec-illegal-attr",
                          f"'{attr}' does not apply to {ftype} fields; it will be "
                          f"ignored.", spec))
    return out


def has_errors(diags: list[Diagnostic]) -> bool:
    return any(d.severity is Severity.ERROR for d in diags)


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
_MAX_LINE = 88


def compute_method_name(spec: FieldSpec) -> str | None:
    return spec.compute or None


def render_field(spec: FieldSpec, indent: str = "") -> str:
    """``name = fields.X(...)`` for ``spec``; only that type's legal kwargs.

    Unknown types render as-is (``fields.<type>``) — validate first. Returns text
    ending in a newline; long calls are broken one argument per line.
    """
    ftype = normalize_type(spec.type) or spec.type
    rule = FIELD_MATRIX.get(ftype, FieldTypeRule())
    legal = set(rule.required) | set(rule.optional) | set(COMMON_ATTRS)
    args: list[str] = []

    if ftype == "Selection":
        opts = ", ".join(f"({k!r}, {v!r})" for k, v in spec.options)
        args.append(f"[{opts}]")
    if ftype in RELATIONAL_TYPES and spec.comodel:
        args.append(repr(spec.comodel))
    if ftype == "One2many" and spec.inverse_name:
        args.append(repr(spec.inverse_name))

    args.append(f"string={(spec.string or humanize(spec.name))!r}")
    if "relation" in legal and spec.relation:
        args.append(f"relation={spec.relation!r}")
    if "currency_field" in legal and spec.currency_field:
        args.append(f"currency_field={spec.currency_field!r}")
    if "digits" in legal and spec.digits:
        args.append(f"digits={spec.digits}" if spec.digits.startswith("(")
                    else f"digits={spec.digits!r}")
    if "size" in legal and spec.size:
        args.append(f"size={int(spec.size)}")
    if "ondelete" in legal and spec.ondelete:
        args.append(f"ondelete={spec.ondelete!r}")
    if "translate" in legal and spec.translate:
        args.append("translate=True")
    if "required" in legal and spec.required:
        args.append("required=True")
    if "default" in legal and spec.default is not None:
        args.append(f"default={spec.default}")
    if spec.compute:
        args.append(f"compute={spec.compute!r}")
        if spec.store:
            args.append("store=True")
    if spec.help:
        args.append(f"help={spec.help!r}")

    head = f"{indent}{spec.name} = fields.{ftype}("
    one = head + ", ".join(args) + ")"
    if len(one) <= _MAX_LINE:
        return one + "\n"
    inner = indent + "    "
    return head + "\n" + "".join(f"{inner}{a},\n" for a in args) + f"{indent})\n"


def render_compute_stub(spec: FieldSpec, indent: str = "") -> str:
    """The ``@api.depends`` + method stub for a computed field ('' if none)."""
    if not spec.compute:
        return ""
    lines = []
    if spec.depends:
        lines.append(f"{indent}@api.depends({', '.join(repr(d) for d in spec.depends)})")
    lines += [
        f"{indent}def {spec.compute}(self):",
        f"{indent}    for record in self:",
        f"{indent}        record.{spec.name} = False  # TODO: compute the value",
    ]
    return "\n".join(lines) + "\n"
