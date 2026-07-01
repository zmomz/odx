"""analyzers — uniform Analyzer contract (RFC §9); plugin-ready, not pluggable.

Every analyzer (built-in or future third-party) implements the same interface
and reserves a `code_prefix` namespace. No plugin discovery/loading machinery is
built now — only the seam. Lands in Phase 4.
"""

from __future__ import annotations

from typing import Protocol

from odx.engine.contracts import Diagnostic


class Analyzer(Protocol):
    code_prefix: str  # namespace, e.g. "view", "oca", "perf"

    def run(self, api, scope) -> list[Diagnostic]:  # api: SemanticAPI, scope: Scope
        ...
