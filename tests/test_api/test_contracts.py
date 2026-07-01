"""Contracts must be importable AND instantiable at runtime.

Regression guard: a latent bug shipped in Phase 0 because nothing imported
`contracts` at runtime (the CLI only touches config/runtime; arch tests parse via
AST). `Diagnostic.field` shadowed `dataclasses.field`, so constructing a
Diagnostic raised. This test forces real construction of every contract.
"""

from __future__ import annotations

from pathlib import Path

from odx.engine.contracts import (
    AccessRule,
    Diagnostic,
    EditKind,
    EditPlan,
    FieldInfo,
    FileEdit,
    Location,
    ModelInfo,
    Provenance,
    Reference,
    Resolution,
    Severity,
)


def _loc() -> Location:
    return Location(Path("m/models/x.py"), 1, 0, 1, 10)


def test_diagnostic_constructs_with_defaults_and_field_attr():
    d = Diagnostic(severity=Severity.ERROR, code="view.unknown-field",
                   message="x", field="partner_id")
    assert d.related == []          # the shadowing bug made this line raise
    assert d.field == "partner_id"


def test_info_and_plan_contracts_construct():
    fi = FieldInfo(name="partner_id", type="many2one")
    mi = ModelInfo(name="res.users", fields=[fi],
                   inherit_chain=["base.mixin"], delegates=["res.partner"])
    assert mi.fields[0].attributes == {}
    # delegation (_inherits) is represented separately from extension (_inherit)
    assert mi.inherit_chain == ["base.mixin"] and mi.delegates == ["res.partner"]
    plan = EditPlan(edits=[FileEdit(_loc().file, EditKind.CREATE, None, "x")],
                    summary="s")
    assert plan.edits[0].kind is EditKind.CREATE
    Reference(kind="view", location=_loc())
    AccessRule("res.partner", None, True, True, False, False)


def test_resolution_and_provenance_are_first_class():
    assert [r.value for r in Resolution] == \
        ["static", "dynamic", "partial", "unresolved"]
    p = Provenance(module="base", location=_loc(), via="_inherit")
    assert p.via == "_inherit"
