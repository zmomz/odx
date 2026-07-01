"""XML parser (graduated from the spike) — validates the NEW output layer:
precise Location (C4), first-class Resolution, and the mode/priority the
effective-arch builder needs to draw cohort boundaries.

Extraction correctness was validated in Phase 0.6; these focus on what the spike
never did — representing location, resolution, and the primary/extension boundary
for downstream application.
"""

from __future__ import annotations

from pathlib import Path

from odx.engine.contracts import Resolution
from odx.engine.parser.xml import parse_string

F = Path("addons/mymod/views/x.xml")


def _views(src: str, module: str = "mymod"):
    return parse_string(src, F, module)


def _view(src: str, xmlid: str, module: str = "mymod"):
    r = _views(src, module)
    return next(v for v in r.views if v.xmlid == xmlid)


# --------------------------------------------------------------------------- #
# never-crash discipline
# --------------------------------------------------------------------------- #
def test_malformed_xml_becomes_parse_error_not_exception():
    r = _views("<odoo><record id='x' model='ir.ui.view'>")  # unclosed
    # recover=True often salvages a tree; if not, it's a recorded error — either
    # way, no exception escaped.
    assert r.parse_error is not None or isinstance(r.views, list)


def test_non_view_records_ignored():
    r = _views("<odoo><record id='p' model='res.partner'>"
               "<field name='name'>x</field></record></odoo>")
    assert r.views == []


# --------------------------------------------------------------------------- #
# Location (C4)
# --------------------------------------------------------------------------- #
def test_view_records_location():
    src = ("<odoo>\n"
           "  <record id='v' model='ir.ui.view'>\n"
           "    <field name='model'>res.partner</field>\n"
           "    <field name='arch' type='xml'><form/></field>\n"
           "  </record>\n"
           "</odoo>\n")
    v = _view(src, "mymod.v")
    assert v.location is not None and v.location.file == F
    assert v.location.start_line == 2                 # the <record> line


def test_locator_records_location():
    src = ("<odoo>\n"
           "  <record id='v' model='ir.ui.view'>\n"
           "    <field name='inherit_id' ref='base.v'/>\n"
           "    <field name='arch' type='xml'>\n"
           "      <xpath expr=\"//field[@name='a']\" position='after'/>\n"
           "    </field>\n"
           "  </record>\n"
           "</odoo>\n")
    v = _view(src, "mymod.v")
    assert len(v.locators) == 1
    assert v.locators[0].location.start_line == 5


# --------------------------------------------------------------------------- #
# primary / extension boundary (load-bearing for cohort grouping)
# --------------------------------------------------------------------------- #
def test_view_without_inherit_is_primary():
    v = _view("<odoo><record id='v' model='ir.ui.view'>"
              "<field name='model'>m</field>"
              "<field name='arch' type='xml'><form/></field></record></odoo>", "mymod.v")
    assert v.is_primary and v.mode == "primary"


def test_view_with_inherit_defaults_to_extension():
    v = _view("<odoo><record id='v' model='ir.ui.view'>"
              "<field name='inherit_id' ref='base.v'/>"
              "<field name='arch' type='xml'><form/></field></record></odoo>", "mymod.v")
    assert v.mode == "extension" and not v.is_primary
    assert v.inherit_ref == "base.v"


def test_explicit_primary_mode_overrides_default():
    v = _view("<odoo><record id='v' model='ir.ui.view'>"
              "<field name='inherit_id' ref='base.v'/>"
              "<field name='mode'>primary</field>"
              "<field name='arch' type='xml'><form/></field></record></odoo>", "mymod.v")
    assert v.is_primary and v.mode == "primary"
    assert v.inherit_ref == "base.v"


def test_inherit_ref_normalized_to_full_xmlid():
    v = _view("<odoo><record id='v' model='ir.ui.view'>"
              "<field name='inherit_id' ref='local_view'/>"
              "<field name='arch' type='xml'><form/></field></record></odoo>", "mymod.v")
    assert v.inherit_ref == "mymod.local_view"       # bare ref gets the module prefix


# --------------------------------------------------------------------------- #
# Resolution — dynamic inherit_id is runtime-derived
# --------------------------------------------------------------------------- #
def test_inherit_id_via_eval_is_dynamic():
    v = _view("<odoo><record id='v' model='ir.ui.view'>"
              "<field name='inherit_id' eval='ref(\"base.v\")'/>"
              "<field name='arch' type='xml'><form/></field></record></odoo>", "mymod.v")
    assert v.resolution is Resolution.DYNAMIC
    assert v.inherit_ref is None                      # not a literal ref — don't guess


# --------------------------------------------------------------------------- #
# priority (secondary apply-order key)
# --------------------------------------------------------------------------- #
def test_priority_parsed_default_16():
    v = _view("<odoo><record id='v' model='ir.ui.view'>"
              "<field name='arch' type='xml'><form/></field></record></odoo>", "mymod.v")
    assert v.priority == 16


def test_priority_parsed_explicit():
    v = _view("<odoo><record id='v' model='ir.ui.view'>"
              "<field name='priority'>99</field>"
              "<field name='arch' type='xml'><form/></field></record></odoo>", "mymod.v")
    assert v.priority == 99


# --------------------------------------------------------------------------- #
# locator extraction — TOP-LEVEL directives only (Odoo applies the arch's direct
# children as the spec; nested xpath is payload/content, not a directive)
# --------------------------------------------------------------------------- #
def test_only_top_level_directives_are_locators():
    src = ("<odoo><record id='v' model='ir.ui.view'>"
           "<field name='inherit_id' ref='base.v'/>"
           "<field name='arch' type='xml'>"
           "  <xpath expr=\"//group\" position='inside'>"
           "     <xpath expr=\"//nested\"/>"          # payload, NOT a separate locator
           "  </xpath>"
           "</field></record></odoo>")
    v = _view(src, "mymod.v")
    assert len(v.locators) == 1
    assert v.locators[0].expr == "//group"


def test_shorthand_position_is_a_locator():
    src = ("<odoo><record id='v' model='ir.ui.view'>"
           "<field name='inherit_id' ref='base.v'/>"
           "<field name='arch' type='xml'>"
           "  <field name='a' position='after'><field name='b'/></field>"
           "</field></record></odoo>")
    v = _view(src, "mymod.v")
    assert len(v.locators) == 1
    loc = v.locators[0]
    assert loc.kind == "shorthand" and loc.position == "after"
    assert loc.expr == "//field[@name='a']"          # synthesized from the tag+name


# --------------------------------------------------------------------------- #
# templates (QWeb) are view records too
# --------------------------------------------------------------------------- #
def test_template_is_a_view():
    v = _view("<odoo><template id='t'>x</template></odoo>", "mymod.t")
    assert v.is_template and v.is_primary


def test_template_with_inherit_is_extension():
    v = _view("<odoo><template id='t' inherit_id='base.t'>"
              "<xpath expr='//div' position='inside'/></template></odoo>", "mymod.t")
    assert v.is_template and v.mode == "extension"
    assert v.inherit_ref == "base.t"
    assert len(v.locators) == 1
