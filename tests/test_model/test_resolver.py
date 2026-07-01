"""ModelResolver — merge contributions in load order, apply _inherit/_inherits,
and infer `related=` comodels as a worklist fixpoint.

These tests are written *tail-first* (Phase 0.6 discipline): the two cases most
likely to make a naive resolver quietly wrong are the first ones here —

  1. a related chain that walks THROUGH another related field and ACROSS an
     `_inherit` boundary (proves the fixpoint + effective-field inheritance), and
  2. a genuine related cycle (proves cycle is distinguished from undecidable and
     surfaced as a Diagnostic, not silently "best-effort" guessed).

Aggregate match-rate would hide both; only adversarial fixtures catch them.
"""

from __future__ import annotations

from pathlib import Path

from odx.engine.contracts import Resolution
from odx.engine.loadorder import Manifest, ModuleGraph
from odx.engine.model.resolver import ModelResolver
from odx.engine.parser.python import parse_source


# --------------------------------------------------------------------------- #
# harness: build a graph + parse per-module sources without touching disk
# --------------------------------------------------------------------------- #
def _graph(depends: dict[str, list[str]]) -> ModuleGraph:
    g = ModuleGraph()
    for name, deps in depends.items():
        g.manifests[name] = Manifest(name=name, path=Path(name), depends=tuple(deps))
    return g


def _resolve(sources: dict[str, str], depends: dict[str, list[str]] | None = None):
    """sources: module_name -> python source. depends: module -> [deps]."""
    depends = depends or {m: [] for m in sources}
    modules = [parse_source(src, Path(f"{mod}/models.py"), mod)
               for mod, src in sources.items()]
    resolver = ModelResolver(_graph(depends))
    return resolver, resolver.resolve(modules)


# --------------------------------------------------------------------------- #
# TAIL CASE 1 — related-through-related, across an _inherit boundary
# --------------------------------------------------------------------------- #
def test_related_through_related_across_inherit_boundary():
    # base.mixin declares partner_id -> res.partner
    # child (_name='my.model', _inherit='base.mixin') declares:
    #   partner_ref  = Many2one(related='partner_id')          # through an INHERITED field
    #   partner_name_src = Many2one(related='partner_ref.parent_id')  # through ANOTHER related field
    base = ("class Mixin(models.Model):\n"
            "    _name = 'base.mixin'\n"
            "    partner_id = fields.Many2one('res.partner')\n")
    partner = ("class Partner(models.Model):\n"
               "    _name = 'res.partner'\n"
               "    parent_id = fields.Many2one('res.partner')\n")
    child = ("class MyModel(models.Model):\n"
             "    _name = 'my.model'\n"
             "    _inherit = 'base.mixin'\n"
             "    partner_ref = fields.Many2one(related='partner_id')\n"
             "    granny = fields.Many2one(related='partner_ref.parent_id')\n")
    _r, models = _resolve(
        {"base": base, "partner": partner, "child": child},
        {"base": [], "partner": [], "child": ["base", "partner"]},
    )

    my = models["my.model"]
    # partner_ref resolved through the INHERITED partner_id -> res.partner
    pr = my.fields["partner_ref"]
    assert pr.resolution is Resolution.STATIC
    assert pr.info.attributes["comodel"] == "res.partner"
    # granny resolved through partner_ref (itself a related field) -> res.partner
    gr = my.fields["granny"]
    assert gr.resolution is Resolution.STATIC
    assert gr.info.attributes["comodel"] == "res.partner"


def test_fixpoint_resolves_regardless_of_declaration_order():
    # granny is declared BEFORE the related field it depends on — a single
    # top-to-bottom pass would leave it unresolved; the fixpoint must not.
    src = ("class M(models.Model):\n"
           "    _name = 'm'\n"
           "    granny = fields.Many2one(related='partner_ref.parent_id')\n"
           "    partner_ref = fields.Many2one(related='partner_id')\n"
           "    partner_id = fields.Many2one('res.partner')\n"
           "class P(models.Model):\n"
           "    _name = 'res.partner'\n"
           "    parent_id = fields.Many2one('res.partner')\n")
    _r, models = _resolve({"a": src})
    m = models["m"]
    assert m.fields["partner_ref"].info.attributes["comodel"] == "res.partner"
    assert m.fields["granny"].info.attributes["comodel"] == "res.partner"
    assert m.fields["granny"].resolution is Resolution.STATIC


