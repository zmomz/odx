"""CI client (RFC §13): Diagnostics -> CI-native annotations.

  - GitHub Actions workflow commands (`::error file=…,line=…::msg`) — rendered
    inline on the PR diff with zero setup;
  - SARIF 2.1.0 — GitHub code scanning / GitLab / any SARIF consumer.

Pure renderers over the `Diagnostic` contract: the same object that is a CLI
table row and an MCP JSON item is a PR annotation here (RFC §10).
"""

from __future__ import annotations

from pathlib import Path

from odx.engine.contracts import Diagnostic, Severity

_GH_LEVEL = {Severity.ERROR: "error", Severity.WARNING: "warning",
             Severity.INFO: "notice", Severity.HINT: "notice"}
_SARIF_LEVEL = {Severity.ERROR: "error", Severity.WARNING: "warning",
                Severity.INFO: "note", Severity.HINT: "note"}


def _rel(path: Path, root: Path | None) -> str:
    if root is not None:
        try:
            return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
        except ValueError:
            pass
    return Path(path).as_posix()


def _esc(s: str) -> str:
    return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def github_annotations(diags: list[Diagnostic], root: Path | None = None) -> str:
    lines = []
    for d in diags:
        lvl = _GH_LEVEL.get(d.severity, "notice")
        props = [f"title={_esc(d.code)}"]
        if d.location is not None:
            props.insert(0, f"file={_rel(d.location.file, root)}")
            if d.location.start_line:
                props.insert(1, f"line={d.location.start_line}")
            if d.location.start_col:
                props.insert(2, f"col={d.location.start_col + 1}")
        lines.append(f"::{lvl} {','.join(props)}::{_esc(d.message)}")
    return "\n".join(lines)


def sarif(diags: list[Diagnostic], root: Path | None = None, version: str = "0.0.1") -> dict:
    rules: dict[str, dict] = {}
    results = []
    for d in diags:
        if d.code not in rules:
            rules[d.code] = {
                "id": d.code,
                "name": d.code.replace(".", "_").replace("-", "_"),
                "shortDescription": {"text": d.code},
                "defaultConfiguration": {"level": _SARIF_LEVEL.get(d.severity, "note")},
            }
        res: dict = {"ruleId": d.code, "level": _SARIF_LEVEL.get(d.severity, "note"),
                     "message": {"text": d.message}}
        if d.location is not None:
            region = {"startLine": max(d.location.start_line, 1)}
            if d.location.start_col:
                region["startColumn"] = d.location.start_col + 1
            res["locations"] = [{"physicalLocation": {
                "artifactLocation": {"uri": _rel(d.location.file, root)},
                "region": region}}]
        if d.model:
            res["properties"] = {"model": d.model, "field": d.field}
        results.append(res)
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "odx", "version": version,
                                "informationUri": "https://github.com/zmomz/odx",
                                "rules": list(rules.values())}},
            "results": results,
        }],
    }
