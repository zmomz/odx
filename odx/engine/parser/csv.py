"""CSV parser: ir.model.access.csv (and other CSV data) -> ACL rows + Location.

Phase 3. Unmeasured by the spikes (deliberately, per XML_RESULTS.md) — lower
risk than view inheritance. Feeds the security analyzers (missing-ACL checks)
and the §7 reference index (access rows reference models/groups).
"""

from __future__ import annotations

from pathlib import Path

from odx.engine.contracts import AccessRule


def parse_access_csv(path: Path, module: str) -> list[AccessRule]:
    raise NotImplementedError("Phase 3")
