"""Phase 0.5 spike as a permanent regression harness (RFC §22).

Skips cleanly when no corpus is present (so CI without `tests/corpus/` stays
green). When a corpus exists, it asserts the RFC §3 resolution thresholds still
hold and that the truly-undecidable (out-of-scope) set stays a small fraction —
guarding against a regression that would silently erode the core assumption.
"""

from __future__ import annotations

import pytest

from tests.spike.run import DEFAULT_CORPUS, find_modules, run


def _corpus_available() -> bool:
    return DEFAULT_CORPUS.is_dir() and bool(find_modules(DEFAULT_CORPUS))


pytestmark = pytest.mark.skipif(
    not _corpus_available(),
    reason="no corpus under tests/corpus/ (see tests/spike/README.md)",
)


@pytest.fixture(scope="module")
def result() -> dict:
    return run(DEFAULT_CORPUS)


def test_parse_rate_is_total(result):
    assert result["overall"]["parse_rate_pct"] == 100.0


def test_models_resolved_above_target(result):
    # RFC §3 target: >= 95%
    assert result["overall"]["models_named_pct"] >= 95.0


def test_inherit_chains_resolved_above_target(result):
    # RFC §3 target: >= 90%
    assert result["overall"]["inherit_resolved_pct"] >= 90.0


def test_out_of_scope_stays_small(result):
    # The core bet: truly-undecidable patterns must stay a small fraction of
    # model classes. Well under 5% (spike baseline ~0.9%).
    oos = result["catalog_by_tag"].get("out-of-scope", 0)
    models = result["overall"]["model_classes"]
    assert oos / models < 0.05, f"out-of-scope share too high: {oos}/{models}"


def test_no_fixable_regressions(result):
    # The chained-assignment parser bug was fixed during the spike; the fixable
    # bucket should stay empty. A non-zero value means a new parser gap crept in.
    assert result["catalog_by_tag"].get("fixable", 0) == 0


# --------------------------------------------------------------------------- #
# Phase 0.6 — XML view-inheritance spike
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def xml_result() -> dict:
    from tests.spike.run_xml import run_xml
    return run_xml(DEFAULT_CORPUS)


def test_xml_parse_rate_high(xml_result):
    assert xml_result["overall"]["parse_rate_pct"] >= 99.0


def test_inherit_id_cross_module_resolution(xml_result):
    # The load-bearing XML question: resolving inherit_id across modules.
    assert xml_result["overall"]["inherit_id_resolved_pct"] >= 95.0


def test_locator_target_resolution(xml_result):
    # xpath/position locators found in the applied inheritance chain.
    assert xml_result["overall"]["locator_target_found_pct"] >= 90.0


def test_xml_out_of_scope_negligible(xml_result):
    oos = xml_result["catalog_by_tag"].get("out-of-scope", 0)
    views = xml_result["overall"]["inheriting_views"]
    assert oos / views < 0.02, f"xml out-of-scope share too high: {oos}/{views}"


# --------------------------------------------------------------------------- #
# Phase 3 — graduated-engine resolver scoreboard (guards odx.engine, not the
# spike code). Thresholds sit below the measured baseline with headroom, so
# normal corpus drift doesn't flake but a real regression trips.
#   pre-merge baseline:  related→STATIC 254 · comodel-absent 80 · unresolved 13
#   post field-override attribute-merge (backlog item 2 done):
#     related→STATIC 263 · comodel-absent 0 · override-merge 76 · M2oRef DYNAMIC 6
#     · unresolved 2 (both hop through a module outside the corpus)
#   post one2many inverse discovery (backlog item 3 done):
#     inverse-resolved 618 · inverse-nonrelational 6 (reference-style, valid)
#     · inverse-dangling 0 · unresolved still 2 (modelling `delegate=True` dissolved
#     the one apparent dangling inverse — it was reachable via delegation)
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def resolver_result() -> dict:
    from tests.spike.run_resolver import run_resolver
    return run_resolver(DEFAULT_CORPUS)