# --------------------------------------------------------------------------- #
# TAIL CASE 2 — a genuine related cycle is recorded as honest state, never a guess.
# The RESOLVER's job ends at UNRESOLVED + a "related-cycle" note; turning that into
# a Diagnostic is the analyzer's job (see test_analyzer_model_resolution.py), so
# discovery and accusation stay in different layers (RFC §6).
# --------------------------------------------------------------------------- #
def test_related_cycle_recorded_as_unresolved_state_not_guessed():
    # a related-through-b, b related-through-a: neither comodel is inferable.
    src = ("class M(models.Model):\n"
           "    _name = 'm'\n"
           "    a = fields.Many2one(related='b.parent_id')\n"
           "    b = fields.Many2one(related='a.parent_id')\n")
    _r, models = _resolve({"mod": src})
    m = models["m"]
    # neither field gets a fabricated comodel
    assert m.fields["a"].info.attributes.get("comodel") is None
    assert m.fields["b"].info.attributes.get("comodel") is None
    # both are honestly UNRESOLVED with the cycle reason recorded for the analyzer
    assert m.fields["a"].resolution is Resolution.UNRESOLVED
    assert m.fields["b"].resolution is Resolution.UNRESOLVED
    assert "related-cycle" in m.fields["a"].notes
    assert "related-cycle" in m.fields["b"].notes


def test_self_related_cycle():
    src = ("class M(models.Model):\n"
           "    _name = 'm'\n"
           "    a = fields.Many2one(related='a.parent_id')\n")
    _r, models = _resolve({"mod": src})
    a = models["m"].fields["a"]
    assert a.resolution is Resolution.UNRESOLVED
    assert "related-cycle" in a.notes


def test_resolver_emits_no_diagnostics_itself():
    # the resolver discovers, it does not accuse (RFC §6): no Diagnostic surface.
    src = ("class M(models.Model):\n"
           "    _name = 'm'\n"
           "    a = fields.Many2one(related='a.parent_id')\n")
    r, _models = _resolve({"mod": src})
    assert not hasattr(r, "diagnostics")


# --------------------------------------------------------------------------- #
# the three honest terminal outcomes must stay distinct
# --------------------------------------------------------------------------- #
def test_related_bottoming_in_dynamic_stays_dynamic_not_guessed():
    # partner_id's comodel is runtime-derived; a chain through it is DYNAMIC,
    # routed to the runtime bridge — not forced to a plausible-but-wrong comodel.
    src = ("class M(models.Model):\n"
           "    _name = 'm'\n"
           "    partner_id = fields.Many2one(comodel_name=SOME_VAR)\n"
           "    ref = fields.Many2one(related='partner_id')\n")
    _r, models = _resolve({"mod": src})
    ref = models["m"].fields["ref"]
    assert ref.resolution is Resolution.DYNAMIC
    assert ref.info.attributes.get("comodel") is None


def test_related_broken_path_is_unresolved():
    src = ("class M(models.Model):\n"
           "    _name = 'm'\n"
           "    ref = fields.Many2one(related='does_not_exist.name')\n")
    _r, models = _resolve({"mod": src})
    assert models["m"].fields["ref"].resolution is Resolution.UNRESOLVED


def test_related_hop_through_nonrelational_is_unresolved():
    # first hop lands on a Char — you can't step into a scalar
    src = ("class M(models.Model):\n"
           "    _name = 'm'\n"
           "    label = fields.Char()\n"
           "    ref = fields.Many2one(related='label.whatever')\n")
    _r, models = _resolve({"mod": src})
    assert models["m"].fields["ref"].resolution is Resolution.UNRESOLVED


# --------------------------------------------------------------------------- #
# ordering IS correctness — the last module in load order wins
# --------------------------------------------------------------------------- #
def test_field_override_follows_load_order_not_discovery_order():
    # 'late' depends on 'early' and redefines the field; even though we hand the
    # resolver 'late' FIRST, load order must let early<late so late wins.
    early = ("class P(models.Model):\n"
             "    _inherit = 'res.partner'\n"
             "    tag = fields.Char()\n")
    late = ("class P(models.Model):\n"
            "    _inherit = 'res.partner'\n"
            "    tag = fields.Text()\n")
    _r, models = _resolve(
        {"late": late, "early": early},               # discovery order: late first
        {"early": [], "late": ["early"]},             # load order: early -> late
    )
    assert models["res.partner"].fields["tag"].info.type == "Text"


