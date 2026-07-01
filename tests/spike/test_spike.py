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
