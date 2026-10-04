"""Runtime settings (RFC §12, §21).

`RuntimeSettings` is the runtime's own, typed view of the project config. It is
built from the *parsed* `.odx.toml` mapping (a plain dict) so the runtime never
depends on the CLI's config module (C7: runtime must not import clients).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

RUNTIME_KINDS = ("docker", "venv", "source", "odoo.sh")

COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")


class SettingsError(ValueError):
    """The project config cannot be turned into usable runtime settings."""


@dataclass(frozen=True)
class RuntimeSettings:
    runtime: str = "docker"                    # docker | venv | source | odoo.sh
    project_root: Path = Path(".")
    odoo_version: str = "18.0"
    addons_paths: tuple[str, ...] = ("./addons",)
    source_path: str | None = None

    # database
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = "odoo"
    db_password: str | None = None
    db_name: str = "my_dev_db"

    # docker
    compose_file: str = "docker-compose.yml"
    docker_service: str = "odoo"
    docker_db_service: str = "db"

    # run
    http_port: int = 8069
    dev: str = "all"

    # overrides
    odoo_bin: str | None = None                # explicit odoo-bin path
    python: str | None = None                  # interpreter (venv/source)
    config_file: str | None = None             # odoo .conf passed with -c

    # logs
    logfile: str | None = None
    logs_scope: str | None = None

    def __post_init__(self) -> None:
        if self.runtime not in RUNTIME_KINDS:
            raise SettingsError(
                f"unknown runtime {self.runtime!r}; expected one of: {', '.join(RUNTIME_KINDS)}"
            )

    # ------------------------------------------------------------------ #
    @property
    def odoo_major(self) -> int:
        try:
            return int(str(self.odoo_version).split(".")[0])
        except ValueError:
            return 0

    def resolve(self, path: str | Path) -> Path:
        """Resolve a config path relative to the project root."""
        p = Path(path).expanduser()
        return Path(os.path.normpath(p if p.is_absolute() else (self.project_root / p)))

    # ------------------------------------------------------------------ #
    @classmethod
    def from_mapping(cls, root: str | Path, data: Mapping[str, Any]) -> "RuntimeSettings":
        """Build settings from a parsed `.odx.toml` mapping (see config.render_config)."""
        root = Path(root)
        odoo = _table(data, "odoo")
        db = _table(data, "database")
        run = _table(data, "run")
        logs = _table(data, "logs")
        docker = _table(data, "docker")

        addons = odoo.get("addons_paths", ["./addons"])
        if isinstance(addons, str):
            addons = [addons]

        compose = _opt_str(docker.get("compose_file"))
        if compose is None or (compose == COMPOSE_NAMES[0] and not (root / compose).is_file()):
            # unset, or the init default that isn't there: use whichever exists.
            compose = next((n for n in COMPOSE_NAMES if (root / n).is_file()), COMPOSE_NAMES[0])

        kwargs: dict[str, Any] = dict(
            runtime=str(odoo.get("runtime", "docker")),
            project_root=root,
            odoo_version=str(odoo.get("version", "18.0")),
            addons_paths=tuple(str(a) for a in addons),
            source_path=_opt_str(odoo.get("source_path")),
            db_host=str(db.get("host", "localhost")),
            db_port=_int(db.get("port", 5432), "database.port"),
            db_user=str(db.get("user", "odoo")),
            db_password=_opt_str(db.get("password")),
            db_name=str(db.get("default", "my_dev_db")),
            compose_file=compose,
            docker_service=str(docker.get("service", "odoo")),
            docker_db_service=str(docker.get("db_service", "db")),
            http_port=_int(run.get("http_port", 8069), "run.http_port"),
            dev=_opt_str(run.get("dev")) or "all",
            odoo_bin=_opt_str(odoo.get("odoo_bin")),
            python=_opt_str(odoo.get("python")),
            config_file=_opt_str(odoo.get("config_file")),
            logfile=_opt_str(logs.get("file")),
            logs_scope=_opt_str(logs.get("scope")),
        )
        return cls(**kwargs)


def _table(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key) or {}
    if not isinstance(value, Mapping):
        raise SettingsError(f"[{key}] must be a table")
    return value


def _opt_str(value: Any) -> str | None:
    """TOML has no null: `false` and `""` both mean 'unset'."""
    if value is None or value is False or value == "":
        return None
    return str(value)


def _int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise SettingsError(f"{name} must be an integer, got {value!r}") from None