def test_extension_in_place_merges_into_same_model():
    a = ("class P(models.Model):\n"
         "    _name = 'res.partner'\n"
         "    a = fields.Char()\n")
    b = ("class P(models.Model):\n"
         "    _inherit = 'res.partner'\n"
         "    b = fields.Char()\n")
    _r, models = _resolve({"a": a, "b": b}, {"a": [], "b": ["a"]})
    partner = models["res.partner"]
    assert set(partner.fields) == {"a", "b"}
    assert "res.partner" not in partner.inherit_chain    # self-inherit is not a parent


# --------------------------------------------------------------------------- #
# inheritance wiring: prototype (_inherit) vs delegation (_inherits)
# --------------------------------------------------------------------------- #
def test_prototype_inheritance_exposes_parent_fields():
    src = ("class Base(models.Model):\n"
           "    _name = 'base.mixin'\n"
           "    shared = fields.Char()\n"
           "class Child(models.Model):\n"
           "    _name = 'my.model'\n"
           "    _inherit = 'base.mixin'\n"
           "    own = fields.Char()\n")
    _r, models = _resolve({"a": src})
    child = models["my.model"]
    assert child.inherit_chain == ["base.mixin"]
    assert set(child.fields) == {"shared", "own"}
    # provenance records the inheritance relationship
    assert child.fields["shared"].provenance.via == "_inherit"
    assert child.fields["own"].provenance.via == "own"


def test_delegation_inheritance_exposes_delegate_fields():
    src = ("class Partner(models.Model):\n"
           "    _name = 'res.partner'\n"
           "    name = fields.Char()\n"
           "class User(models.Model):\n"
           "    _name = 'res.users'\n"
           "    _inherits = {'res.partner': 'partner_id'}\n"
           "    login = fields.Char()\n")
    _r, models = _resolve({"a": src})
    user = models["res.users"]
    assert user.delegates == ["res.partner"]
    assert user.fields["name"].provenance.via == "_inherits"


# --------------------------------------------------------------------------- #
# model resolution rollup — honest, not alarmist
# --------------------------------------------------------------------------- #
def test_dynamic_field_does_not_knock_model_off_static():
    src = ("class M(models.Model):\n"
           "    _name = 'm'\n"
           "    ok = fields.Char()\n"
           "    dyn = fields.Many2one(comodel_name=SOME_VAR)\n")
    _r, models = _resolve({"mod": src})
    # the model itself is well-known; only the one field is DYNAMIC
    assert models["m"].resolution is Resolution.STATIC
    assert models["m"].fields["dyn"].resolution is Resolution.DYNAMIC


def test_unresolved_field_makes_model_partial():
    src = ("class M(models.Model):\n"
           "    _name = 'm'\n"
           "    ref = fields.Many2one(related='does_not_exist.x')\n")
    _r, models = _resolve({"mod": src})
    assert models["m"].resolution is Resolution.PARTIAL


def test_dynamic_identity_dominates():
    src = "class M(models.Model):\n    _inherit = SOME_LIST\n"
    _r, models = _resolve({"mod": src})
    assert models == {} or all(m.resolution is Resolution.DYNAMIC
                               for m in models.values())


# --------------------------------------------------------------------------- #
# never-crash / empty inputs
# --------------------------------------------------------------------------- #
def test_empty_corpus_resolves_to_nothing():
    _r, models = _resolve({})
    assert models == {}


def test_literal_comodel_untouched_by_worklist():
    src = ("class M(models.Model):\n"
           "    _name = 'm'\n"
           "    p = fields.Many2one('res.partner')\n")
    _r, models = _resolve({"mod": src})
    assert models["m"].fields["p"].resolution is Resolution.STATIC
    assert models["m"].fields["p"].info.attributes["comodel"] == "res.partner"


