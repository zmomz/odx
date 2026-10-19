"""Web client (RFC §13): the read-only JSON API and the browser page."""

from __future__ import annotations

import json
import shutil
import threading
import urllib.request
from pathlib import Path

import pytest

from odx.clients.web.server import make_server

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "planted"


@pytest.fixture(scope="module")
def base_url(tmp_path_factory):
    root = tmp_path_factory.mktemp("w") / "proj"
    shutil.copytree(FIXTURE, root / "addons")
    (root / ".odx.toml").write_text('[odoo]\naddons_paths = ["./addons"]\n')
    srv = make_server(root, port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.status, r.headers.get("Content-Type"), r.read()


def getj(url):
    return json.loads(get(url)[2])


def test_index_page(base_url):
    code, ctype, body = get(base_url + "/")
    assert code == 200 and "text/html" in ctype and b"odx model browser" in body


def test_models_and_model(base_url):
    names = {m["name"] for m in getj(base_url + "/api/models?prefix=library.")}
    assert {"library.book", "library.author", "library.loan"} <= names
    m = getj(base_url + "/api/model/library.book")
    assert m["defined_in"] == "lib_core" and any(f["name"] == "title" for f in m["fields"])


def test_usage_graph_check_report_fix(base_url):
    u = getj(base_url + "/api/usage?target=library.book.state")
    assert u["known"] and u["by_kind"]
    g = getj(base_url + "/api/graph?model=library.book")
    ids = {n["id"] for n in g["nodes"]}
    assert {"library.author", "res.company"} <= ids
    diags = getj(base_url + "/api/check?only=views")
    assert diags and all(d["code"].startswith("view.") for d in diags)
    rep = getj(base_url + "/api/report")
    assert rep["grade"] in "ABCDF"
    fix = getj(base_url + "/api/fix")
    assert "ir.model.access.csv" in fix["preview"]


def test_unknown_routes(base_url):
    import urllib.error
    with pytest.raises(urllib.error.HTTPError) as e:
        get(base_url + "/api/model/no.such")
    assert e.value.code == 404
