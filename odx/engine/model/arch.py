"""Effective-arch builder — the UNIT of view resolution (Phase 0.6 finding).

The spike proved you cannot resolve a locator by walking a view's own ancestor
chain. Odoo builds a view's *effective arch* by applying, to the base view, ALL
views that inherit it — the whole inheriting cohort — in module load order, then
`sequence`. Only against that effective arch do a view's own field references and
a child view's locators resolve. Testing against the full applied result lifted
the spike's locator resolution 84.5% -> 97.29% and collapsed "hard" 644 -> 105.

Consequences baked into this design:
  - The unit of resolution is "base view + its inheriting cohort applied in
    order", not "this view + its parents".
  - ORDER IS CORRECTNESS: application order = ModuleGraph.load_order() (then
    per-view `sequence`). Wrong order => missed override => false
    `xpath-target-missing`. Hence this consumes `odx.engine.loadorder` directly.
  - Locators MUST be evaluated with the Odoo XPath dialect registered
    (odx.engine.model.xpath.register_dialect) or matches silently fail.

Produces one effective arch per (primary) view, with provenance on each node:
which module/inheritance contributed it (powers `why` on view nodes + §8
invalidation).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from odx.engine.contracts import Resolution
from odx.engine.loadorder import ModuleGraph
from odx.engine.model.raw import RawView


@dataclass
class EffectiveView:
    xmlid: str
    model: str | None
    arch = None                        # fully-applied arch element
    resolution: Resolution = Resolution.STATIC
    # per-node provenance filled during application:
    contributors: list[str] = field(default_factory=list)   # xmlids applied, in order
    unresolved_locators: list = field(default_factory=list)  # -> Phase 4 Diagnostics


class EffectiveArchBuilder:
    """Applies inheriting-view cohorts to bases, in load order.

    Registry-wide: give it every RawView and the ModuleGraph; it groups by base,
    orders each cohort, applies `xpath`/`position` directives, and returns the
    effective views. The residue (locators that still don't match after correct,
    complete application) is genuine and becomes Phase 4 Diagnostics — never
    before this is complete (docs/PHASE3.md, applicator-before-analyzer).
    """

    def __init__(self, graph: ModuleGraph) -> None:
        self.graph = graph
        # ensure Odoo's XPath dialect is active before any locator eval
        from odx.engine.model import xpath
        xpath.register_dialect()

    def build(self, views: list[RawView]) -> dict[str, EffectiveView]:
        raise NotImplementedError(
            "Phase 3 — group by base, order by graph.load_order()+sequence, apply"
        )

    def _cohort_order(self, base_xmlid: str, cohort: list[RawView]) -> list[RawView]:
        """Order an inheriting cohort by (module load order, then sequence)."""
        rank = {name: i for i, name in enumerate(self.graph.load_order())}
        return sorted(cohort, key=lambda v: (rank.get(v.module, 1 << 30),
                                             getattr(v, "sequence", 16)))
