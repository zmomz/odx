"""Python parser (graduated from the spike) — validates the NEW output layer:
precise Location (C4) and first-class Resolution on every model and field.

Extraction correctness was validated in Phase 0.5; these tests focus on what the
spike never did — representing location and resolution for downstream clients.
"""

from __future__ import annotations

from pathlib import Path

from odx.engine.contracts import Resolution
from odx.engine.parser.python import parse_source

F = Path("addons/mymod/models/x.py")


def _parse(src: str):
    return parse_source(src, F, "mymod")


def _model(src: str, name: str):
    mod = _parse(src)
    return next(m for m in mod.models if m.effective_name == name)


def _field(src: str, model: str, field: str):
    m = _model(src, model)
    return next(f for f in m.fields if f.name == field)


# --------------------------------------------------------------------------- #
# never-crash discipline
# --------------------------------------------------------------------------- #
def test_syntax_error_becomes_parse_error_not_exception():
    mod = _parse("class Broken(models.Model)\n    _name = 'x'\n")  # missing colon
    assert mod.parse_error is not None
    assert "SyntaxError" in mod.parse_error
    assert mod.models == []


def test_non_model_classes_ignored():
    mod = _parse("class Plain:\n    x = 1\n")
    assert mod.models == []


# --------------------------------------------------------------------------- #
# Location (C4) — precise ranges, recorded at parse time
# --------------------------------------------------------------------------- #
def test_model_location_points_at_class_name():
    src = "class Partner(models.Model):\n    _name = 'res.partner'\n"
    m = _model(src, "res.partner")
    assert m.location is not None
    assert m.location.file == F
    assert m.location.start_line == 1
    # range covers exactly the class name token
    assert m.location.start_col == len("class ")
    assert m.location.end_col == len("class ") + len("Partner")


def test_field_location_points_at_field_name():
    src = ("class P(models.Model):\n"
           "    _name = 'res.partner'\n"
           "    ref = fields.Char()\n")
    f = _field(src, "res.partner", "ref")
    assert f.location.start_line == 3
    assert f.location.start_col == 4          # indentation
    assert f.location.end_col == 4 + len("ref")


# --------------------------------------------------------------------------- #
# Resolution — first-class on every field
# --------------------------------------------------------------------------- #
def test_literal_comodel_is_static():
    f = _field("class P(models.Model):\n _name='a'\n p = fields.Many2one('res.partner')\n",
               "a", "p")
    assert f.resolution is Resolution.STATIC
    assert f.comodel == "res.partner"


def test_dynamic_comodel_is_dynamic():
    f = _field("class P(models.Model):\n _name='a'\n p = fields.Many2one(comodel_name=SOME_VAR)\n",
               "a", "p")
    assert f.resolution is Resolution.DYNAMIC
    assert "dynamic-comodel" in f.notes
    assert f.comodel is None


def test_related_comodel_is_partial():
    f = _field("class P(models.Model):\n _name='a'\n"
               " n = fields.Char(related='partner_id.name')\n", "a", "n")
    # Char isn't relational, so related doesn't downgrade it
    assert f.resolution is Resolution.STATIC


def test_relational_related_without_comodel_is_partial():
    f = _field("class P(models.Model):\n _name='a'\n"
               " pid = fields.Many2one(related='order_id.partner_id')\n", "a", "pid")
    assert f.resolution is Resolution.PARTIAL
    assert "comodel-via-related" in f.notes


def test_plain_char_is_static():
    f = _field("class P(models.Model):\n _name='a'\n t = fields.Char()\n", "a", "t")
    assert f.resolution is Resolution.STATIC


def test_computed_one2many_needs_no_inverse():
    f = _field("class P(models.Model):\n _name='a'\n"
               " ls = fields.One2many('a.line', compute='_c')\n", "a", "ls")
    # comodel literal -> STATIC; no inverse required because computed
    assert f.resolution is Resolution.STATIC
    assert f.inverse_name is None


def test_dynamic_inverse_is_dynamic():
    f = _field("class P(models.Model):\n _name='a'\n"
               " ls = fields.One2many('a.line', INV)\n", "a", "ls")
    assert f.resolution is Resolution.DYNAMIC
    assert "dynamic-inverse" in f.notes


# --------------------------------------------------------------------------- #
# Resolution — model identity
# --------------------------------------------------------------------------- #
def test_dynamic_inherit_marks_model_dynamic():
    m = _parse("class P(models.Model):\n _inherit = SOME_LIST\n").models[0]
    assert m.resolution is Resolution.DYNAMIC
    assert "dynamic-inherit" in m.notes


def test_fields_in_loop_marks_model_partial():
    src = ("class P(models.Model):\n"
           "    _name = 'a'\n"
           "    for i in range(3):\n"
           "        x = fields.Char()\n")
    m = _model(src, "a")
    assert m.resolution is Resolution.PARTIAL
    assert "fields-in-loop" in m.notes


# --------------------------------------------------------------------------- #
# the Phase 0.5 bug that must stay fixed: chained `_name = _description = '...'`
# --------------------------------------------------------------------------- #
def test_chained_name_description_assignment():
    src = "class Order(models.Model):\n    _name = _description = 'sale.order'\n"
    m = _model(src, "sale.order")
    assert m.name == "sale.order"
    assert m.resolution is Resolution.STATIC


# --------------------------------------------------------------------------- #
# inheritance shapes
# --------------------------------------------------------------------------- #
def test_inherit_in_place_effective_name():
    m = _parse("class P(models.Model):\n _inherit = 'res.partner'\n").models[0]
    assert m.effective_name == "res.partner"
    assert m.name is None


def test_inherits_delegation_captured():
    src = ("class P(models.Model):\n"
           "    _name = 'a'\n"
           "    _inherits = {'res.partner': 'partner_id'}\n")
    m = _model(src, "a")
    assert m.inherits == {"res.partner": "partner_id"}
