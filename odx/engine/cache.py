"""Durable parse cache (RFC §8: "caching is the same graph made durable").

Not a second mechanism: the incremental pipeline already keys every file by a
content hash; this persists the per-file parse result under that key so the
non-stale part of the graph survives process restart. Python parsing dominates
a cold build (~6s of ~8s on the 733-module corpus; unpickling the same results
takes ~0.5s), so `.py` parse results are what is cached. XML re-parses in ~1s and
holds lxml trees, so it isn't.

Key = sha1(parser-version, absolute path, file content hash). The parser
version is a hash of the parser/raw-type sources, so upgrading odx invalidates
the cache instead of serving stale shapes.

Location: `$ODX_CACHE_DIR`, else `$XDG_CACHE_HOME/odx`, else `~/.cache/odx` —
OUTSIDE the project: the engine still never writes into user source (C3), and
the cache is opt-in (`Engine.load(..., cache=True)`, `[engine] cache = true`).
Never raises: an unreadable/corrupt entry is a miss.
"""

from __future__ import annotations

import hashlib
import os
import pickle
from pathlib import Path

from odx.engine.model.raw import RawModule

_SOURCES = ("parser/python.py", "model/raw.py")


def parser_version() -> str:
    root = Path(__file__).resolve().parent
    h = hashlib.sha1()
    for rel in _SOURCES:
        try:
            h.update((root / rel).read_bytes())
        except OSError:
            h.update(rel.encode())
    return h.hexdigest()[:16]


def default_dir() -> Path:
    env = os.environ.get("ODX_CACHE_DIR")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "odx"


class ParseCache:
    def __init__(self, directory: Path | None = None) -> None:
        self.dir = Path(directory) if directory else default_dir()
        self.version = parser_version()
        self.hits = 0
        self.misses = 0

    def _path(self, file: Path, content_hash: str) -> Path:
        k = hashlib.sha1(f"{self.version}\0{Path(file).resolve()}\0{content_hash}".encode()).hexdigest()
        return self.dir / "py" / k[:2] / f"{k}.pkl"

    def get(self, file: Path, content_hash: str) -> RawModule | None:
        if not content_hash:
            self.misses += 1
            return None
        p = self._path(file, content_hash)
        try:
            with p.open("rb") as fh:
                obj = pickle.load(fh)
        except (OSError, pickle.UnpicklingError, EOFError, AttributeError, ImportError):
            self.misses += 1
            return None
        if not isinstance(obj, RawModule):
            self.misses += 1
            return None
        self.hits += 1
        return obj

    def put(self, file: Path, content_hash: str, value: RawModule) -> None:
        if not content_hash:
            return
        p = self._path(file, content_hash)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            with tmp.open("wb") as fh:
                pickle.dump(value, fh, protocol=pickle.HIGHEST_PROTOCOL)
            tmp.replace(p)
        except OSError:
            pass                                     # a cache that can't write is a no-op
