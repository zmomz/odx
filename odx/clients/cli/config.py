"""Project config (RFC §21). Format: TOML (`.odx.toml`).

Written by `odx init`, read by clients. Reading uses the stdlib `tomllib`
(Python 3.11+, no dependency). Writing is done by hand so we control comments
and layout without pulling in a TOML writer dependency for Phase 0.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_NAME = ".odx.toml"


@dataclass
class OdooConfig:
    version: str = "18.0"
    runtime: str = "docker"          # docker | venv | source | odoo.sh
    addons_paths: list[str] = field(default_factory=lambda: ["./addons"])
    source_path: str | None = None


@dataclass
class ProjectConfig:
    project: str = "my_project"
    odoo: OdooConfig = field(default_factory=OdooConfig)
    database_default: str = "my_dev_db"
    database_host: str = "localhost"
    database_port: int = 5432
    database_user: str = "odoo"
    database_password: str = "odoo"
    docker_compose_file: str = "docker-compose.yml"
    docker_service: str = "odoo"
    docker_db_service: str = "db"
    http_port: int = 8069
    logs_scope: str | None = None


def find_config(start: Path) -> Path | None:
    """Walk up from `start` looking for a .odx.toml (like git finds .git)."""
    start = start.resolve()
    for parent in (start, *start.parents):
        candidate = parent / CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


def load_config(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def render_config(cfg: ProjectConfig) -> str:
    """Render a ProjectConfig to commented TOML text (deterministic layout)."""
    addons = ", ".join(f'"{p}"' for p in cfg.odoo.addons_paths)
    source = f'"{cfg.odoo.source_path}"' if cfg.odoo.source_path else "false"
    logs_scope = f'"{cfg.logs_scope}"' if cfg.logs_scope else '""'
    return f"""\
# .odx.toml — odx project config (written by `odx init`)
# https://  (odx RFC §21)
project = "{cfg.project}"

[odoo]
version = "{cfg.odoo.version}"
runtime = "{cfg.odoo.runtime}"          # docker | venv | source | odoo.sh
addons_paths = [{addons}]
source_path = {source}

[database]
default = "{cfg.database_default}"
host = "{cfg.database_host}"
port = {cfg.database_port}
user = "{cfg.database_user}"
password = "{cfg.database_password}"

[docker]                          # used when runtime = "docker"
compose_file = "{cfg.docker_compose_file}"
service = "{cfg.docker_service}"
db_service = "{cfg.docker_db_service}"

[run]
http_port = {cfg.http_port}
dev = "all"

[logs]
scope = {logs_scope}

[engine]
cache = true                     # durable parse cache (~/.cache/odx); speeds up re-runs
"""


def write_config(root: Path, cfg: ProjectConfig) -> Path:
    path = root / CONFIG_NAME
    path.write_text(render_config(cfg), encoding="utf-8")
    return path
