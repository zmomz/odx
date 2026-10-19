from __future__ import annotations

import xmlrpc.client

import pytest

from odx.engine.contracts import DbField, DbModel, DbSnapshot
from odx.runtime.rpc import (
    OdooRpc, RpcError, snapshot_from_dict, snapshot_from_json, snapshot_to_dict, snapshot_to_json,
)
from odx.runtime.settings import RuntimeSettings

TABLES = {
    "ir.model": [
        {"id": 1, "model": "res.partner", "modules": "base, sale", "transient": False},
        {"id": 2, "model": "sale.wizard", "modules": "sale", "transient": True},
    ],
    "ir.model.fields": [
        {"id": 1, "model": "res.partner", "name": "name", "ttype": "char", "relation": False,
         "required": True, "store": True, "modules": "base"},
        {"id": 2, "model": "res.partner", "name": "sale_ids", "ttype": "one2many",
         "relation": "sale.order", "required": False, "store": True, "modules": "sale"},
        {"id": 3, "model": "sale.wizard", "name": "x", "ttype": "integer", "relation": False,
         "required": False, "store": False, "modules": "sale"},
    ],
    "ir.module.module": [
        {"id": 2, "name": "sale", "state": "installed"},
        {"id": 1, "name": "base", "state": "installed"},
    ],
    "ir.model.data": [{"id": i, "module": "base", "name": f"x{i}"} for i in range(5)],
}


class FakeCommon:
    def __init__(self, ok=True):
        self.ok = ok

    def authenticate(self, db, user, password, ctx):
        return 7 if self.ok and (user, password) == ("admin", "pw") else False


class FakeObject:
    def __init__(self):
        self.calls = []

    def execute_kw(self, db, uid, password, model, method, args, kwargs):
        assert (db, uid, password) == ("mydb", 7, "pw")
        self.calls.append((model, method, args, kwargs))
        rows = TABLES[model]
        domain = args[0]
        if domain:
            (fname, _op, value), = domain
            rows = [r for r in rows if r.get(fname) == value]
        off, lim = kwargs.get("offset", 0), kwargs.get("limit")
        rows = rows[off: off + lim if lim else None]
        return [{k: v for k, v in r.items() if k in kwargs["fields"] or k == "id"} for r in rows]


def factory(obj=None, ok=True):
    obj = obj or FakeObject()
    urls = []

    def make(url):
        urls.append(url)
        return FakeCommon(ok) if url.endswith("/common") else obj
    make.urls = urls
    return make


def test_snapshot_reads_models_fields_modules_xmlids():
    obj = FakeObject()
    f = factory(obj)
    rpc = OdooRpc("http://h:8069/", "mydb", "admin", "pw", proxy_factory=f, batch_size=2)
    snap = rpc.snapshot()
    assert f.urls == ["http://h:8069/xmlrpc/2/common", "http://h:8069/xmlrpc/2/object"]
    assert snap.database == "mydb"
    partner = snap.models["res.partner"]
    assert partner.modules == ("base", "sale") and not partner.transient
    assert partner.fields["name"] == DbField("name", "char", None, True, True, ("base",))
    assert partner.fields["sale_ids"].relation == "sale.order"
    assert snap.models["sale.wizard"].transient
    assert snap.models["sale.wizard"].fields["x"].store is False
    assert list(snap.installed_modules) == ["base", "sale"]
    assert snap.xmlids == tuple(f"base.x{i}" for i in range(5))
    # ir.model.data (5 rows, batch 2) needed 3 paged calls
    data_calls = [c for c in obj.calls if c[0] == "ir.model.data"]
    assert [c[3]["offset"] for c in data_calls] == [0, 2, 4]
    assert all(c[3]["order"] == "id" for c in data_calls)


def test_auth_failure_raises():
    rpc = OdooRpc("http://h", "mydb", "admin", "pw", proxy_factory=factory(ok=False))
    with pytest.raises(RpcError, match="authentication failed"):
        rpc.snapshot()


def test_connection_error_raises_rpcerror():
    def boom(url):
        class P:
            def authenticate(self, *a):
                raise ConnectionRefusedError("refused")
        return P()
    with pytest.raises(RpcError, match="cannot reach"):
        OdooRpc("http://h", "d", "u", "p", proxy_factory=boom).login()


def test_fault_raises_rpcerror():
    class Obj:
        def execute_kw(self, *a):
            raise xmlrpc.client.Fault(1, "AccessError")
    rpc = OdooRpc("http://h", "mydb", "admin", "pw", proxy_factory=factory(Obj()))
    with pytest.raises(RpcError, match="AccessError"):
        rpc.call("ir.model", "search_read", [])


def test_from_settings():
    s = RuntimeSettings(http_port=9999, db_name="dev")
    rpc = OdooRpc.from_settings(s, proxy_factory=factory())
    assert rpc.url == "http://localhost:9999" and rpc.db == "dev"


def test_json_roundtrip(tmp_path):
    snap = OdooRpc("http://h", "mydb", "admin", "pw", proxy_factory=factory()).snapshot()
    path = snapshot_to_json(snap, tmp_path / "snap.json")
    assert snapshot_from_json(path) == snap
    assert snapshot_from_dict(snapshot_to_dict(snap)) == snap


def test_from_dict_minimal_and_bad_format():
    snap = snapshot_from_dict({"database": "d", "models": {"m": {"fields": {"f": {"type": "char"}}}}})
    assert snap == DbSnapshot("d", {"m": DbModel("m", {"f": DbField("f", "char")})})
    with pytest.raises(ValueError):
        snapshot_from_dict({"format": 99})
