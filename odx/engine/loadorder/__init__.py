"""loadorder — the module dependency graph & load order (RFC §2, §6).

**The foundation stone of Phase 3.** Phase 0.5 (Python `_inherit`/`_inherits`
provenance) and Phase 0.6 (registry-wide, *ordered* view-arch application) both
depend on knowing the module load order. So this is built first and both the
model resolver and the effective-arch applicator sit on top of it.

Why order is load-bearing (Phase 0.6 finding): Odoo applies field overrides and
view inheritances in module load order (dependency topological order, then
`sequence`). Get the order wrong and a later module's override is missed — the
resolver would report a stale value or a false "xpath-target-missing". So the
order is not a convenience; it is correctness.

This module is intentionally dependency-light (stdlib only): the engine core
must stay framework-free (C6).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST_NAMES = ("__manifest__.py", "__openerp__.py")


@dataclass(frozen=True)
class Manifest:
    name: str                      # module technical name (directory name)
    path: Path                     # module directory
    depends: tuple[str, ...] = ()
    data_files: tuple[str, ...] = ()   # 'data' + 'demo' declared load order
    auto_install: bool = False
    application: bool = False
    installable: bool = True
    raw: dict = field(default_factory=dict)


def parse_manifest(module_dir: Path) -> Manifest | None:
    """Parse a module's manifest dict via ast.literal_eval (no code execution).

    Returns None if the directory has no manifest. Never raises on a malformed
    manifest — returns a best-effort Manifest so one bad module can't sink the
    whole graph (mirrors the spike's never-crash discipline).
    """
    for name in MANIFEST_NAMES:
        mf = module_dir / name
        if mf.is_file():
            break
    else:
        return None

    data: dict = {}
    try:
        data = ast.literal_eval(mf.read_text(encoding="utf-8", errors="replace"))
        if not isinstance(data, dict):
            data = {}
    except (ValueError, SyntaxError):
        data = {}

    depends = tuple(data.get("depends", []) or [])
    files = tuple(data.get("data", []) or []) + tuple(data.get("demo", []) or [])
    return Manifest(
        name=module_dir.name,
        path=module_dir,
        depends=depends,
        data_files=files,
        auto_install=bool(data.get("auto_install", False)),
        application=bool(data.get("application", False)),
        installable=bool(data.get("installable", True)),
        raw=data,
    )


@dataclass
class ModuleGraph:
    """The manifest dependency graph. The single source of truth for load order.

    Both the model resolver (field-override order) and the view applicator
    (inheritance order) query this.
    """
    manifests: dict[str, Manifest] = field(default_factory=dict)

    @classmethod
    def discover(cls, *addons_paths: Path) -> "ModuleGraph":
        g = cls()
        for root in addons_paths:
            for mf_name in MANIFEST_NAMES:
                for mf in Path(root).rglob(mf_name):
                    m = parse_manifest(mf.parent)
                    if m is not None:
                        g.manifests.setdefault(m.name, m)
        return g

    def depends_of(self, module: str) -> tuple[str, ...]:
        m = self.manifests.get(module)
        return m.depends if m else ()

    def dependents_of(self, module: str) -> list[str]:
        """Modules that (directly) depend on `module` — the invalidation frontier
        (RFC §8: a change to `module` dirties these)."""
        return [n for n, m in self.manifests.items() if module in m.depends]

    def missing_dependencies(self) -> dict[str, list[str]]:
        """depends entries not present in the graph (corpus boundary, not error)."""
        out: dict[str, list[str]] = {}
        for n, m in self.manifests.items():
            missing = [d for d in m.depends if d not in self.manifests]
            if missing:
                out[n] = missing
        return out

    def load_order(self) -> list[str]:
        """Topological load order (Kahn), stable within a level by name.

        Missing dependencies are treated as already-satisfied (they live outside
        the known corpus). Cycles — which Odoo forbids — are surfaced by
        `cycles()`; here they're appended deterministically so the list stays
        total rather than silently dropping modules.
        """
        known = set(self.manifests)
        indeg = {n: sum(1 for d in m.depends if d in known)
                 for n, m in self.manifests.items()}
        ready = sorted(n for n, d in indeg.items() if d == 0)
        order: list[str] = []
        while ready:
            n = ready.pop(0)
            order.append(n)
            for dep in sorted(self.dependents_of(n)):
                indeg[dep] -= 1
                if indeg[dep] == 0:
                    ready.append(dep)
            ready.sort()
        if len(order) < len(self.manifests):          # cycle remnant
            order.extend(sorted(set(self.manifests) - set(order)))
        return order

    def cycles(self) -> list[str]:
        """Modules involved in a dependency cycle (should be empty for valid
        Odoo trees). A non-empty result is a real defect worth a Diagnostic."""
        order = set(self.load_order())
        # any module whose deps aren't all orderable before it hints a cycle;
        # a precise SCC pass lands with the real implementation.
        known = set(self.manifests)
        placed: set[str] = set()
        stuck: list[str] = []
        for n in self.load_order():
            if all(d in placed or d not in known for d in self.depends_of(n)):
                placed.add(n)
            else:
                stuck.append(n)
        return stuck