# --------------------------------------------------------------------------- #
# FIELD-OVERRIDE ATTRIBUTE MERGE (Phase 0.5 backlog item 2).
#
# Odoo re-declaring a field is an ATTRIBUTE MERGE, not a wholesale replace: an
# attribute the override omits is inherited from the field it overrides. The old
# last-wins merge silently dropped every unrestated attribute. `comodel` is only
# the visible symptom (the scoreboard tracks it); the same bug dropped `help`,
# `domain`, `string`, `required`, ... which NO metric tracks. So these fixtures
# assert survival of the *untracked* attributes as much as comodel — the aggregate
# would pass while a specific shape silently drops a specific attribute, which is
# the production failure mode (task requirement #1/#2).
# --------------------------------------------------------------------------- #
def test_same_model_override_preserves_untracked_attributes():
    # A LATER same-model contribution refines ONLY `string`. comodel is tracked;
    # help and domain are tracked by nothing — assert all three survive.
    base = (
        "class P(models.Model):\n"
        "    _name = 'res.partner'\n"
        "    parent_id = fields.Many2one('res.partner', help='the parent', "
        "domain=\"[('is_company', '=', True)]\")\n"
    )
    override = (
        "class P(models.Model):\n"
        "    _inherit = 'res.partner'\n"
        "    parent_id = fields.Many2one(string='Parent')\n"
    )
    _r, models = _resolve({"base": base, "ext": override},
                          {"base": [], "ext": ["base"]})
    f = models["res.partner"].fields["parent_id"]
    assert f.resolution is Resolution.STATIC
    assert f.info.attributes["comodel"] == "res.partner"                 # tracked
    assert f.info.attributes["help"] == "the parent"                     # UNtracked, survives
    assert f.info.attributes["domain"] == "[('is_company', '=', True)]"  # UNtracked, survives
    assert f.info.attributes["string"] == "Parent"                       # override's own wins


def test_override_of_inherited_field_merges_comodel_and_help():
    # sale.order overrides utm.mixin.campaign_id, restating only string/tracking.
    mixin = ("class Mixin(models.Model):\n"
             "    _name = 'utm.mixin'\n"
             "    campaign_id = fields.Many2one('utm.campaign', help='utm campaign')\n")
    child = ("class Sale(models.Model):\n"
             "    _name = 'sale.order'\n"
             "    _inherit = 'utm.mixin'\n"
             "    campaign_id = fields.Many2one(string='Campaign', tracking=True)\n")
    _r, models = _resolve({"utm": mixin, "sale": child},
                          {"utm": [], "sale": ["utm"]})
    f = models["sale.order"].fields["campaign_id"]
    assert f.resolution is Resolution.STATIC
    assert f.info.attributes["comodel"] == "utm.campaign"   # inherited, not restated
    assert f.info.attributes["help"] == "utm campaign"      # inherited untracked attr
    assert f.info.attributes["string"] == "Campaign"        # override wins
    assert f.info.attributes["tracking"] is True
    assert f.provenance.via == "own"                        # it IS declared here (as an override)


def test_override_of_delegated_field_merges_comodel():
    # A delegated (`_inherits`) field overridden without restating comodel.
    partner = ("class Partner(models.Model):\n"
               "    _name = 'res.partner'\n"
               "    company_id = fields.Many2one('res.company', help='the company')\n")
    users = ("class User(models.Model):\n"
             "    _name = 'res.users'\n"
             "    _inherits = {'res.partner': 'partner_id'}\n"
             "    partner_id = fields.Many2one('res.partner', required=True)\n"
             "    company_id = fields.Many2one(string='Company')\n")
    _r, models = _resolve({"a": partner, "b": users}, {"a": [], "b": ["a"]})
    f = models["res.users"].fields["company_id"]
    assert f.resolution is Resolution.STATIC
    assert f.info.attributes["comodel"] == "res.company"    # from the delegate target
    assert f.info.attributes["help"] == "the company"
    assert f.info.attributes["string"] == "Company"