def test_resolver_parse_rate_is_total(resolver_result):
    assert resolver_result["parse_rate_pct"] == 100.0


def test_resolver_model_count_stable(resolver_result):
    assert resolver_result["models_resolved"] >= 1250


def test_related_worklist_lift_holds(resolver_result):
    # the `related=` fixpoint must keep resolving the bulk of comodel-via-related
    # fields to STATIC. A drop here is a resolver regression invisible to unit tests.
    # Floor raised post-merge: attribute-merge restores `related=` that overrides
    # dropped, so the fixpoint now reaches 11 more fields (254 -> 263).
    assert resolver_result["related_resolved_static"] >= 255


def test_no_false_related_cycles_on_real_code(resolver_result):
    # the load-bearing guard: clean corpus code contains no genuine related-cycle,
    # so any non-zero count means the fixpoint is mis-detecting cycles (the exact
    # false-accusation failure the discovery/accusation split exists to prevent).
    assert resolver_result["related_cycles"] == 0


def test_unresolved_fields_stay_a_small_tail(resolver_result):
    fr = resolver_result["field_resolution"]
    unresolved = fr.get("unresolved", 0)
    assert unresolved / resolver_result["own_fields"] < 0.005


def test_comodel_absent_backlog_is_drained(resolver_result):
    # backlog-2 was 80 comodel-absent PARTIALs; field-override attribute-merge
    # drained it to 0 (74 -> STATIC, 6 genuine Many2oneReference -> DYNAMIC). This
    # locks the drain: reverting the merge to last-wins jumps this straight back to
    # ~80, which no other metric would catch. Small headroom for corpus drift.
    assert resolver_result["comodel_absent_partial"] <= 5


def test_override_attribute_merge_lift_holds(resolver_result):
    # the field-override attribute-merge itself: fields that recovered a comodel
    # from the field they override (same-model earlier contribution OR an
    # inherited/mixin field). A regression that stops merging drops this to 0.
    assert resolver_result["comodel_merged"] >= 70


def test_many2one_reference_marked_dynamic_not_partial(resolver_result):
    # the genuine runtime-derived comodels (Many2oneReference res_id/trg_field_ref)
    # must be marked DYNAMIC by nature, not left in the comodel-absent PARTIAL pile
    # and not fabricated. A regression that reverts them to PARTIAL drops this to 0.
    assert resolver_result["comodel_runtime_ref"] >= 5


def test_one2many_inverse_resolution_lift_holds(resolver_result):
    # backlog item 3: One2many inverses verified against the comodel's EFFECTIVE
    # fields (own + inherited + delegated). Floor sits below the measured 618 with
    # headroom. A regression that stops verifying, or that re-scans raw own fields
    # (missing the 18 inherited/delegated inverses), drops this.
    assert resolver_result["inverse_resolved"] >= 600


def test_no_dangling_inverses_on_real_code(resolver_result):
    # the load-bearing honesty guard (mirrors the false-cycle guard): clean corpus
    # code contains no genuinely-dangling One2many inverse. The corpus's one
    # apparent dangling inverse was reachable through a `delegate=True` delegation;
    # modelling that dissolved it. A non-zero count means either a real defect crept
    # into the corpus OR the lookup regressed to raw own fields and is now
    # falsely accusing an inherited/delegated inverse — the exact trap this closes.
    assert resolver_result["inverse_dangling"] == 0


def test_reference_style_inverses_stay_first_class(resolver_result):
    # the reference-style inverses (Integer soft-FK: res_id/sms_id_int/...) are a
    # valid Odoo pattern, marked honestly rather than fabricated into a defect or
    # force-resolved. Locks them as a recognised, non-empty first-class state.
    assert resolver_result["inverse_nonrelational"] >= 5


