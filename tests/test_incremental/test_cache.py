"""Durable parse cache (RFC §8): warm runs reuse parse results keyed by content
hash; a changed file is a miss; a corrupt entry is a miss, never a crash; the
cache lives outside the project (no-write law on user source)."""

from __future__ import annotations

import shutil
from pathlib import Path

from odx.engine.api import Engine
from odx.engine.cache import ParseCache

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "planted"


def _eng(root, cache_dir):
    e = Engine.load(root, addons_paths=[root], cache=cache_dir)
    e.warm()
    return e, e._incremental.cache


def test_warm_run_hits_and_changed_file_misses(tmp_path):
    root = tmp_path / "addons"
    shutil.copytree(FIXTURE, root)
    cdir = tmp_path / "cache"
    _, cold = _eng(root, cdir)
    assert cold.hits == 0 and cold.misses > 0
    e2, warm = _eng(root, cdir)
    assert warm.misses == 0 and warm.hits == cold.misses
    assert e2.field_exists("library.book", "title")
    f = root / "lib_core" / "models" / "library.py"
    f.write_text(f.read_text() + "\n# touched\n")
    _, third = _eng(root, cdir)
    assert third.misses == 1
    assert not any(p for p in root.rglob("*.pkl"))          # nothing written into source


def test_corrupt_entry_is_a_miss(tmp_path):
    c = ParseCache(tmp_path)
    f = FIXTURE / "base" / "models" / "res.py"
    from odx.engine.parser.python import parse_python
    c.put(f, "abc", parse_python(f, "base"))
    p = c._path(f, "abc")
    p.write_bytes(b"not a pickle")
    assert c.get(f, "abc") is None
    assert c.get(f, "") is None


def test_parser_version_is_part_of_the_key(tmp_path, monkeypatch):
    a = ParseCache(tmp_path)
    b = ParseCache(tmp_path)
    b.version = "different"
    f = FIXTURE / "base" / "models" / "res.py"
    assert a._path(f, "h") != b._path(f, "h")
