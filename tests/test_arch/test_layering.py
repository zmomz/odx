"""Architecture tests (RFC C6, C7, §20).

The physical package split *is* the architecture; these tests fail the build on
violation. They parse source statically (via `ast`) so they don't need the
imported packages to be installed.

Enforced rules:
  1. engine/ imports nothing from odx.runtime or odx.clients        (C6)
  2. engine/ imports no client/runtime frameworks (typer, rich)   (C6)
  3. runtime/ imports nothing from odx.clients                     (C7 direction)
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ODX_ROOT = REPO_ROOT / "odx"
ENGINE = ODX_ROOT / "engine"
RUNTIME = ODX_ROOT / "runtime"

FORBIDDEN_FRAMEWORKS = {"typer", "rich", "click"}


def _iter_py(root: Path):
    yield from root.rglob("*.py")


def _imported_modules(path: Path) -> set[str]:
    """Return the top-of-import module strings referenced by a source file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                mods.add(node.module)
    return mods


def _violations(root: Path, predicate) -> list[str]:
    bad: list[str] = []
    for py in _iter_py(root):
        for mod in _imported_modules(py):
            if predicate(mod):
                rel = py.relative_to(REPO_ROOT)
                bad.append(f"{rel}: imports '{mod}'")
    return bad


def test_engine_does_not_import_runtime_or_clients():
    bad = _violations(
        ENGINE,
        lambda m: m.startswith("odx.runtime") or m.startswith("odx.clients"),
    )
    assert not bad, "engine must not import runtime/clients:\n" + "\n".join(bad)


def test_engine_does_not_import_client_frameworks():
    bad = _violations(
        ENGINE,
        lambda m: m.split(".")[0] in FORBIDDEN_FRAMEWORKS,
    )
    assert not bad, "engine must stay framework-free:\n" + "\n".join(bad)


def test_runtime_does_not_import_clients():
    if not RUNTIME.exists():
        pytest.skip("no runtime package yet")
    bad = _violations(RUNTIME, lambda m: m.startswith("odx.clients"))
    assert not bad, "runtime must not import clients:\n" + "\n".join(bad)


def test_engine_package_exists():
    assert ENGINE.is_dir(), "engine package must exist"
    assert (ENGINE / "contracts.py").is_file()
    assert (ENGINE / "api.py").is_file()
