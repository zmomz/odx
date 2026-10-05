"""LSP client (RFC §13): diagnostics, hover, definition, references, completion,
code actions — driven through the JSON-RPC handlers, plus a real stdio session."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from odx.clients.lsp.protocol import read_message, write_message
from odx.clients.lsp.server import OdxLanguageServer, path_to_uri

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "planted"


@pytest.fixture()
def proj(tmp_path, monkeypatch):
    monkeypatch.setenv("ODX_CACHE_DIR", str(tmp_path / "cache"))
    root = tmp_path / "proj"
    shutil.copytree(FIXTURE, root / "addons")
    (root / ".odx.toml").write_text('[odoo]\naddons_paths = ["./addons"]\n')
    return root


@pytest.fixture()
def srv(proj):
    s = OdxLanguageServer()
    s.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"rootUri": path_to_uri(proj)}})
    s.handle({"jsonrpc": "2.0", "method": "initialized", "params": {}})
    return s


def _pos(path: Path, needle: str, offset: int = 0):
    for i, line in enumerate(path.read_text().splitlines()):
        if needle in line:
            return {"line": i, "character": line.index(needle) + offset}
    raise AssertionError(needle)


def req(s, method, params):
    r = s.handle({"jsonrpc": "2.0", "id": 9, "method": method, "params": params})
    assert "error" not in r, r
    return r["result"]


def test_initialize_advertises_capabilities(proj):
    s = OdxLanguageServer()
    r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"rootUri": path_to_uri(proj)}})
    caps = r["result"]["capabilities"]
    assert caps["hoverProvider"] and caps["referencesProvider"] and caps["codeActionProvider"]


def test_diagnostics_published_for_project_files(srv, proj):
    pubs = [m["params"] for m in srv.out if m["method"] == "textDocument/publishDiagnostics"]
    views = path_to_uri(proj / "addons/lib_ext/views/ext_views.xml")
    d = next(p for p in pubs if p["uri"] == views)
    codes = {x["code"] for x in d["diagnostics"]}
    assert "view.unknown-field" in codes
    unk = next(x for x in d["diagnostics"] if x["code"] == "view.unknown-field")
    line = (proj / "addons/lib_ext/views/ext_views.xml").read_text().splitlines()[unk["range"]["start"]["line"]]
    assert "pages_count" in line and unk["range"]["end"]["character"] > unk["range"]["start"]["character"]


def test_save_after_fix_clears_the_diagnostic(srv, proj):
    v = proj / "addons/lib_ext/views/ext_views.xml"
    v.write_text(v.read_text().replace('<field name="pages_count"/>', ""))
    srv.out.clear()
    srv.handle({"jsonrpc": "2.0", "method": "textDocument/didSave",
                "params": {"textDocument": {"uri": path_to_uri(v)}}})
    pub = next(m["params"] for m in srv.out if m["params"]["uri"] == path_to_uri(v))
    assert "view.unknown-field" not in {x["code"] for x in pub["diagnostics"]}


def test_hover_definition_references_on_a_view_field(srv, proj):
    v = proj / "addons/lib_core/views/library_views.xml"
    td = {"uri": path_to_uri(v)}
    pos = _pos(v, 'name="author_id"', len('name="') + 2)
    hover = req(srv, "textDocument/hover", {"textDocument": td, "position": pos})
    assert "library.book.author_id" in hover["contents"]["value"] and "Many2one" in hover["contents"]["value"]
    d = req(srv, "textDocument/definition", {"textDocument": td, "position": pos})
    assert d["uri"].endswith("lib_core/models/library.py")
    src = (proj / "addons/lib_core/models/library.py").read_text().splitlines()
    assert src[d["range"]["start"]["line"]].strip().startswith("author_id")
    refs = req(srv, "textDocument/references", {"textDocument": td, "position": pos})
    assert len(refs) >= 3 and any(r["uri"].endswith("ext_security.xml") or r["uri"].endswith(".xml") for r in refs)


def test_definition_of_an_xmlid_ref(srv, proj):
    v = proj / "addons/lib_ext/views/ext_views.xml"
    pos = _pos(v, "lib_core.view_book_form", 3)
    d = req(srv, "textDocument/definition", {"textDocument": {"uri": path_to_uri(v)}, "position": pos})
    assert d["uri"].endswith("lib_core/views/library_views.xml")


def test_hover_on_python_field_definition(srv, proj):
    py = proj / "addons/lib_core/models/library.py"
    pos = _pos(py, "author_id = fields", 2)
    h = req(srv, "textDocument/hover", {"textDocument": {"uri": path_to_uri(py)}, "position": pos})
    assert "library.book.author_id" in h["contents"]["value"]


def test_completion_of_view_field_names(srv, proj):
    v = proj / "addons/lib_core/views/library_views.xml"
    uri = path_to_uri(v)
    text = v.read_text().replace('<field name="title"/>\n                        <field name="author_id"',
                                 '<field name="ti\n                        <field name="author_id"', 1)
    srv.handle({"jsonrpc": "2.0", "method": "textDocument/didOpen",
                "params": {"textDocument": {"uri": uri, "text": text}}})
    line_no = next(i for i, l in enumerate(text.splitlines()) if l.rstrip().endswith('name="ti'))
    col = len(text.splitlines()[line_no])
    items = req(srv, "textDocument/completion", {"textDocument": {"uri": uri},
                                                 "position": {"line": line_no, "character": col}})
    labels = {i["label"] for i in items["items"]}
    assert {"title", "author_id", "state"} <= labels


def test_code_action_from_suggested_fix(srv, proj):
    py = proj / "addons/lib_ext/models/ext.py"
    line = _pos(py, "class LibraryLoan")["line"]
    acts = req(srv, "textDocument/codeAction", {
        "textDocument": {"uri": path_to_uri(py)},
        "range": {"start": {"line": line, "character": 0}, "end": {"line": line, "character": 0}},
        "context": {"diagnostics": []}})
    assert acts and acts[0]["kind"] == "quickfix"
    edited = "".join(acts[0]["edit"]["changes"].keys())
    assert "ir.model.access.csv" in edited


def test_unknown_method_is_a_jsonrpc_error(srv):
    r = srv.handle({"jsonrpc": "2.0", "id": 5, "method": "nope/nothing", "params": {}})
    assert r["error"]["code"] == -32601


def test_stdio_session(proj, tmp_path):
    msgs = io.BytesIO()
    for m in ({"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"rootUri": path_to_uri(proj)}},
              {"jsonrpc": "2.0", "method": "initialized", "params": {}},
              {"jsonrpc": "2.0", "id": 2, "method": "shutdown", "params": {}},
              {"jsonrpc": "2.0", "method": "exit", "params": {}}):
        write_message(msgs, m)
    env = {**__import__("os").environ, "ODX_CACHE_DIR": str(tmp_path / "c")}
    p = subprocess.run([sys.executable, "-m", "odx.clients.lsp.server"], input=msgs.getvalue(),
                       capture_output=True, timeout=120, env=env)
    assert p.returncode == 0, p.stderr.decode()
    out = io.BytesIO(p.stdout)
    got = []
    while (m := read_message(out)) is not None:
        got.append(m)
    assert got[0]["id"] == 1 and "capabilities" in got[0]["result"]
    assert any(m.get("method") == "textDocument/publishDiagnostics" for m in got)
    assert any(m.get("id") == 2 for m in got)
