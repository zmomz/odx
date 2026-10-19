"""MCP client (RFC §13, Phase 5): the tool layer over the engine, plus a real
stdio round trip through the MCP protocol."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

import pytest

from odx.clients.mcp.server import TOOLS, build_server
from odx.clients.mcp.tools import OdxTools

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "planted"


@pytest.fixture()
def proj(tmp_path):
    root = tmp_path / "proj"
    shutil.copytree(FIXTURE, root / "addons")
    (root / ".odx.toml").write_text('[odoo]\naddons_paths = ["./addons"]\n')
    return root


@pytest.fixture()
def tools(proj):
    return OdxTools(proj)


def test_every_registered_tool_is_json_serializable(tools):
    calls = {
        "list_modules": {}, "list_models": {"prefix": "library."},
        "get_model": {"name": "library.book"},
        "field_exists": {"model": "library.book", "field": "title"},
        "find_references": {"target": "library.book.state"},
        "check": {"only": ["views"]}, "report": {},
        "validate_field": {"model": "library.book", "name": "a_id", "type": "Many2one",
                           "comodel": "library.author"},
        "upgrade_check": {"target": "18.0"}, "reload": {},
        "plan_fix": {"only": ["access"]},
    }
    for name, args in calls.items():
        json.dumps(getattr(tools, name)(**args))


def test_field_exists_is_tri_state_with_runtime_hint(tools):
    assert tools.field_exists("library.book", "title")["state"] == "static"
    no = tools.field_exists("library.book", "nope")
    assert no["state"] == "unresolved" and not no["exists"]
    dyn = tools.field_exists("library.book", "x_studio")
    assert dyn["state"] == "dynamic" and "runtime" in dyn["hint"] or "running" in dyn["hint"]


def test_get_model_and_references(tools):
    m = tools.get_model("library.book")
    assert m["found"] and m["defined_in"] == "lib_core"
    names = {f["name"] for f in m["fields"]}
    assert {"title", "author_id", "shelf_code"} <= names
    refs = tools.find_references("library.book.state", kinds=["view-modifier"])
    assert refs["known"] and refs["references"] and \
        {r["kind"] for r in refs["references"]} == {"view-modifier"}
    assert not tools.get_model("no.model")["found"]


def test_propose_then_apply_writes_only_on_apply(tools, proj):
    csv = proj / "addons" / "lib_ext" / "security" / "ir.model.access.csv"
    p = tools.plan_fix(only=["access"])
    assert p["plan_id"] and "ir.model.access.csv" in p["preview"] and not csv.exists()
    res = tools.apply_plan(p["plan_id"])
    assert res["applied"] and csv.exists()
    assert not tools.apply_plan(p["plan_id"])["applied"]          # single use
    assert not any(d["code"] == "access.missing-acl" for d in tools.check()["diagnostics"])


def test_plan_add_field_validates_first(tools):
    bad = tools.plan_add_field("library.book", "x_ids", "One2many",
                               comodel="library.author", inverse_name="nope", module="lib_ext")
    assert bad["plan_id"] is None
    assert any(d["code"] == "field.spec-inverse-missing" for d in bad["diagnostics"])
    ok = tools.plan_add_field("library.book", "pages", "Integer", module="lib_ext")
    assert ok["plan_id"] and "pages = fields.Integer" in ok["preview"]


def test_server_registers_every_tool_with_annotations(proj):
    server = build_server(proj)
    listed = asyncio.run(server.list_tools())
    assert {t.name for t in listed} == set(TOOLS)
    by = {t.name: t for t in listed}
    assert by["check"].annotations.readOnlyHint is True
    assert by["apply_plan"].annotations.readOnlyHint is False
    out = asyncio.run(server.call_tool("field_exists", {"model": "library.book", "field": "title"}))
    blob = json.dumps([c.model_dump() if hasattr(c, "model_dump") else c for c in
                       (out[0] if isinstance(out, tuple) else out)], default=str)
    assert "static" in blob


def test_stdio_round_trip(proj):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "odx.clients.mcp.server", "--root", str(proj)])
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = await s.list_tools()
                res = await s.call_tool("check", {"only": ["views"]})
                return {t.name for t in tools.tools}, res
    names, res = asyncio.run(run())
    assert "check" in names and not res.isError
    text = "".join(getattr(c, "text", "") for c in res.content)
    assert "view.unknown-field" in text