def test_multi_level_override_chain_flows_comodel_transitively():
    # Mirrors the live corpus case: alias.optional (comodel) -> alias.mixin
    # (refines required) -> hr.job (refines help). Neither middle nor leaf restates
    # comodel; it must flow TWO hops. Each level's own added attribute survives.
    a = ("class A(models.Model):\n"
         "    _name = 'alias.optional'\n"
         "    alias_id = fields.Many2one('mail.alias', domain=\"[('x', '=', 1)]\")\n")
    b = ("class B(models.Model):\n"
         "    _name = 'alias.mixin'\n"
         "    _inherit = 'alias.optional'\n"
         "    alias_id = fields.Many2one(required=True)\n")
    c = ("class C(models.Model):\n"
         "    _name = 'hr.job'\n"
         "    _inherit = 'alias.mixin'\n"
         "    alias_id = fields.Many2one(help='job alias')\n")
    _r, models = _resolve({"a": a, "b": b, "c": c},
                          {"a": [], "b": ["a"], "c": ["b"]})
    f = models["hr.job"].fields["alias_id"]
    assert f.resolution is Resolution.STATIC
    assert f.info.attributes["comodel"] == "mail.alias"     # flowed two hops
    assert f.info.attributes["domain"] == "[('x', '=', 1)]"  # from A
    assert f.info.attributes["required"] is True            # from B
    assert f.info.attributes["help"] == "job alias"         # from C
    # the intermediate mixin resolved too (proves parent-first, not just the leaf)
    assert models["alias.mixin"].fields["alias_id"].info.attributes["comodel"] == "mail.alias"


def test_two_modules_refine_same_inherited_field_union_of_attrs():
    # Two extensions of base.mixin each refine `ref` differently; neither restates
    # comodel. The union of attrs survives, and a conflicting attr follows LOAD
    # ORDER (base -> a -> b), not discovery order.
    base = ("class Base(models.Model):\n"
            "    _name = 'base.mixin'\n"
            "    ref = fields.Many2one('res.partner', help='base help')\n")
    mod_a = ("class A(models.Model):\n"
             "    _inherit = 'base.mixin'\n"
             "    ref = fields.Many2one(string='A string', help='A help')\n")
    mod_b = ("class B(models.Model):\n"
             "    _inherit = 'base.mixin'\n"
             "    ref = fields.Many2one(tracking=True)\n")
    _r, models = _resolve(
        {"base": base, "b": mod_b, "a": mod_a},          # discovery order b before a
        {"base": [], "a": ["base"], "b": ["a"]},         # load order base -> a -> b
    )
    f = models["base.mixin"].fields["ref"]
    assert f.info.attributes["comodel"] == "res.partner"  # from base, never restated
    assert f.info.attributes["string"] == "A string"      # only A set it -> survives
    assert f.info.attributes["tracking"] is True          # only B set it -> survives
    assert f.info.attributes["help"] == "A help"          # A refines base's help


def test_conflicting_attr_winner_follows_load_order_both_directions():
    # The ordering dependency made explicit (requirement #3): the SAME sources with
    # reversed load order flip which override wins the conflicting attr. If merge
    # order and load order ever diverge, this is what catches it.
    base = ("class Base(models.Model):\n"
            "    _name = 'base.mixin'\n"
            "    ref = fields.Many2one('res.partner')\n")
    early = ("class E(models.Model):\n"
             "    _inherit = 'base.mixin'\n"
             "    ref = fields.Many2one(string='early')\n")
    late = ("class L(models.Model):\n"
            "    _inherit = 'base.mixin'\n"
            "    ref = fields.Many2one(string='late')\n")
    _r, m1 = _resolve({"base": base, "early": early, "late": late},
                      {"base": [], "early": ["base"], "late": ["early"]})
    assert m1["base.mixin"].fields["ref"].info.attributes["string"] == "late"
    # reverse the dependency edge: base -> late -> early ; now early wins
    _r, m2 = _resolve({"base": base, "early": early, "late": late},
                      {"base": [], "late": ["base"], "early": ["late"]})
    assert m2["base.mixin"].fields["ref"].info.attributes["string"] == "early"
    # comodel survives regardless of order (neither override restates it)
    assert m2["base.mixin"].fields["ref"].info.attributes["comodel"] == "res.partner"


