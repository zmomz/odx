"""EffectiveArchBuilder — registry-wide, load-ordered cohort application.

Written tail-first (Phase 0.6 discipline): the cases most likely to make a naive
applicator quietly wrong come first — each is built around WHICH configuration
resolves WHICH way (not an aggregate count), because aggregate match-rate hides
exactly these:

  1. ORDER IS CORRECTNESS — a locator resolvable only when its cohort is applied
     in load order; reversing the order flips it to a genuine dangle (both
     directions asserted).
  2. The resolution UNIT is the whole cohort, not a view's own parents — a locator
     that matches a node added by a SIBLING inheritance (not on its ancestor path).
  3. The primary boundary — a `mode="primary"` view starts its own tree; a
     `replace` in one tree must not clobber another.

Honest state in both directions (RFC §10): a genuine dangle is UNRESOLVED; an
engine limitation (dialect gap / interpolation / dynamic inherit / base outside
corpus) ANNOUNCES itself as DYNAMIC — never a silent no-match, never a fabricated
defect. The builder DISCOVERS state; it emits no Diagnostics (RFC §6 layering).
"""

from __future__ import annotations

from pathlib import Path

from odx.engine.contracts import Resolution
from odx.engine.loadorder import Manifest, ModuleGraph
from odx.engine.model.arch import EffectiveArchBuilder
from odx.engine.parser.xml import parse_string


# --------------------------------------------------------------------------- #
# harness — build a graph + parse per-module XML without touching disk
# --------------------------------------------------------------------------- #
def _graph(depends: dict[str, list[str]]) -> ModuleGraph:
    g = ModuleGraph()
    for name, deps in depends.items():
        g.manifests[name] = Manifest(name=name, path=Path(name), depends=tuple(deps))
    return g


def _build(sources: dict[str, str], depends: dict[str, list[str]] | None = None):
    depends = depends or {m: [] for m in sources}
    views = []
    for mod, src in sources.items():
        views.extend(parse_string(src, Path(f"{mod}/views.xml"), mod).views)
    return EffectiveArchBuilder(_graph(depends)).build(views)


def _rec(xmlid, inherit=None, arch="<form/>", mode=None, priority=None, model="m"):
    ihr = f"<field name='inherit_id' ref='{inherit}'/>" if inherit else ""
    md = f"<field name='mode'>{mode}</field>" if mode else ""
    pr = f"<field name='priority'>{priority}</field>" if priority else ""
    mdl = f"<field name='model'>{model}</field>" if model else ""
    return (f"<odoo><record id='{xmlid.split('.')[-1]}' model='ir.ui.view'>"
            f"{mdl}{ihr}{md}{pr}"
            f"<field name='arch' type='xml'>{arch}</field></record></odoo>")


def _reasons(ev):
    return {u.reason.split(":", 1)[0] for u in ev.unresolved_locators}


# --------------------------------------------------------------------------- #
# TAIL CASE 1 — ORDER IS CORRECTNESS (both directions)
# base in A; B adds node <b>; C targets <b>. Only if C applies after B is it found.
# --------------------------------------------------------------------------- #
_BASE = _rec("A.v", arch="<form><field name='a'/></form>")
_ADDS_B = _rec("B.v2", inherit="A.v",
               arch="<field name='a' position='after'><field name='b'/></field>")
_NEEDS_B = _rec("C.v3", inherit="A.v",
                arch="<xpath expr=\"//field[@name='b']\" position='after'>"
                     "<field name='c'/></xpath>")


def test_cohort_applies_in_load_order_forward():
    eff = _build({"A": _BASE, "B": _ADDS_B, "C": _NEEDS_B},
                 {"A": [], "B": ["A"], "C": ["B"]})     # A -> B -> C
    ev = eff["A.v"]
    assert ev.resolution is Resolution.STATIC
    assert ev.arch.xpath(".//field[@name='c']")         # C found b that B added
    assert ev.unresolved_locators == []
    assert ev.contributors == ["A.v", "B.v2", "C.v3"]


def test_cohort_ordering_reversed_makes_locator_dangle():
    # SAME sources, reversed dependency: C applies BEFORE B, so //field[@name='b']
    # isn't there yet -> a genuine UNRESOLVED dangle (not a silent skip).
    eff = _build({"A": _BASE, "B": _ADDS_B, "C": _NEEDS_B},
                 {"A": [], "C": ["A"], "B": ["C"]})     # A -> C -> B
    ev = eff["A.v"]
    assert not ev.arch.xpath(".//field[@name='c']")
    dangling = [u for u in ev.unresolved_locators
                if u.resolution is Resolution.UNRESOLVED]
    assert len(dangling) == 1
    assert dangling[0].view_xmlid == "C.v3"
    assert dangling[0].reason == "target-not-found"
    assert ev.resolution is Resolution.UNRESOLVED


