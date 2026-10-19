"""Engine-side project configuration (RFC §21: written by `init`, read by the
engine, used by every client).

Reads `.odx.toml` with stdlib `tomllib`. Two kinds of addons paths matter to the
engine:

  - **project paths** (`[odoo].addons_paths`) — the user's own modules.
    Diagnostics are scoped to these by default.
  - **context paths** — Odoo itself (`[odoo].source_path` → `<src>/addons` and
    `<src>/odoo/addons`) plus any `[odoo].context_paths` (OCA dependencies,
    enterprise). They are resolved so `_inherit` chains and xml-ids close, but
    never accused.

Without context paths the engine still works — every model it can't see
resolves DYNAMIC instead of being guessed (RFC §10).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_NAME = ".odx.toml"


@dataclass(frozen=True)
class EngineConfig:
    root: Path
    project_paths: tuple[Path, ...]
    context_paths: tuple[Path, ...] = ()
    odoo_version: str = "17.0"
    raw: dict = field(default_factory=dict)

    @property
    def all_paths(self) -> list[Path]:
        seen: list[Path] = []
        for p in (*self.project_paths, *self.context_paths):
            if p not in seen:
                seen.append(p)
        return seen


def find_config(start: Path) -> Path | None:
    start = Path(start).resolve()
    for parent in (start, *start.parents):
        c = parent / CONFIG_NAME
        if c.is_file():
            return c
    return None


def _paths(root: Path, values) -> list[Path]:
    out = []
    for v in values or []:
        if isinstance(v, str) and v:
            p = (root / v).resolve() if not Path(v).is_absolute() else Path(v)
            if p.is_dir():
                out.append(p)
    return out


def load_config(root: Path) -> EngineConfig:
    """Config for the project at/above `root`; a bare directory (no config) is a
    project whose addons path is the directory itself."""
    root = Path(root).resolve()
    cfg = find_config(root)
    if cfg is None:
        return EngineConfig(root=root, project_paths=(root,))
    base = cfg.parent
    try:
        data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        data = {}
    odoo = data.get("odoo", {}) if isinstance(data.get("odoo"), dict) else {}
    project = _paths(base, odoo.get("addons_paths")) or [base]
    context: list[Path] = []
    src = odoo.get("source_path")
    if isinstance(src, str) and src:
        s = (base / src).resolve() if not Path(src).is_absolute() else Path(src)
        context += [p for p in (s / "odoo" / "addons", s / "addons") if p.is_dir()]
    context += _paths(base, odoo.get("context_paths"))
    return EngineConfig(root=base, project_paths=tuple(project),
                        context_paths=tuple(p for p in context if p not in project),
                        odoo_version=str(odoo.get("version") or "17.0"), raw=data)