def test_override_dropping_related_restores_it_then_fixpoint_resolves():
    # An override that refines `string` only, dropping `related=`. Last-wins would
    # leave it comodel-absent forever; merge restores the related path and the
    # worklist fixpoint then infers the comodel. (Live: rescued 11 corpus fields
    # the old resolver left UNRESOLVED — the merge x fixpoint interaction.)
    base = ("class M(models.Model):\n"
            "    _name = 'm'\n"
            "    order_id = fields.Many2one('sale.order')\n"
            "    partner_id = fields.Many2one(related='order_id.partner_id')\n")
    parent = ("class Sale(models.Model):\n"
              "    _name = 'sale.order'\n"
              "    partner_id = fields.Many2one('res.partner')\n")
    override = ("class M(models.Model):\n"
                "    _inherit = 'm'\n"
                "    partner_id = fields.Many2one(string='Customer')\n")
    _r, models = _resolve({"a": base, "sale": parent, "ext": override},
                          {"a": [], "sale": [], "ext": ["a", "sale"]})
    f = models["m"].fields["partner_id"]
    assert f.resolution is Resolution.STATIC
    assert f.info.attributes["comodel"] == "res.partner"
    assert f.info.attributes["string"] == "Customer"


def test_override_declared_attribute_beats_base():
    # Precedence: when BOTH declare an attribute, the override wins; the base only
    # fills gaps. (Guards against an inverted merge that lets the base clobber.)
    base = ("class M(models.Model):\n"
            "    _name = 'm'\n"
            "    x = fields.Many2one('a.model', required=True, help='base')\n")
    override = ("class M(models.Model):\n"
                "    _inherit = 'm'\n"
                "    x = fields.Many2one('b.model', required=False, help='override')\n")
    _r, models = _resolve({"a": base, "b": override}, {"a": [], "b": ["a"]})
    f = models["m"].fields["x"]
    assert f.info.attributes["comodel"] == "b.model"    # override's own comodel wins
    assert f.info.attributes["required"] is False       # override wins
    assert f.info.attributes["help"] == "override"      # override wins


def test_many2one_reference_stays_dynamic_not_fabricated():
    # The 6 genuine Many2oneReference (res_id/trg_field_ref): comodel is chosen at
    # runtime from a sibling res_model field. DYNAMIC by nature (requirement #4) —
    # marked, never forced to a plausible-but-wrong comodel, in EITHER direction.
    src = ("class A(models.Model):\n"
           "    _name = 'mail.activity'\n"
           "    res_model = fields.Char()\n"
           "    res_id = fields.Many2oneReference(model_field='res_model')\n")
    _r, models = _resolve({"mail": src})
    f = models["mail.activity"].fields["res_id"]
    assert f.resolution is Resolution.DYNAMIC
    assert f.info.attributes.get("comodel") is None
    assert "comodel-runtime-ref" in f.notes


# --------------------------------------------------------------------------- #
# ONE2MANY INVERSE DISCOVERY (Phase 0.5 backlog item 3).
#
# A One2many names its comodel and an `inverse_name` (the Many2one on the comodel
# pointing back). The resolver verifies that inverse against the comodel's
# EFFECTIVE fields — the inverse Many2one is frequently inherited or delegated
# onto the comodel, not declared there (18 such live corpus sites). These fixtures
# are built around WHICH configuration resolves WHICH way, not aggregate count:
# each is a distinct shape the confirm-live-state pass surfaced (or that a naive
# raw-own-fields lookup would get wrong). Honest in both directions — the sole
# provable defect (a truly-absent inverse) is UNRESOLVED; everything undecidable
# is noted, never fabricated into a defect.
# --------------------------------------------------------------------------- #
def test_inverse_on_own_field_of_comodel_resolves():
    # the common case (599 live): inverse names a Many2one declared ON the comodel.
    src = ("class Order(models.Model):\n"
           "    _name = 'sale.order'\n"
           "    line_ids = fields.One2many('sale.order.line', 'order_id')\n"
           "class Line(models.Model):\n"
           "    _name = 'sale.order.line'\n"
           "    order_id = fields.Many2one('sale.order')\n")
    _r, models = _resolve({"a": src})
    f = models["sale.order"].fields["line_ids"]
    assert f.resolution is Resolution.STATIC
    assert "inverse-resolved" in f.notes