def test_priority_breaks_ties_within_a_module():
    # Two extensions in the SAME module (same load rank) — only `priority` can
    # order them. The dependent ('a_need') sorts alphabetically BEFORE the adder
    # ('z_add'), so a name/discovery-order applicator would run it first and it
    # would dangle. A lower priority on the adder must win.
    need = ("<record id='a_need' model='ir.ui.view'>"
            "<field name='inherit_id' ref='A.v'/><field name='priority'>20</field>"
            "<field name='arch' type='xml'>"
            "<xpath expr=\"//field[@name='b']\" position='after'>"
            "<field name='c'/></xpath></field></record>")
    add = ("<record id='z_add' model='ir.ui.view'>"
           "<field name='inherit_id' ref='A.v'/><field name='priority'>1</field>"
           "<field name='arch' type='xml'>"
           "<field name='a' position='after'><field name='b'/></field>"
           "</field></record>")
    eff = _build({"A": _BASE, "B": f"<odoo>{need}{add}</odoo>"},
                 {"A": [], "B": ["A"]})
    ev = eff["A.v"]
    assert ev.arch.xpath(".//field[@name='c']")          # adder ran first via priority
    assert ev.resolution is Resolution.STATIC


# --------------------------------------------------------------------------- #
# TAIL CASE 2 — the UNIT is the cohort, not the view's own parents.
# C's target is added by B, a SIBLING inheritance (both inherit A directly). C's
# own ancestor chain is [A] and does not contain B — only full-cohort application
# resolves it. This is precisely what lifted the spike 84.5% -> 97%.
# --------------------------------------------------------------------------- #
def test_locator_resolves_against_sibling_not_ancestor():
    eff = _build({"A": _BASE, "B": _ADDS_B, "C": _NEEDS_B},
                 {"A": [], "B": ["A"], "C": ["A", "B"]})   # C is a sibling of B
    ev = eff["A.v"]
    assert ev.arch.xpath(".//field[@name='c']")            # resolved via sibling B
    assert ev.resolution is Resolution.STATIC


# --------------------------------------------------------------------------- #
# TAIL CASE 3 — the primary boundary. A primary view starts its OWN tree; a
# `replace` there must not clobber the base tree other extensions apply to.
# --------------------------------------------------------------------------- #
def test_primary_view_starts_its_own_tree():
    base = _rec("A.v", arch="<form><field name='a'/><field name='keep'/></form>")
    # a derived PRIMARY that replaces <field name='a'> — only within its own copy
    prim = _rec("B.p", inherit="A.v", mode="primary",
                arch="<field name='a' position='replace'><field name='pa'/></field>")
    # an ordinary extension of the base that still expects <field name='a'>
    ext = _rec("C.e", inherit="A.v",
               arch="<field name='a' position='after'><field name='ax'/></field>")
    eff = _build({"A": base, "B": prim, "C": ext},
                 {"A": [], "B": ["A"], "C": ["A"]})
    base_ev, prim_ev = eff["A.v"], eff["B.p"]
    # base tree kept <field name='a'> (the primary's replace didn't leak into it)
    assert base_ev.arch.xpath(".//field[@name='a']")
    assert base_ev.arch.xpath(".//field[@name='ax']")      # C applied to the base
    assert not base_ev.arch.xpath(".//field[@name='pa']")  # primary's node stayed home
    # the primary's OWN tree has the replacement and inherited the base's <keep>
    assert prim_ev.arch.xpath(".//field[@name='pa']")
    assert not prim_ev.arch.xpath(".//field[@name='a']")
    assert prim_ev.arch.xpath(".//field[@name='keep']")    # seeded from base


# --------------------------------------------------------------------------- #
# honest state — engine limitations ANNOUNCE themselves as DYNAMIC
# --------------------------------------------------------------------------- #
def test_dialect_gap_announced_dynamic_not_silent_no_match():
    base = _rec("A.v", arch="<form><div class='x'/></form>")
    # an unknown extension function -> must be DYNAMIC (announced), NOT a dangle.
    ext = _rec("B.e", inherit="A.v",
               arch="<xpath expr=\"//div[made_up_fn('x')]\" position='inside'>"
                    "<field name='b'/></xpath>")
    eff = _build({"A": base, "B": ext}, {"A": [], "B": ["A"]})
    ev = eff["A.v"]
    assert "dialect-unsupported" in _reasons(ev)
    u = ev.unresolved_locators[0]
    assert u.resolution is Resolution.DYNAMIC           # announced, not UNRESOLVED
    assert ev.resolution is not Resolution.UNRESOLVED   # not a false accusation


