"""Odoo's XPath dialect — a completeness GATE, not a nice-to-have (Phase 0.6).

Odoo view locators use an XPath *dialect*, not stock XPath 1.0. The Phase 0.6
spike found 332 of 335 initial "failures" were one missing extension function
(`hasclass`). That is the dangerous failure mode in miniature: a gap in our
XPath dialect does not error — it silently fails to match, producing a false
`view.xpath-target-missing` that blames the *user's* code for *our* limitation.

So this module is a gate with two hard rules:
  1. The dialect must be provably complete: every Odoo XPath extension is either
     implemented here or explicitly listed as unsupported.
  2. An unimplemented construct must ANNOUNCE ITSELF as an engine limitation
     (Resolution.DYNAMIC / a distinct marker), never degrade to a silent
     no-match. The `view.xpath-target-missing` analyzer (Phase 4) must not run
     against results produced with an incomplete dialect (see docs/PHASE3.md
     "applicator-before-analyzer").

Also handles data-file id interpolation `%(xmlid)d` / `%(xmlid)s`, which must be
resolved to record ids before an expr is evaluated (Phase 0.6 straggler).
"""

from __future__ import annotations

import re
from lxml import etree

# The known surface of Odoo's XPath dialect. Keep this list authoritative:
# audit against Odoo source per target version and mark each implemented or not.
# (Sourced initially from Odoo's `base.models.ir_ui_view` xpath usage.)
ODOO_XPATH_FUNCTIONS = {
    "hasclass": "implemented",       # node has all given CSS classes
    # candidates to audit & implement/mark per version:
    # "hasclass"-adjacent helpers, custom functions registered by other addons...
}

# `%(module.record)d` / `%(module.record)s` id interpolation in data files.
_INTERP = re.compile(r"%\((?P<xmlid>[\w.]+)\)[ds]")


def register_dialect() -> None:
    """Register Odoo's XPath extension functions on lxml's default namespace.

    Call once before evaluating any view locator. Proven in the Phase 0.6 spike
    (`tests/spike/run_xml.py`) to lift locator resolution 77% -> 84.5%.
    """
    ns = etree.FunctionNamespace(None)

    def hasclass(context, *classes):
        node = context.context_node
        node_classes = frozenset((node.get("class") or "").split())
        return all(c in node_classes for c in classes)

    ns["hasclass"] = hasclass


def resolve_interpolation(expr: str, xmlid_to_id) -> str:
    """Replace `%(xmlid)d/s` with resolved record ids before evaluation.

    `xmlid_to_id` maps a full xml-id to its numeric id (or a stable placeholder
    in pure-static mode). Unknown xml-ids are left for the caller to flag rather
    than silently mismatched.
    """
    return _INTERP.sub(
        lambda m: str(xmlid_to_id(m.group("xmlid"))), expr
    )


def unsupported_functions(expr: str) -> list[str]:
    """Names used in `expr` that are NOT in our known dialect.

    A non-empty result means: do not trust a no-match as 'target missing' —
    this is an engine limitation to surface, per rule 2 above.
    """
    used = set(re.findall(r"([A-Za-z_][\w-]*)\s*\(", expr or ""))
    used -= {"and", "or", "not", "text", "contains", "starts-with",
             "last", "position", "count", "normalize-space", "concat",
             "substring", "string", "boolean", "number", "translate"}  # XPath 1.0
    return sorted(n for n in used if n not in ODOO_XPATH_FUNCTIONS)
