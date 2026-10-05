"""Index fixtures (RFC §22): each §7 reference kind is captured, with the right
target, location and Resolution, on the clean fixture module."""

from __future__ import annotations

from pathlib import Path

import pytest

from odx.engine.api import Engine
from odx.engine.contracts import Resolution
from odx.engine.index import RefKind
from odx.engine.index import exprs as X

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "planted"


@pytest.fixture(scope="module")
def api():
    return Engine.load(FIXTURE, addons_paths=[FIXTURE]).api


def kinds(api, model, field=None):
    return {e.kind for e in api.entries_to(model, field)}


@pytest.mark.parametrize("model,field,kind", [
    ("library.book", "title", RefKind.VIEW_FIELD),
    ("library.book", "state", RefKind.VIEW_MODIFIER),          # invisible / decoration
    ("res.partner", "country_id", RefKind.VIEW_DOMAIN),        # author_id domain path hop
    ("library.book", "author_id", RefKind.SEARCH_FILTER),      # group_by
    ("library.book", "isbn", RefKind.SQL_CONSTRAINT),
    ("library.book", "isbn", RefKind.CONSTRAINT),
    ("library.author", "book_ids", RefKind.DEPENDS),
    ("library.book", "author_id", RefKind.INVERSE),
    ("res.partner", "country_id", RefKind.RELATED),
    ("library.book", "currency_id", RefKind.CURRENCY_FIELD),
    ("library.book", "title", RefKind.MODEL_ATTR),             # _rec_name
    ("library.book", "company_id", RefKind.RECORD_RULE),
    ("library.book", "state", RefKind.ACTION_DOMAIN),
    ("library.book", "title", RefKind.REPORT),                 # QWeb t-field o.title
    ("res.country", "name", RefKind.REPORT),                   # o.author_id...country_id.name
    ("library.author", "name", RefKind.DATA_RECORD),
])
def test_field_reference_kind_captured(api, model, field, kind):
    assert kind.value in kinds(api, model, field)


@pytest.mark.parametrize("model,kind", [
    ("library.book", RefKind.VIEW),
    ("library.book", RefKind.ACTION),
    ("library.book", RefKind.MENU),
    ("library.book", RefKind.ACL),
    ("library.book", RefKind.RECORD_RULE),
    ("library.book", RefKind.REPORT),
    ("library.author", RefKind.FIELD_COMODEL),
    ("res.partner", RefKind.DELEGATE),
    ("library.book", RefKind.INHERIT),
])
def test_model_reference_kind_captured(api, model, kind):
    assert kind.value in kinds(api, model)


def test_method_references(api):
    refs = api.method_references("library.book", "action_done")
    assert refs and refs[0].kind == RefKind.VIEW_BUTTON.value
    assert api.method_references("library.author", "_compute_book_count")[0].kind == "field-method"


def test_nested_subview_context_is_the_comodel(api):
    # <field name="book_ids"><tree><field name="state"/> resolves on library.book
    refs = [e for e in api.entries_to("library.book", "state")
            if e.kind == "view-field" and "view_author_form" in (e.source or "")]
    assert refs and refs[0].resolution is Resolution.STATIC


def test_locations_point_at_the_referencing_line(api):
    e = next(e for e in api.entries_to("library.book", "author_id")
             if e.kind == "view-field" and "view_book_tree" in (e.source or ""))
    text = Path(e.location.file).read_text().splitlines()
    assert 'name="author_id"' in text[e.location.start_line - 1]


def test_every_reference_resolution_is_honest(api):
    # the fixture's stub `base` lacks the framework ir.* models: those are
    # honestly DYNAMIC (outside what we can see), never UNRESOLVED
    core = [e for e in api.entries() if e.module == "lib_core"
            and not (e.model or "").startswith("ir.")]
    assert all(e.resolution is not Resolution.UNRESOLVED for e in api.entries()
               if e.module == "lib_core")
    assert core and all(e.resolution is Resolution.STATIC for e in core), \
        [(e.kind, e.model, e.field, e.note) for e in core if e.resolution is not Resolution.STATIC]


def test_xmlid_graph_defined_and_implicit(api):
    assert api.xmlid_defined("lib_core.view_book_form")
    assert api.xmlid_defined("lib_core.model_library_book")             # implicit ir.model
    assert api.xmlid_defined("lib_ext.model_library_book")              # every contributor
    assert api.xmlid_defined("lib_core.field_library_book__title")      # implicit field
    assert api.xmlid_defined("base.module_lib_core")                    # implicit module


def test_targeted_invalidation(api):
    idx = api.index
    f = FIXTURE / "lib_core" / "views" / "library_views.xml"
    before = len(idx.in_file(f))
    assert before > 0
    removed = idx.drop_file(f)
    assert removed == before and idx.in_file(f) == []
    assert not idx.xmlids.is_defined("lib_core.view_book_form")


# --- pure expression readers --------------------------------------------- #
def test_domain_refs_only_real_leaves():
    d = X.domain_refs("[('type', 'not in', ('a', 'b', 'c')), '|', ('x.y', '=', z), ('k', 'in', [1])]")
    assert d.paths == ("type", "x.y", "k") and d.names == ("z",)


def test_modifier_names_skip_context():
    names, ok = X.modifier_names("state != 'draft' or not partner_id or context.get('x') or uid")
    assert ok and names == ["state", "partner_id"]


def test_var_paths_stop_at_calls():
    vps = X.var_paths("o.partner_id.name_get() and doc.amount", {"o", "doc"})
    assert {(v.var, v.path) for v in vps} == {("o", ("partner_id",)), ("doc", ("amount",))}
