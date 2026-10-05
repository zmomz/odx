"""Analyzer fixtures with planted bugs (RFC §22): every planted defect yields the
expected Diagnostic `code` at the expected `Location`, and the clean module
(`lib_core`) yields none — a false accusation is the failure that destroys trust.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from odx.engine.api import Engine
from odx.engine.contracts import Scope, Severity

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "planted"


@pytest.fixture(scope="module")
def engine() -> Engine:
    return Engine.load(FIXTURE, addons_paths=[FIXTURE])


@pytest.fixture(scope="module")
def diags(engine):
    return engine.check(Scope("all"))


def _line(rel: str, needle: str) -> int:
    for i, line in enumerate((FIXTURE / rel).read_text().splitlines(), start=1):
        if needle in line:
            return i
    raise AssertionError(f"{needle!r} not in {rel}")


PLANTED = [
    # (code, file relative to fixture root, needle on the reported line)
    ("view.unknown-field", "lib_ext/views/ext_views.xml", "pages_count"),
    ("module.missing-dependency", "lib_ext/views/ext_views.xml", "shelf_code"),
    ("view.unknown-modifier-field", "lib_ext/views/ext_views.xml", "is_published"),
    ("view.domain-unknown-field", "lib_ext/views/ext_views.xml", "nickname"),
    ("view.xpath-target-missing", "lib_ext/views/ext_views.xml", "nonexistent_anchor"),
    ("view.button-method-missing", "lib_ext/views/ext_views.xml", "action_publish"),
    ("action.domain-unknown-field", "lib_ext/views/ext_views.xml", 'id="action_book_ext"'),
    ("report.unknown-field", "lib_ext/views/ext_views.xml", "isbn13"),
    ("field.inverse-dangling", "lib_ext/models/ext.py", "reviewer_ids"),
    ("field.related-broken", "lib_ext/models/ext.py", "missing_field"),
    ("field.method-missing", "lib_ext/models/ext.py", "_compute_score"),
    ("field.compute-store-no-depends", "lib_ext/models/ext.py", "rank = fields"),
    ("field.unknown-comodel", "lib_ext/models/ext.py", "library.shelf"),
    ("field.depends-unknown-field", "lib_ext/models/ext.py", "def _compute_origin"),
    ("field.monetary-no-currency", "lib_ext/models/ext.py", "fee = fields"),
    ("model.order-unknown-field", "lib_ext/models/ext.py", "class LibraryLoan"),
    ("access.missing-acl", "lib_ext/models/ext.py", "class LibraryLoan"),
    ("security.rule-unknown-field", "lib_ext/security/ext_security.xml", 'id="rule_book_owner"'),
    ("data.unknown-field", "lib_ext/data/ext_data.xml", "subtitle"),
    ("xmlid.unknown-ref", "lib_ext/data/ext_data.xml", "author_missing"),
    ("xmlid.missing-dependency", "lib_ext/data/ext_data.xml", "partner_shelf_keeper"),
    ("data.function-method-missing", "lib_ext/data/ext_data.xml", "_init_books"),
    ("parse.python-error", "lib_ext/models/broken.py", "from odoo import"),
    ("manifest.missing-data-file", "lib_ext/__manifest__.py", "{"),
]


@pytest.mark.parametrize("code,rel,needle", PLANTED, ids=[p[0] for p in PLANTED])
def test_planted_bug_is_reported_at_its_location(diags, code, rel, needle):
    want_file = (FIXTURE / rel).resolve()
    hits = [d for d in diags if d.code == code and d.location is not None
            and Path(d.location.file).resolve() == want_file]
    assert hits, f"{code} not reported in {rel}; got {[d.code for d in diags]}"
    want_line = _line(rel, needle)
    assert any(h.location.start_line == want_line for h in hits), \
        f"{code} at {[h.location.start_line for h in hits]}, expected line {want_line}"


def test_identical_duplicate_record_is_warned(diags):
    dup = [d for d in diags if d.code == "xmlid.duplicate"]
    assert dup and dup[0].severity is Severity.WARNING


def test_clean_module_has_no_findings(diags):
    core = (FIXTURE / "lib_core").resolve()
    bad = [d for d in diags if d.location is not None
           and core in Path(d.location.file).resolve().parents]
    assert not bad, [(d.code, d.message) for d in bad]


def test_base_stub_has_no_findings_besides_hints(diags):
    base = (FIXTURE / "base").resolve()
    bad = [d for d in diags if d.location is not None and d.severity is not Severity.HINT
           and base in Path(d.location.file).resolve().parents]
    assert not bad, [(d.code, d.message) for d in bad]


def test_only_filters_by_category(engine):
    views = engine.check(Scope("all"), only=["views"])
    assert views and all(d.code.startswith("view.") for d in views)


def test_scope_by_module_narrows(engine):
    only_core = engine.check(Scope("m", modules=("lib_core",)))
    assert only_core == []


def test_missing_acl_has_a_safe_fix_and_fix_composes(engine, diags):
    acl = next(d for d in diags if d.code == "access.missing-acl")
    assert acl.suggested_fix is not None
    plan = engine.fix(Scope("all"), only=["access"])
    files = {Path(e.file).name for e in plan.edits}
    assert "ir.model.access.csv" in files and "__manifest__.py" in files
    csv = next(e for e in plan.edits if Path(e.file).name == "ir.model.access.csv")
    assert "model_library_loan" in csv.new_text


def test_missing_dependency_fix_adds_depends(engine):
    plan = engine.fix(Scope("all"), only=["module", "xmlid"])
    man = [e for e in plan.edits if Path(e.file).name == "__manifest__.py"]
    assert man and "'lib_other'" in man[0].new_text
    # one manifest edit even though two diagnostics want the same dependency
    assert len(man) == 1 and man[0].new_text.count("'lib_other'") == 1


def test_report_scores_and_categorizes(engine):
    rep = engine.report(Scope("all"))
    assert 0 <= rep.score < 100 and rep.grade in "ABCDF"
    assert rep.by_category.get("view", 0) >= 5
    assert rep.recommendations
    clean = engine.report(Scope("m", modules=("lib_core",)))
    assert clean.score == 100 and clean.grade == "A"
