"""§16 field-type matrix: guessing, spec validation, rendering."""

from __future__ import annotations

import ast

import pytest

from odx.engine.contracts import FieldSpec, Severity
from odx.engine.generators.fields import (
    FIELD_MATRIX,
    guess_type,
    normalize_type,
    render_compute_stub,
    render_field,
    validate_spec,
)


@pytest.mark.parametrize("name,expected", [
    ("partner_id", "Many2one"),
    ("tag_ids", "One2many"),
    ("is_active", "Boolean"),
    ("has_children", "Boolean"),
    ("start_date", "Date"),
    ("event_datetime", "Datetime"),
    ("amount", "Monetary"),
    ("unit_price", "Monetary"),
    ("amount_total", "Monetary"),
    ("total", "Monetary"),
    ("name", "Char"),
    ("description", "Char"),
])
def test_guess_type_first_suggestion(name, expected):
    assert guess_type(name)[0] == expected


def test_guess_type_x2many_offers_both():
    assert guess_type("line_ids") == ["One2many", "Many2many"]


def test_normalize_type_case_insensitive():
    assert normalize_type("many2one") == "Many2one"
    assert normalize_type("CHAR") == "Char"
    assert normalize_type("nope") is None


def test_matrix_shape():
    assert FIELD_MATRIX["Many2one"].required == ("comodel",)
    assert set(FIELD_MATRIX["One2many"].required) == {"comodel", "inverse_name"}
    assert "options" in FIELD_MATRIX["Selection"].required
    assert "currency_field" in FIELD_MATRIX["Monetary"].required


def _codes(spec):
    return {d.code: d.severity for d in validate_spec(spec)}


def test_valid_spec_has_no_diagnostics():
    assert validate_spec(FieldSpec("library.book", "partner_id", "Many2one",
                                   comodel="res.partner")) == []


def test_many2one_without_comodel_is_error():
    c = _codes(FieldSpec("library.book", "partner_id", "Many2one"))
    assert c["field.spec-missing-comodel"] is Severity.ERROR


def test_one2many_without_inverse_is_error():
    c = _codes(FieldSpec("library.book", "line_ids", "One2many", comodel="library.line"))
    assert c["field.spec-missing-inverse"] is Severity.ERROR


def test_selection_default_must_be_a_key():
    spec = FieldSpec("m.x", "state", "Selection", options=(("a", "A"), ("b", "B")),
                     default="'c'")
    assert _codes(spec)["field.spec-invalid-default"] is Severity.ERROR
    ok = FieldSpec("m.x", "state", "Selection", options=(("a", "A"),), default="'a'")
    assert validate_spec(ok) == []


def test_selection_without_options_is_error():
    assert "field.spec-missing-options" in _codes(FieldSpec("m.x", "state", "Selection"))


def test_monetary_without_currency_is_warning():
    c = _codes(FieldSpec("m.x", "amount", "Monetary"))
    assert c["field.monetary-no-currency"] is Severity.WARNING


def test_stored_compute_without_depends_is_warning():
    c = _codes(FieldSpec("m.x", "total", "Float", compute="_compute_total", store=True))
    assert c["field.compute-store-no-depends"] is Severity.WARNING


@pytest.mark.parametrize("bad", ["1abc", "class", "with-dash", ""])
def test_invalid_identifier(bad):
    assert _codes(FieldSpec("m.x", bad, "Char"))["field.spec-invalid-name"] is Severity.ERROR


def test_unknown_type():
    assert _codes(FieldSpec("m.x", "foo", "Strange"))["field.spec-unknown-type"] is Severity.ERROR


def test_illegal_attr_is_warned():
    c = _codes(FieldSpec("m.x", "name", "Char", comodel="res.partner"))
    assert c["field.spec-illegal-attr"] is Severity.WARNING


def _call(src: str) -> ast.Call:
    node = ast.parse(src).body[0]
    assert isinstance(node, ast.Assign)
    return node.value


def test_render_many2one():
    src = render_field(FieldSpec("m.x", "partner_id", "Many2one", comodel="res.partner",
                                 ondelete="cascade", required=True))
    call = _call(src)
    assert call.func.attr == "Many2one"
    assert call.args[0].value == "res.partner"
    kw = {k.arg for k in call.keywords}
    assert kw == {"string", "ondelete", "required"}


def test_render_only_legal_kwargs():
    # comodel / currency / size on a Boolean must not leak into the source
    src = render_field(FieldSpec("m.x", "is_done", "Boolean", comodel="res.partner",
                                 currency_field="currency_id", size=5, required=True))
    call = _call(src)
    assert not call.args
    assert {k.arg for k in call.keywords} == {"string"}


def test_render_selection_and_one2many():
    sel = _call(render_field(FieldSpec("m.x", "state", "Selection",
                                       options=(("draft", "Draft"),), default="'draft'")))
    assert ast.literal_eval(sel.args[0]) == [("draft", "Draft")]
    o2m = _call(render_field(FieldSpec("m.x", "line_ids", "One2many",
                                       comodel="m.line", inverse_name="x_id")))
    assert [a.value for a in o2m.args] == ["m.line", "x_id"]


def test_render_long_call_wraps_and_parses():
    src = render_field(FieldSpec("m.x", "a_rather_long_field_name_id", "Many2one",
                                 comodel="some.very.long.model.name", required=True,
                                 help="A long help text that pushes the line over"),
                       indent="    ")
    assert src.count("\n") > 1
    ast.parse("class A:\n" + src)


def test_compute_stub():
    stub = render_compute_stub(FieldSpec("m.x", "total", "Float", compute="_compute_total",
                                         depends=("a", "b.c")), "    ")
    assert "@api.depends('a', 'b.c')" in stub
    ast.parse("class A:\n" + stub)
