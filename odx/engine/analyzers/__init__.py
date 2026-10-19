"""analyzers — uniform Analyzer contract (RFC §9); plugin-ready, not pluggable.

Every analyzer — built-in or future third-party — implements the same
interface and reserves a `code_prefix` namespace. Built-ins are NOT
special-cased: `run_analyzers` iterates the same list a plugin would join.
No discovery/loading machinery exists (RFC §9 — the seam, not the machinery).

Discovery vs accusation (RFC §6): the resolver, arch builder and index RECORD
honest state (`Resolution.UNRESOLVED` + a note); analyzers are the one path
from that state to a `Diagnostic`. They only read the SemanticAPI.
"""

from __future__ import annotations

from typing import Protocol

from odx.engine.contracts import Diagnostic, Scope, Severity


class Analyzer(Protocol):
    code_prefix: str                  # namespace, e.g. "view", "oca", "perf"
    categories: tuple[str, ...]       # `--only` aliases this analyzer answers to

    def run(self, api, scope: Scope) -> list[Diagnostic]:  # api: SemanticAPI
        ...


# `odx check --only views,access,security` -> these category names
CATEGORIES = {
    "views": "view", "view": "view",
    "fields": "field", "field": "field", "models": "model", "model": "model",
    "access": "access", "acl": "access",
    "security": "security",
    "xmlids": "xmlid", "xmlid": "xmlid",
    "manifest": "manifest", "manifests": "manifest", "dependencies": "module",
    "module": "module", "modules": "module",
    "data": "data", "actions": "action", "action": "action",
    "reports": "report", "report": "report", "python": "python",
    "parse": "parse",
}


def all_analyzers() -> list:
    from odx.engine.analyzers.access import AccessAnalyzer
    from odx.engine.analyzers.dependencies import DependencyAnalyzer
    from odx.engine.analyzers.fields import FieldAnalyzer
    from odx.engine.analyzers.index_refs import IndexReferenceAnalyzer
    from odx.engine.analyzers.manifest import ManifestAnalyzer
    from odx.engine.analyzers.model_resolution import ModelResolutionAnalyzer
    from odx.engine.analyzers.views import ViewAnalyzer
    from odx.engine.analyzers.xmlids import XmlIdAnalyzer
    return [IndexReferenceAnalyzer(), ModelResolutionAnalyzer(), FieldAnalyzer(),
            ViewAnalyzer(), AccessAnalyzer(), XmlIdAnalyzer(), ManifestAnalyzer(),
            DependencyAnalyzer()]


_SEV_ORDER = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2, Severity.HINT: 3}


def _wanted(d: Diagnostic, only: set[str] | None) -> bool:
    if not only:
        return True
    prefix = d.code.split(".", 1)[0]
    return prefix in only or d.code in only


def run_analyzers(api, scope: Scope, only: list[str] | None = None) -> list[Diagnostic]:
    """Run the battery over `scope`; `only` filters by category/prefix/code."""
    wanted: set[str] | None = None
    if only:
        wanted = set()
        for o in only:
            o = o.strip()
            if o:
                wanted.add(CATEGORIES.get(o, o))
    out: list[Diagnostic] = []
    seen: set[tuple] = set()
    for a in all_analyzers():
        for d in a.run(api, scope):
            if not _wanted(d, wanted):
                continue
            key = (d.code, d.model, d.field, d.location, d.message)
            if key in seen:
                continue
            seen.add(key)
            out.append(d)
    out.sort(key=lambda d: (str(d.location.file) if d.location else "~",
                            d.location.start_line if d.location else 0,
                            _SEV_ORDER.get(d.severity, 9), d.code))
    return out


__all__ = ["Analyzer", "CATEGORIES", "all_analyzers", "run_analyzers"]
