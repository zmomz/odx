"""Contract -> JSON (shared by every client: CLI --format json, MCP tools, CI).

The §10 contracts are frozen dataclasses of plain data, so serialization is a
mechanical walk: dataclasses -> dicts, Enums -> values, Paths -> strings. No
client re-derives meaning here (RFC §13: clients render, they don't analyze).
"""

from __future__ import annotations

import dataclasses
import json
from enum import Enum
from pathlib import Path
from typing import Any


def to_jsonable(obj: Any, *, relative_to: Path | None = None) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name), relative_to=relative_to)
                for f in dataclasses.fields(obj)}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Path):
        if relative_to is not None:
            try:
                return str(obj.resolve().relative_to(relative_to.resolve()))
            except ValueError:
                return str(obj)
        return str(obj)
    if isinstance(obj, dict):
        return {str(to_jsonable(k, relative_to=relative_to)): to_jsonable(v, relative_to=relative_to)
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_jsonable(x, relative_to=relative_to) for x in obj]
    return obj


def dumps(obj: Any, *, relative_to: Path | None = None, indent: int | None = 2) -> str:
    return json.dumps(to_jsonable(obj, relative_to=relative_to), indent=indent,
                      ensure_ascii=False)
