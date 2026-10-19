"""`odx-web` — read-only HTTP JSON API + a single-page model browser.

    GET /                         the browser (index.html)
    GET /api/modules              project modules
    GET /api/models?prefix=&module=
    GET /api/model/<name>         ModelInfo
    GET /api/usage?target=x.y     UsageReport (why/impact)
    GET /api/graph?model=x        model neighbourhood: _inherit / _inherits / comodels
    GET /api/check?only=&module=  Diagnostics
    GET /api/report               HealthReport
    GET /api/fix                  EditPlan preview (rendered diff) — never applied

Bound to 127.0.0.1 by default. Nothing here writes: previews only (C3).
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from odx.clients import serialize
from odx.clients.edits import render_plan
from odx.engine.api import Engine
from odx.engine.contracts import Scope

STATIC = Path(__file__).resolve().parent / "static"


def graph(eng: Engine, model: str) -> dict:
    m = eng.get_model(model)
    if m is None:
        return {"nodes": [], "edges": []}
    nodes = {model: {"id": model, "kind": "center"}}
    edges = []
    for p in m.inherit_chain:
        nodes.setdefault(p, {"id": p, "kind": "parent"})
        edges.append({"from": model, "to": p, "kind": "_inherit"})
    for d in m.delegates:
        nodes.setdefault(d, {"id": d, "kind": "delegate"})
        edges.append({"from": model, "to": d, "kind": "_inherits"})
    for f in m.fields:
        c = f.attributes.get("comodel")
        if isinstance(c, str) and f.via == "own":
            nodes.setdefault(c, {"id": c, "kind": "comodel"})
            edges.append({"from": model, "to": c, "kind": f.type, "field": f.name})
    for s in eng.list_models():
        if s.name != model and model in s.parents:
            nodes.setdefault(s.name, {"id": s.name, "kind": "child"})
            edges.append({"from": s.name, "to": model, "kind": "_inherit"})
    return {"nodes": list(nodes.values()), "edges": edges}


class App:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.engine = Engine.load(self.root)

    def scope(self, q: dict) -> Scope:
        mods = [m for m in (q.get("module", [""])[0] or "").split(",") if m]
        return Scope("modules", modules=tuple(mods)) if mods else Scope("project")

    def route(self, path: str, q: dict) -> tuple[int, str, bytes]:
        j = lambda obj: (200, "application/json",
                         serialize.dumps(obj, relative_to=self.root).encode())
        e = self.engine
        if path in ("/", "/index.html"):
            return 200, "text/html; charset=utf-8", (STATIC / "index.html").read_bytes()
        if path == "/api/modules":
            return j(e.modules(project_only=q.get("all", ["0"])[0] != "1"))
        if path == "/api/models":
            return j(e.list_models(q.get("prefix", [""])[0], q.get("module", [None])[0]))
        if path.startswith("/api/model/"):
            m = e.get_model(unquote(path[len("/api/model/"):]))
            return j(m) if m is not None else (404, "application/json", b'{"error":"unknown model"}')
        if path == "/api/usage":
            return j(e.usage(q.get("target", [""])[0]))
        if path == "/api/graph":
            return j(graph(e, q.get("model", [""])[0]))
        if path == "/api/check":
            only = [o for o in (q.get("only", [""])[0] or "").split(",") if o]
            return j(e.check(self.scope(q), only or None))
        if path == "/api/report":
            return j(e.report(self.scope(q)))
        if path == "/api/fix":
            plan = e.fix(self.scope(q))
            return j({"summary": plan.summary, "files": [str(x.file) for x in plan.edits],
                      "preview": render_plan(plan, cwd=self.root)})
        return 404, "application/json", b'{"error":"not found"}'


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:                         # noqa: N802
            u = urlparse(self.path)
            try:
                code, ctype, body = app.route(u.path, parse_qs(u.query))
            except Exception as e:                        # never kill the server
                code, ctype = 500, "application/json"
                body = json.dumps({"error": f"{type(e).__name__}: {e}"}).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args) -> None:        # quiet by default
            pass
    return Handler


def make_server(root: Path, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    app = App(root)
    app.engine.warm()
    return ThreadingHTTPServer((host, port), make_handler(app))


def main() -> None:
    ap = argparse.ArgumentParser(prog="odx-web", description="odx model browser (read-only)")
    ap.add_argument("--root", default=".")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    srv = make_server(Path(a.root), a.host, a.port)
    print(f"odx web: http://{a.host}:{srv.server_address[1]}/")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
