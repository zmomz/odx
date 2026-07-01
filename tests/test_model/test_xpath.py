"""Odoo XPath dialect — the completeness GATE (docs/PHASE3.md constraint 1).

A gap in the dialect does not error; it silently fails to match, producing a
false `view.xpath-target-missing` that blames the user's code for our limitation
(the Phase 0.6 `hasclass` episode). So these tests assert the two hard rules:
  1. the dialect is provably complete (catalog 'implemented' == registered), and
  2. an unimplemented / unknown construct ANNOUNCES itself via
     `unsupported_functions()` — never degrades to a silent no-match.
"""

from __future__ import annotations

from lxml import etree

from odx.engine.model import xpath


# --------------------------------------------------------------------------- #
# rule 1 — provable completeness
# --------------------------------------------------------------------------- #
def test_dialect_is_provably_complete():
    # every 'implemented' catalog entry is actually registered, and vice-versa.
    xpath.register_dialect()
    xpath.assert_complete()


def test_every_catalog_entry_has_a_known_status():
    for name, status in xpath.ODOO_XPATH_FUNCTIONS.items():
        assert status in ("implemented", "unsupported"), name


def test_implemented_functions_are_registered_and_callable():
    # each 'implemented' name must evaluate without lxml raising "unregistered
    # function" — the exact silent-gap failure the gate guards against.
    xpath.register_dialect()
    tree = etree.fromstring("<root><div class='a'/></root>")
    for name in xpath.implemented_functions():
        try:
            tree.xpath(f".//*[{name}('a')]")
        except etree.XPathEvalError as e:            # unregistered -> would raise here
            raise AssertionError(f"{name} marked implemented but not registered: {e}")


def test_hasclass_actually_matches_after_registration():
    xpath.register_dialect()
    tree = etree.fromstring("<root><div class='a b'/><div class='a'/></root>")
    hits = tree.xpath(".//div[hasclass('a', 'b')]")
    assert len(hits) == 1                     # only the div with BOTH classes


# --------------------------------------------------------------------------- #
# rule 2 — an unknown construct announces itself (never a silent no-match)
# --------------------------------------------------------------------------- #
def test_unknown_function_is_flagged_not_silently_ignored():
    assert xpath.unsupported_functions("//div[made_up_fn('x')]") == ["made_up_fn"]


def test_xpath_1_0_builtins_are_not_flagged():
    # the old partial list was missing name/local-name/floor/etc.; a locator using
    # them must NOT be mistaken for a dialect gap (that would false-DYNAMIC it).
    expr = ("//field[name()='x' and local-name()='y' and contains(@n,'z') "
            "and starts-with(@n,'a') and floor(1.2)=1 and last()]")
    assert xpath.unsupported_functions(expr) == []


def test_hasclass_is_known_and_not_flagged():
    assert xpath.unsupported_functions("//div[hasclass('oe_x')]") == []


def test_unsupported_status_entry_is_reported(monkeypatch):
    # if a future dialect fn is catalogued but not yet implemented, it must be
    # surfaced by unsupported_functions() so the applicator marks it DYNAMIC.
    monkeypatch.setitem(xpath.ODOO_XPATH_FUNCTIONS, "hypothetical_fn", "unsupported")
    assert "hypothetical_fn" in xpath.unsupported_functions("//x[hypothetical_fn()]")


def test_empty_expr_flags_nothing():
    assert xpath.unsupported_functions(None) == []
    assert xpath.unsupported_functions("") == []


# --------------------------------------------------------------------------- #
# %(xmlid)d interpolation (view backlog item 3)
# --------------------------------------------------------------------------- #
def test_interpolation_detected():
    assert xpath.has_interpolation("//button[@name='%(mod.act)d']")
    assert not xpath.has_interpolation("//button[@name='action_x']")


def test_interpolation_resolved_with_mapping():
    out = xpath.resolve_interpolation(
        "//button[@name='%(mod.act)d']", {"mod.act": 42}.__getitem__)
    assert out == "//button[@name='42']"
