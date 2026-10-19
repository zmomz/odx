"""ManifestAnalyzer — module graph and packaging (RFC §15 "manifest,
dependencies"): unknown/cyclic depends, declared data files that don't exist,
files that fail to parse, and manifest hygiene."""

from __future__ import annotations

from odx.engine.contracts import Diagnostic, Location, Scope, Severity


def _manifest_loc(api, module: str) -> Location | None:
    ms = api.module(module)
    if ms is None:
        return None
    for name in ("__manifest__.py", "__openerp__.py"):
        p = ms.path / name
        if p.is_file():
            return Location(p, 1, 0, 1, 0)
    return None


class ManifestAnalyzer:
    code_prefix = "manifest"
    categories = ("manifest", "parse")

    def run(self, api, scope: Scope) -> list[Diagnostic]:
        out: list[Diagnostic] = []
        graph = api.ws.graph
        cyc = set(graph.cycles())
        for name, ms in api.ws.modules.items():
            if not api.in_scope(scope, module=name):
                continue
            loc = _manifest_loc(api, name)
            mf = ms.manifest
            for dep in mf.depends:
                if dep not in graph.manifests:
                    out.append(Diagnostic(
                        Severity.WARNING, "manifest.unknown-dependency",
                        f"`{name}` depends on `{dep}`, which is not in any indexed addons path; "
                        "add its path to .odx.toml (context_paths) so references into it resolve.",
                        location=loc))
            if name in cyc:
                out.append(Diagnostic(
                    Severity.ERROR, "manifest.dependency-cycle",
                    f"`{name}` is part of a dependency cycle; Odoo cannot load it.",
                    location=loc))
            for rel in ms.missing_data_files:
                out.append(Diagnostic(
                    Severity.ERROR, "manifest.missing-data-file",
                    f"`{rel}` is declared in the manifest but does not exist; install will fail.",
                    location=loc))
            if not mf.raw:
                out.append(Diagnostic(
                    Severity.ERROR, "manifest.unreadable",
                    "The manifest is not a plain dict literal (or failed to parse).",
                    location=loc))
            elif "license" not in mf.raw:
                out.append(Diagnostic(
                    Severity.HINT, "manifest.no-license",
                    f"`{name}` declares no license (Odoo defaults to LGPL-3 and warns).",
                    location=loc))
            for rmod in ms.python:
                if rmod.parse_error:
                    out.append(Diagnostic(
                        Severity.ERROR, "parse.python-error",
                        f"{rmod.file.name}: {rmod.parse_error}",
                        location=Location(rmod.file, 1, 0, 1, 0)))
            for x in ms.xml:
                if x.parse_error:
                    out.append(Diagnostic(
                        Severity.ERROR, "parse.xml-error", f"{x.file.name}: {x.parse_error}",
                        location=Location(x.file, 1, 0, 1, 0)))
            for c in ms.csv:
                if c.parse_error:
                    out.append(Diagnostic(
                        Severity.ERROR, "parse.csv-error", f"{c.file.name}: {c.parse_error}",
                        location=Location(c.file, 1, 0, 1, 0)))
        return out