# --------------------------------------------------------------------------- #
# Phase 3 — graduated-engine EFFECTIVE-ARCH scoreboard (guards odx.engine, the
# view applicator). Confirm-live-state (before building) overturned the doc's
# framing a fourth time: the spike's 97.29% came from a loose per-ancestor test;
# a CORRECT primary-aware, load-ordered applicator that counts only TOP-LEVEL
# directives (Odoo's real spec) measures:
#   effective views built 3487 · record locators 2131 · record resolved 98.59%
#   · dangling-after-apply 30 (genuine UNRESOLVED, Phase-4 candidates)
#   · qweb-deferred 48 · interpolation-deferred 17 (DYNAMIC) · dialect gaps 0
#   · base-outside-corpus 7 (DYNAMIC). Thresholds sit below the measured baseline
# with headroom so corpus drift doesn't flake but a real regression trips.
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def arch_result() -> dict:
    from tests.spike.run_arch import run_arch
    return run_arch(DEFAULT_CORPUS)


def test_arch_parse_rate_high(arch_result):
    assert arch_result["parse_rate_pct"] >= 99.0


def test_arch_effective_views_built_stable(arch_result):
    assert arch_result["effective_views_built"] >= 3400


def test_arch_record_resolution_high(arch_result):
    # the load-bearing view number: record-view locators found after correct,
    # registry-wide, load-ordered application. A regression that stops respecting
    # the primary boundary or the load order drops this — invisible to unit tests.
    # Live after Odoo-parity application: 2261/2261. Small slack only for a
    # re-cloned corpus whose upstream branch moved.
    assert arch_result["record_resolved_pct"] >= 99.5


def test_arch_dangling_stays_a_small_tail(arch_result):
    # genuine UNRESOLVED record danglers (Phase-4 candidates). Kept a small,
    # bounded tail; a jump means the applicator regressed to producing false
    # missing-targets (the exact false accusation the whole design guards against).
    # The PHASE3 "30 genuine danglers" were applicator gaps ($0, <data>, default
    # `inside`, all-attribute shorthand matching, ...). Now 0; a jump means a
    # regression to false missing-targets.
    assert arch_result["record_dangling_after_apply"] <= 5
    assert arch_result["template_dangling_after_apply"] <= 5


def test_arch_deferred_buckets_are_closed(arch_result):
    # view backlog items 3 (%(xmlid)d) and 4 (QWeb templates) are closed: nothing
    # is parked as "deferred" any more. Optional (inactive) views are their own
    # honest PARTIAL bucket, never folded into danglers.
    assert arch_result["qweb_deferred"] == 0
    assert arch_result["interpolation_deferred"] == 0
    assert arch_result["inactive_view_misses"] >= 0


def test_corpus_check_stays_quiet():
    """The whole analyzer battery over Odoo 17 + OCA: Odoo core ships clean, so
    findings must stay a small, triaged set (61 live, each genuine or a corpus
    boundary). A jump is a false-accusation regression."""
    from pathlib import Path
    from odx.engine.api import Engine
    from odx.engine.contracts import Scope
    corpus = Path(DEFAULT_CORPUS)
    if not corpus.is_dir():
        pytest.skip("no corpus")
    diags = Engine.load(corpus, addons_paths=[corpus]).check(Scope("all"))
    by = {}
    for d in diags:
        by[d.code] = by.get(d.code, 0) + 1
    assert len(diags) <= 120, by
    assert by.get("view.xpath-target-missing", 0) <= 3, by
    assert by.get("view.unknown-field", 0) <= 3, by
    assert by.get("xmlid.unknown-ref", 0) <= 5, by


def test_arch_dialect_gaps_are_announced_not_silent(arch_result):
    # the audit finding: `hasclass` is the only extension function used in the
    # corpus, so live dialect gaps are 0. If a NEW extension ever appears it is
    # announced (DYNAMIC via dialect-unsupported), never a silent no-match — so
    # any non-zero here is a surfaced limitation, not a false accusation. Lock the
    # audited baseline at 0 to catch a genuinely-new construct for triage.
    assert arch_result["dialect_unsupported"] == 0
