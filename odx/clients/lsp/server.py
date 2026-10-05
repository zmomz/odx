"""`odx-lsp` — a Language Server over the engine (stdio).

Features:
  - diagnostics: whole project on `initialized`, re-checked on save
    (incremental reload of the saved file; RFC §8)
  - hover: field / model / method / xml-id under the cursor
  - definition: field, model, method or xml-id -> its declaration
  - references: where-used of the symbol under the cursor (the §7 index)
  - completion: field names inside `<field name="…">` of a view (XML) and
    inside `@api.depends('…')` / `related='…'` strings (Python)
  - code actions: a Diagnostic's suggested_fix -> WorkspaceEdit

Rendering only: every answer is an Engine query mapped to LSP shapes.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import pathname2url

from odx.clients.lsp.protocol import read_message, write_message
from odx.engine.api import Engine
from odx.engine.contracts import Diagnostic, Location, Resolution, Scope, Severity

_SEV = {Severity.ERROR: 1, Severity.WARNING: 2, Severity.INFO: 3, Severity.HINT: 4}
_WORD = re.compile(r"[A-Za-z_][\w.]*")


def uri_to_path(uri: str) -> Path:
    return Path(unquote(urlparse(uri).path))


def path_to_uri(p: Path) -> str:
    return "file://" + pathname2url(str(Path(p).resolve()))


def lsp_range(loc: Location | None, text_line: str | None = None) -> dict:
    if loc is None:
        return {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}
    sl = max(loc.start_line - 1, 0)
    el = max(loc.end_line - 1, sl)
    sc, ec = loc.start_col, loc.end_col
    if ec <= sc and el == sl:                       # line-only location: span the text
        if text_line is not None:
            stripped = len(text_line) - len(text_line.lstrip())
            sc, ec = stripped, len(text_line.rstrip())
        else:
            ec = sc + 1
    return {"start": {"line": sl, "character": sc}, "end": {"line": el, "character": ec}}


def word_at(text: str, line: int, col: int) -> str:
    lines = text.splitlines()
    if line >= len(lines):
        return ""
    s = lines[line]
    for m in _WORD.finditer(s):
        if m.start() <= col <= m.end():
            return m.group(0)
    return ""


class OdxLanguageServer:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root
        self.engine: Engine | None = None
        self.docs: dict[str, str] = {}               # uri -> text (open documents)
        self.published: set[str] = set()
        self.out: list[dict] = []                    # messages to send
        self.shutdown = False

    # -- plumbing -------------------------------------------------------- #
    def notify(self, method: str, params: Any) -> None:
        self.out.append({"jsonrpc": "2.0", "method": method, "params": params})

    def handle(self, msg: dict) -> dict | None:
        method = msg.get("method")
        handler = getattr(self, "on_" + (method or "").replace("/", "_").replace("$", "S"), None)
        if "id" in msg and method is not None:        # request
            if handler is None:
                return {"jsonrpc": "2.0", "id": msg["id"],
                        "error": {"code": -32601, "message": f"method not found: {method}"}}
            try:
                result = handler(msg.get("params") or {})
            except Exception as e:                    # never take the editor down
                return {"jsonrpc": "2.0", "id": msg["id"],
                        "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}
            return {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        if handler is not None:                       # notification
            try:
                handler(msg.get("params") or {})
            except Exception as e:
                self.notify("window/logMessage", {"type": 1, "message": f"odx: {e}"})
        return None

    def _text(self, uri: str) -> str:
        if uri in self.docs:
            return self.docs[uri]
        try:
            return uri_to_path(uri).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    # -- lifecycle -------------------------------------------------------- #
    def on_initialize(self, params: dict) -> dict:
        root = params.get("rootUri") and uri_to_path(params["rootUri"]) or \
            (Path(params["rootPath"]) if params.get("rootPath") else None) or self.root or Path.cwd()
        self.root = Path(root)
        self.engine = Engine.load(self.root, cache=True)
        return {"capabilities": {
            "textDocumentSync": {"openClose": True, "change": 1, "save": {"includeText": False}},
            "hoverProvider": True, "definitionProvider": True, "referencesProvider": True,
            "completionProvider": {"triggerCharacters": ['"', "'", "."]},
            "codeActionProvider": True,
        }, "serverInfo": {"name": "odx", "version": "0.1"}}

    def on_initialized(self, params: dict) -> None:
        self.engine.warm()
        self.publish_all()

    def on_shutdown(self, params: dict) -> None:
        self.shutdown = True
        return None

    def on_exit(self, params: dict) -> None:
        self.shutdown = True

    # -- documents -------------------------------------------------------- #
    def on_textDocument_didOpen(self, params: dict) -> None:
        td = params["textDocument"]
        self.docs[td["uri"]] = td.get("text", "")

    def on_textDocument_didChange(self, params: dict) -> None:
        changes = params.get("contentChanges") or []
        if changes:
            self.docs[params["textDocument"]["uri"]] = changes[-1].get("text", "")

    def on_textDocument_didClose(self, params: dict) -> None:
        self.docs.pop(params["textDocument"]["uri"], None)

    def on_textDocument_didSave(self, params: dict) -> None:
        path = uri_to_path(params["textDocument"]["uri"])
        self.engine.reload([path])
        self.publish_all()

    # -- diagnostics ------------------------------------------------------ #
    def _to_lsp(self, d: Diagnostic) -> dict:
        text_line = None
        if d.location is not None:
            lines = self._text(path_to_uri(d.location.file)).splitlines()
            if 0 < d.location.start_line <= len(lines):
                text_line = lines[d.location.start_line - 1]
        out = {"range": lsp_range(d.location, text_line), "severity": _SEV[d.severity],
               "code": d.code, "source": "odx", "message": d.message}
        if d.related:
            out["relatedInformation"] = [
                {"location": {"uri": path_to_uri(r.file), "range": lsp_range(r)},
                 "message": "related"} for r in d.related]
        return out

    def diagnostics(self) -> list[Diagnostic]:
        return self.engine.check(Scope("project"))

    def publish_all(self) -> None:
        by_uri: dict[str, list[dict]] = {}
        for d in self.diagnostics():
            if d.location is None:
                continue
            by_uri.setdefault(path_to_uri(d.location.file), []).append(self._to_lsp(d))
        for uri in self.published - set(by_uri):
            self.notify("textDocument/publishDiagnostics", {"uri": uri, "diagnostics": []})
        for uri, ds in by_uri.items():
            self.notify("textDocument/publishDiagnostics", {"uri": uri, "diagnostics": ds})
        self.published = set(by_uri)

    # -- symbol under cursor --------------------------------------------- #
    def symbol(self, params: dict) -> tuple[str, str | None, str | None] | None:
        """(kind, model, member) for the cursor: kind in field|model|method|xmlid."""
        uri = params["textDocument"]["uri"]
        pos = params["position"]
        path = uri_to_path(uri)
        w = word_at(self._text(uri), pos["line"], pos["character"])
        if not w:
            return None
        line = pos["line"] + 1
        last = w.split(".")[-1]
        refs = [r for r in self.engine.references_in_file(path) if r.location.start_line == line]
        for r in refs:
            if r.field and (r.field == last or r.field == w) and r.model:
                if self.engine.get_field(r.model, r.field) is not None:
                    return ("field", r.model, r.field)
                return ("method", r.model, r.field)
        for r in refs:
            if r.model == w and not r.field:
                return ("model", w, None)
        for model, f in self.engine.fields_defined_in(path):
            if f.name == w and f.location and f.location.start_line == line:
                return ("field", model, f.name)
        if "." in w and self.engine.xmlid_location(w) is not None:
            return ("xmlid", w, None)
        if self.engine.get_model(w) is not None:
            return ("model", w, None)
        # a short xml-id (`ref="view_x"`) relative to the file's module
        mod = Engine.module_containing(path)
        if mod and self.engine.xmlid_location(f"{mod}.{w}") is not None:
            return ("xmlid", f"{mod}.{w}", None)
        return None

    def on_textDocument_hover(self, params: dict) -> dict | None:
        sym = self.symbol(params)
        if sym is None:
            return None
        kind, model, member = sym
        if kind == "field":
            f = self.engine.get_field(model, member)
            attrs = ", ".join(f"{k}={v!r}" for k, v in f.attributes.items() if k in
                              ("comodel", "related", "compute", "store", "required", "inverse_name"))
            res = "" if f.resolution is Resolution.STATIC else f"  \n_resolution: {f.resolution.value}_"
            md = (f"**{model}.{member}** · `{f.type}`  \ndefined in `{f.defined_in}`"
                  + (f"  \n{attrs}" if attrs else "") + res)
        elif kind == "model":
            m = self.engine.get_model(model)
            md = (f"**{model}** ({m.kind}) · {len(m.fields)} fields  \ndefined in "
                  f"`{m.defined_in}` · extended by {', '.join(m.modules[1:]) or 'nothing'}")
        elif kind == "method":
            md = f"**{model}.{member}()** — method"
        else:
            loc = self.engine.xmlid_location(model)
            md = f"**{model}** — external id" + (f" ({Path(loc.file).name}:{loc.start_line})" if loc else "")
        return {"contents": {"kind": "markdown", "value": md}}

    def on_textDocument_definition(self, params: dict) -> Any:
        sym = self.symbol(params)
        if sym is None:
            return None
        kind, model, member = sym
        loc = None
        if kind == "field":
            loc = self.engine.get_field(model, member).location
        elif kind == "model":
            loc = self.engine.get_model(model).location
        elif kind == "method":
            u = self.engine.usage(f"{model}.{member}")
            loc = u.definition
        elif kind == "xmlid":
            loc = self.engine.xmlid_location(model)
        if loc is None:
            return None
        return {"uri": path_to_uri(loc.file), "range": lsp_range(loc)}

    def on_textDocument_references(self, params: dict) -> list:
        sym = self.symbol(params)
        if sym is None:
            return []
        kind, model, member = sym
        if kind == "xmlid":
            return []
        target = f"{model}.{member}" if member else model
        u = self.engine.usage(target)
        return [{"uri": path_to_uri(r.location.file), "range": lsp_range(r.location)}
                for r in u.references if r.location.start_line > 0]

    def on_textDocument_completion(self, params: dict) -> dict:
        uri = params["textDocument"]["uri"]
        path = uri_to_path(uri)
        pos = params["position"]
        lines = self._text(uri).splitlines()
        if pos["line"] >= len(lines):
            return {"isIncomplete": False, "items": []}
        before = lines[pos["line"]][:pos["character"]]
        model = None
        if path.suffix == ".xml" and re.search(r"<field\s+[^>]*name=[\"'][\w]*$", before):
            model = self.engine.view_model_at(path, pos["line"] + 1)
        elif path.suffix == ".py" and re.search(r"(depends|related|constrains|onchange)\W*[\"'][\w.]*$", before):
            for m, f in self.engine.fields_defined_in(path):
                if f.location and f.location.start_line <= pos["line"] + 1:
                    model = m
            if model is None:
                for m, _f in self.engine.fields_defined_in(path):
                    model = m
                    break
        if not model:
            return {"isIncomplete": False, "items": []}
        items = [{"label": f.name, "kind": 5, "detail": f"{f.type} · {f.defined_in}"}
                 for f in self.engine.get_fields(model)]
        return {"isIncomplete": False, "items": items}

    def on_textDocument_codeAction(self, params: dict) -> list:
        path = uri_to_path(params["textDocument"]["uri"]).resolve()
        rng = params["range"]
        out = []
        for d in self.diagnostics():
            if d.suggested_fix is None or d.location is None:
                continue
            if Path(d.location.file).resolve() != path:
                continue
            line = d.location.start_line - 1
            if not (rng["start"]["line"] <= line <= rng["end"]["line"]):
                continue
            changes: dict[str, list] = {}
            for e in d.suggested_fix.edits:
                cur = self._text(path_to_uri(e.file))
                n = len(cur.splitlines()) + 1
                changes.setdefault(path_to_uri(e.file), []).append({
                    "range": {"start": {"line": 0, "character": 0},
                              "end": {"line": n, "character": 0}},
                    "newText": e.new_text})
            out.append({"title": f"odx: {d.suggested_fix.summary}", "kind": "quickfix",
                        "diagnostics": [self._to_lsp(d)], "edit": {"changes": changes}})
        return out


def serve(stdin=None, stdout=None, root: Path | None = None) -> int:
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    server = OdxLanguageServer(root)
    while True:
        msg = read_message(stdin)
        if msg is None:
            return 0
        resp = server.handle(msg)
        for note in server.out:
            write_message(stdout, note)
        server.out.clear()
        if resp is not None:
            write_message(stdout, resp)
        if msg.get("method") == "exit":
            return 0


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(prog="odx-lsp", description="odx language server (stdio)")
    ap.add_argument("--root", default=None)
    args = ap.parse_args()
    raise SystemExit(serve(root=Path(args.root) if args.root else None))


if __name__ == "__main__":
    main()