def test_hasclass_locator_resolves_not_flagged():
    base = _rec("A.v", arch="<form><div class='oe_x target'/></form>")
    ext = _rec("B.e", inherit="A.v",
               arch="<xpath expr=\"//div[hasclass('oe_x')]\" position='inside'>"
                    "<field name='b'/></xpath>")
    eff = _build({"A": base, "B": ext}, {"A": [], "B": ["A"]})
    ev = eff["A.v"]
    assert ev.resolution is Resolution.STATIC
    assert ev.arch.xpath(".//field[@name='b']")


def test_interpolation_locator_announced_dynamic():
    base = _rec("A.v", arch="<form><button name='b'/></form>")
    ext = _rec("B.e", inherit="A.v",
               arch="<xpath expr=\"//button[@name='%(A.act)d']\" position='after'>"
                    "<field name='x'/></xpath>")
    eff = _build({"A": base, "B": ext}, {"A": [], "B": ["A"]})
    ev = eff["A.v"]
    assert "interpolation-deferred" in _reasons(ev)
    assert ev.unresolved_locators[0].resolution is Resolution.DYNAMIC


def test_inherit_id_via_eval_is_dynamic_view():
    base = _rec("A.v", arch="<form><field name='a'/></form>")
    dyn = ("<odoo><record id='e' model='ir.ui.view'>"
           "<field name='inherit_id' eval='ref(\"A.v\")'/>"
           "<field name='arch' type='xml'>"
           "<field name='a' position='after'><field name='b'/></field>"
           "</field></record></odoo>")
    eff = _build({"A": base, "B": dyn}, {"A": [], "B": ["A"]})
    assert eff["B.e"].resolution is Resolution.DYNAMIC   # routed to runtime bridge


def test_base_outside_corpus_is_dynamic_not_dangling():
    # inherit_id points at a view no module in the corpus defines -> we can't
    # resolve it statically. DYNAMIC (boundary), never a fabricated dangle.
    ext = _rec("B.e", inherit="external.unknown_view",
               arch="<field name='a' position='after'><field name='b'/></field>")
    eff = _build({"B": ext}, {"B": []})
    ev = eff["B.e"]
    assert ev.resolution is Resolution.DYNAMIC
    assert "base-outside-corpus" in _reasons(ev)


# --------------------------------------------------------------------------- #
# QWeb templates — deferred, first-class, never a defect (view backlog item 4)
# --------------------------------------------------------------------------- #
def test_template_effective_view_is_partial_not_static():
    tpl = "<odoo><template id='t'><div>x</div></template></odoo>"
    eff = _build({"A": tpl}, {"A": []})
    assert eff["A.t"].resolution is Resolution.PARTIAL   # t- inheritance not modelled


# --------------------------------------------------------------------------- #
# position semantics — replace / attributes
# --------------------------------------------------------------------------- #
def test_replace_swaps_the_target():
    base = _rec("A.v", arch="<form><field name='a'/></form>")
    ext = _rec("B.e", inherit="A.v",
               arch="<field name='a' position='replace'><field name='z'/></field>")
    ev = _build({"A": base, "B": ext}, {"A": [], "B": ["A"]})["A.v"]
    assert not ev.arch.xpath(".//field[@name='a']")
    assert ev.arch.xpath(".//field[@name='z']")


def test_attributes_position_sets_attribute():
    base = _rec("A.v", arch="<form><field name='a'/></form>")
    ext = _rec("B.e", inherit="A.v",
               arch="<field name='a' position='attributes'>"
                    "<attribute name='invisible'>1</attribute></field>")
    ev = _build({"A": base, "B": ext}, {"A": [], "B": ["A"]})["A.v"]
    node = ev.arch.xpath(".//field[@name='a']")[0]
    assert node.get("invisible") == "1"


# --------------------------------------------------------------------------- #
# layer boundary (RFC §6) — the builder returns data, never Diagnostics
# --------------------------------------------------------------------------- #
def test_builder_emits_no_diagnostics():
    eff = _build({"A": _BASE, "C": _NEEDS_B}, {"A": [], "C": ["A"]})
    builder = EffectiveArchBuilder(_graph({"A": []}))
    assert not hasattr(builder, "diagnostics")
    # the dangling locator is recorded as honest state, not an emitted Diagnostic
    ev = eff["A.v"]
    assert any(u.resolution is Resolution.UNRESOLVED for u in ev.unresolved_locators)
    assert all(type(u).__name__ == "UnresolvedLocator"
               for u in ev.unresolved_locators)


def test_empty_input_builds_nothing():
    assert _build({}, {}) == {}
