from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from odx.clients.cli import config as cfgmod
from odx.runtime.settings import RuntimeSettings, SettingsError


def test_from_rendered_default_config(tmp_path):
    data = tomllib.loads(cfgmod.render_config(cfgmod.ProjectConfig(project="acme")))
    s = RuntimeSettings.from_mapping(tmp_path, data)
    assert s.runtime == "docker"
    assert s.project_root == tmp_path
    assert s.odoo_version == "18.0" and s.odoo_major == 18
    assert s.addons_paths == ("./addons",)
    assert s.source_path is None                       # `false` in TOML
    assert (s.db_name, s.db_host, s.db_port) == ("my_dev_db", "localhost", 5432)
    assert (s.db_user, s.db_password) == ("odoo", "odoo")
    assert (s.compose_file, s.docker_service, s.docker_db_service) == (
        "docker-compose.yml", "odoo", "db")
    assert s.http_port == 8069 and s.dev == "all"
    assert s.logs_scope is None                        # "" in TOML


def test_from_mapping_full():
    data = {
        "project": "x",
        "odoo": {"version": "17.0", "runtime": "venv", "addons_paths": ["a", "b"],
                 "source_path": "/opt/odoo", "python": "/v/bin/python", "odoo_bin": "ob",
                 "config_file": "odoo.conf"},
        "database": {"default": "d", "host": "h", "port": "5433", "user": "u", "password": "p"},
        "run": {"http_port": 9000, "dev": "reload,qweb"},
        "logs": {"scope": "my_mod", "file": "odoo.log"},
        "docker": {"compose_file": "c.yml", "service": "web", "db_service": "pg"},
    }
    s = RuntimeSettings.from_mapping("/proj", data)
    assert s.runtime == "venv" and s.odoo_major == 17
    assert s.addons_paths == ("a", "b")
    assert s.db_port == 5433 and s.db_user == "u" and s.db_password == "p"
    assert s.compose_file == "c.yml" and s.docker_service == "web" and s.docker_db_service == "pg"
    assert s.http_port == 9000 and s.dev == "reload,qweb"
    assert s.logs_scope == "my_mod" and s.logfile == "odoo.log"
    assert s.python == "/v/bin/python" and s.odoo_bin == "ob" and s.config_file == "odoo.conf"


def test_empty_mapping_uses_defaults(tmp_path):
    s = RuntimeSettings.from_mapping(tmp_path, {})
    assert s.runtime == "docker" and s.db_password is None


def test_compose_file_autodetected(tmp_path):
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    s = RuntimeSettings.from_mapping(tmp_path, {"docker": {"compose_file": "docker-compose.yml"}})
    assert s.compose_file == "compose.yaml"
    s2 = RuntimeSettings.from_mapping(tmp_path, {})
    assert s2.compose_file == "compose.yaml"


def test_addons_path_string_is_accepted():
    s = RuntimeSettings.from_mapping("/p", {"odoo": {"addons_paths": "./addons"}})
    assert s.addons_paths == ("./addons",)


def test_bad_runtime_rejected():
    with pytest.raises(SettingsError):
        RuntimeSettings.from_mapping("/p", {"odoo": {"runtime": "kubernetes"}})


def test_bad_port_rejected():
    with pytest.raises(SettingsError):
        RuntimeSettings.from_mapping("/p", {"database": {"port": "abc"}})


def test_resolve_relative_and_absolute():
    s = RuntimeSettings(project_root=Path("/proj/sub"))
    assert s.resolve("addons") == Path("/proj/sub/addons")
    assert s.resolve("../odoo") == Path("/proj/odoo")
    assert s.resolve("/abs") == Path("/abs")


def test_render_config_has_docker_and_credentials():
    text = cfgmod.render_config(cfgmod.ProjectConfig())
    data = tomllib.loads(text)
    assert data["docker"] == {"compose_file": "docker-compose.yml", "service": "odoo",
                              "db_service": "db"}
    assert data["database"]["user"] == "odoo"
    assert data["database"]["password"] == "odoo"