def test_inverse_reachable_only_through_inherit_on_comodel():
    # requirement #2 (14 live): the inverse Many2one is INHERITED onto the comodel
    # via `_inherit`, not declared on it. A lookup over the comodel's RAW own
    # fields would miss it and cry dangling; the EFFECTIVE closure finds it.
    mixin = ("class Mixin(models.Model):\n"
             "    _name = 'order.mixin'\n"
             "    order_id = fields.Many2one('sale.order')\n")
    line = ("class Line(models.Model):\n"
            "    _name = 'sale.order.line'\n"
            "    _inherit = 'order.mixin'\n"
            "    name = fields.Char()\n")
    order = ("class Order(models.Model):\n"
             "    _name = 'sale.order'\n"
             "    line_ids = fields.One2many('sale.order.line', 'order_id')\n")
    _r, models = _resolve({"mixin": mixin, "line": line, "order": order},
                          {"mixin": [], "line": ["mixin"], "order": ["line"]})
    f = models["sale.order"].fields["line_ids"]
    assert f.resolution is Resolution.STATIC
    assert "inverse-resolved" in f.notes
    assert "inverse-dangling" not in f.notes


def test_inverse_reachable_only_through_delegation_on_comodel():
    # the live `test.inherit.daughter` shape, distilled: the inverse Many2one is a
    # DELEGATED field on the comodel (`delegate=True` -> `_inherits`). Without
    # modelling delegation this reads as dangling — the exact false accusation the
    # confirm-live-state pass caught. The comodel delegates to a mixin that (via an
    # extension) carries the back-pointer.
    mixin = ("class Mixin(models.Model):\n"
             "    _name = 'daughter.mixin'\n"
             "    name = fields.Char()\n")
    mixin_ext = ("class MixinExt(models.Model):\n"
                 "    _inherit = 'daughter.mixin'\n"
                 "    parent_id = fields.Many2one('res.partner')\n")
    daughter = ("class Daughter(models.Model):\n"
                "    _name = 'test.daughter'\n"
                "    template_id = fields.Many2one('daughter.mixin', delegate=True, required=True)\n")
    partner = ("class Partner(models.Model):\n"
               "    _inherit = 'res.partner'\n"
               "    daughter_ids = fields.One2many('test.daughter', 'parent_id')\n")
    _r, models = _resolve(
        {"m": mixin, "mx": mixin_ext, "d": daughter, "p": partner},
        {"m": [], "mx": ["m"], "d": ["mx"], "p": ["d"]},
    )
    # delegation is modelled: the daughter delegates to the mixin
    assert models["test.daughter"].delegates == ["daughter.mixin"]
    f = models["res.partner"].fields["daughter_ids"]
    assert f.resolution is Resolution.STATIC
    assert "inverse-resolved" in f.notes
    assert "inverse-dangling" not in f.notes


def test_inverse_name_restored_by_merge_is_verified():
    # inverse-only-after-merge: an override of the One2many restates only `string`,
    # dropping `inverse_name`. `_merge_field` restores it from the base One2many;
    # verification must run on the MERGED inverse_name, not the (absent) raw one.
    base = ("class Order(models.Model):\n"
            "    _name = 'sale.order'\n"
            "    line_ids = fields.One2many('sale.order.line', 'order_id')\n"
            "class Line(models.Model):\n"
            "    _name = 'sale.order.line'\n"
            "    order_id = fields.Many2one('sale.order')\n")
    override = ("class Order(models.Model):\n"
                "    _inherit = 'sale.order'\n"
                "    line_ids = fields.One2many(string='Lines')\n")
    _r, models = _resolve({"a": base, "ext": override},
                          {"a": [], "ext": ["a"]})
    f = models["sale.order"].fields["line_ids"]
    assert f.info.attributes["inverse_name"] == "order_id"   # restored by merge
    assert f.resolution is Resolution.STATIC
    assert "inverse-resolved" in f.notes


def test_dangling_inverse_is_unresolved_not_guessed():
    # the sole provable defect: the inverse names a field ABSENT from a fully
    # visible comodel. UNRESOLVED + a reason note the analyzer can turn into a
    # Diagnostic (RFC §6) — never silently accepted as STATIC.
    src = ("class Order(models.Model):\n"
           "    _name = 'sale.order'\n"
           "    line_ids = fields.One2many('sale.order.line', 'no_such_field')\n"
           "class Line(models.Model):\n"
           "    _name = 'sale.order.line'\n"
           "    name = fields.Char()\n")
    _r, models = _resolve({"a": src})
    f = models["sale.order"].fields["line_ids"]
    assert f.resolution is Resolution.UNRESOLVED
    assert "inverse-dangling" in f.notes
    assert "inverse-missing:no_such_field" in f.notes


def test_nonrelational_inverse_stays_static_with_honest_note():
    # the reference-style inverse (6 live): the inverse names a REAL field on the
    # comodel that is an Integer soft-FK (`res_id`/`sms_id_int` — paired with a
    # res_model or a deleted-record id). A legitimate Odoo pattern: the comodel is
    # solidly known, so we do NOT fabricate a defect. STATIC + an honest note the
    # analyzer can weigh — honest in the "don't invent uncertainty" direction.
    src = ("class Host(models.Model):\n"
           "    _name = 'attach.host'\n"
           "    attachment_ids = fields.One2many('ir.attachment', 'res_id')\n"
           "class Attach(models.Model):\n"
           "    _name = 'ir.attachment'\n"
           "    res_model = fields.Char()\n"
           "    res_id = fields.Integer()\n")
    _r, models = _resolve({"a": src})
    f = models["attach.host"].fields["attachment_ids"]
    assert f.resolution is Resolution.STATIC
    assert "inverse-nonrelational" in f.notes
    assert "inverse-dangling" not in f.notes


def test_inverse_comodel_outside_corpus_not_downgraded():
    # the comodel is a known literal but its module isn't in the corpus, so we
    # can't see its fields to prove the inverse dangles. We know the comodel — so
    # downgrading would fabricate uncertainty. Left STATIC + an honest note.
    src = ("class Order(models.Model):\n"
           "    _name = 'sale.order'\n"
           "    line_ids = fields.One2many('external.model.not.here', 'order_id')\n")
    _r, models = _resolve({"a": src})
    f = models["sale.order"].fields["line_ids"]
    assert f.resolution is Resolution.STATIC
    assert "inverse-comodel-outside-corpus" in f.notes
    assert "inverse-dangling" not in f.notes


def test_dynamic_inverse_stays_dynamic_and_unverified():
    # inverse_name is a runtime expression -> the parser marks the field DYNAMIC
    # ("dynamic-inverse"); verification is skipped, never forced to a verdict.
    src = ("class Order(models.Model):\n"
           "    _name = 'sale.order'\n"
           "    line_ids = fields.One2many('sale.order.line', SOME_VAR)\n"
           "class Line(models.Model):\n"
           "    _name = 'sale.order.line'\n"
           "    order_id = fields.Many2one('sale.order')\n")
    _r, models = _resolve({"a": src})
    f = models["sale.order"].fields["line_ids"]
    assert f.resolution is Resolution.DYNAMIC
    assert "dynamic-inverse" in f.notes
    assert "inverse-resolved" not in f.notes
    assert "inverse-dangling" not in f.notes


def test_delegate_true_field_stays_a_real_many2one():
    # `delegate=True` establishes delegation but the field itself is still a real
    # Many2one on the model — it must not vanish or lose its comodel.
    src = ("class Cron(models.Model):\n"
           "    _name = 'ir.cron'\n"
           "    server_id = fields.Many2one('ir.actions.server', delegate=True, required=True)\n"
           "class Server(models.Model):\n"
           "    _name = 'ir.actions.server'\n"
           "    name = fields.Char()\n")
    _r, models = _resolve({"a": src})
    cron = models["ir.cron"]
    assert cron.delegates == ["ir.actions.server"]
    assert cron.fields["server_id"].info.type == "Many2one"
    assert cron.fields["server_id"].info.attributes["comodel"] == "ir.actions.server"
    # the delegate's fields are exposed on the delegating model
    assert cron.fields["name"].provenance.via == "_inherits"


def test_type_changing_override_drops_stale_comodel():
    # A Many2one redefined as a Char must NOT drag the comodel onto a scalar.
    base = ("class M(models.Model):\n"
            "    _name = 'm'\n"
            "    thing = fields.Many2one('res.partner')\n")
    override = ("class M(models.Model):\n"
                "    _inherit = 'm'\n"
                "    thing = fields.Char(help='now a label')\n")
    _r, models = _resolve({"a": base, "b": override}, {"a": [], "b": ["a"]})
    f = models["m"].fields["thing"]
    assert f.info.type == "Char"
    assert f.info.attributes.get("comodel") is None
    assert f.resolution is Resolution.STATIC
    assert f.info.attributes["help"] == "now a label"
