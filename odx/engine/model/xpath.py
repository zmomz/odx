"""Odoo's XPath dialect — a completeness GATE, not a nice-to-have (Phase 0.6).

Odoo view locators use an XPath *dialect*, not stock XPath 1.0. The Phase 0.6
spike found 332 of 335 initial "failures" were one missing extension function
(`hasclass`). That is the dangerous failure mode in miniature: a gap in our
XPath dialect does not error — it silently fails to match, producing a false
`view.xpath-target-missing` that blames the *user's* code for *our* limitation.

So this module is a gate with two hard rules:
  1. The dialect must be provably complete: every Odoo XPath extension is either
     implemented here or explicitly listed as unsupported. `assert_complete()`
     makes the first half testable; `unsupported_functions()` the second.
  2. An unimplemented construct must ANNOUNCE ITSELF as an engine limitation
     (via `unsupported_functions()` -> `Resolution.DYNAMIC`), never degrade to a
     silent no-match. The `view.xpath-target-missing` analyzer (Phase 4) must not
     run against results produced with an incomplete dialect (see docs/PHASE3.md
     "applicator-before-analyzer").

**Audit (Phase 3, live corpus).** Sorting every function name that appears in a
real inheritance locator across the corpus surfaced exactly one non-XPath-1.0
name: `hasclass`. Odoo registers no other custom XPath function for view
inheritance (it uses stock XPath 1.0 plus `hasclass`; see
`odoo/addons/base/models/ir_ui_view.py`). So the known dialect is XPath 1.0 +
`hasclass`, and it is complete. `ODOO_XPATH_FUNCTIONS` is kept authoritative so a
future addition is a one-line change that the gate test immediately enforces.

Also handles data-file id interpolation `%(xmlid)d` / `%(xmlid)s`, which must be
resolved to record ids before an expr is evaluated (Phase 0.6 straggler; view
backlog item 3).
"""

from __future__ import annotations

import re
from lxml import etree

# The known surface of Odoo's XPath dialect beyond XPath 1.0. Authoritative:
# every entry is either "implemented" (registered by register_dialect and asserted
# by assert_complete) or "unsupported" (surfaced by unsupported_functions ->
# DYNAMIC, never a silent no-match). Audited against Odoo's ir_ui_view usage.
ODOO_XPATH_FUNCTIONS: dict[str, str] = {
    "hasclass": "implemented",       # node has all given CSS classes
}

# Stock XPath 1.0 function library — used to tell "a real Odoo extension we must
# implement" apart from "an ordinary XPath function lxml already provides". A name
# here is never flagged unsupported (the old list was partial — missing e.g.
# `name`, `local-name`, `floor` — which would false-flag ordinary locators).
XPATH_1_0_FUNCTIONS: frozenset[str] = frozenset({
    # node-set
    "last", "position", "count", "id", "local-name", "namespace-uri", "name",
    # string
    "string", "concat", "starts-with", "contains", "substring-before",
    "substring-after", "substring", "string-length", "normalize-space",
    "translate",
    # boolean
    "boolean", "not", "true", "false", "lang",
    # number
    "number", "sum", "floor", "ceiling", "round",
    # operators that regex-match the `name(` shape but are XPath keywords
    "and", "or", "text", "node", "comment", "processing-instruction", "div", "mod",
})

# `%(module.record)d` / `%(module.record)s` id interpolation in data files.
_INTERP = re.compile(r"%\((?P<xmlid>[\w.]+)\)[ds]")

# names used as a function call in an expr: `foo(` (captures `foo`)
_FN_CALL = re.compile(r"([A-Za-z_][\w-]*)\s*\(")


def _implementations() -> dict:
    """The extension functions register_dialect installs. Single source of truth:
    both register_dialect and assert_complete read this, so the registered set and
    the 'implemented' catalog can never silently diverge."""

    def hasclass(context, *classes):
        node = context.context_node
        node_classes = frozenset((node.get("class") or "").split())
        return all(c in node_classes for c in classes)

    return {"hasclass": hasclass}


def register_dialect() -> None:
    """Register Odoo's XPath extension functions on lxml's default namespace.

    Call once before evaluating any view locator. Proven in the Phase 0.6 spike
    (`tests/spike/run_xml.py`) to lift locator resolution 77% -> 84.5%.
    """
    ns = etree.FunctionNamespace(None)
    for name, fn in _implementations().items():
        ns[name] = fn


def implemented_functions() -> list[str]:
    """Dialect extensions marked implemented in the catalog."""
    return sorted(n for n, s in ODOO_XPATH_FUNCTIONS.items() if s == "implemented")


def registered_functions() -> list[str]:
    """Dialect extensions register_dialect actually installs."""
    return sorted(_implementations())


def assert_complete() -> None:
    """The completeness GATE (rule 1), callable from a test.

    Fails if the catalog and the implementations disagree in EITHER direction:
      - an entry marked "implemented" that register_dialect doesn't install
        (a silent no-match waiting to happen), or
      - a function register_dialect installs that the catalog doesn't record as
        "implemented" (drift the audit wouldn't catch), or
      - a catalog value that is neither "implemented" nor "unsupported".
    """
    for name, status in ODOO_XPATH_FUNCTIONS.items():
        if status not in ("implemented", "unsupported"):
            raise AssertionError(
                f"dialect entry {name!r} has unknown status {status!r} "
                "(must be 'implemented' or 'unsupported')")
    impl = set(implemented_functions())
    reg = set(registered_functions())
    if impl != reg:
        raise AssertionError(
            "XPath dialect incomplete: catalog 'implemented' set "
            f"{sorted(impl)} != registered set {sorted(reg)}")


def resolve_interpolation(expr: str, xmlid_to_id) -> str:
    """Replace `%(xmlid)d/s` with resolved record ids before evaluation.

    `xmlid_to_id` maps a full xml-id to its numeric id (or a stable placeholder
    in pure-static mode). Unknown xml-ids are left for the caller to flag rather
    than silently mismatched.
    """
    return _INTERP.sub(
        lambda m: str(xmlid_to_id(m.group("xmlid"))), expr
    )


def normalize_interpolation(text: str, module: str) -> str:
    """Canonicalize `%(id)d` -> `%(module.id)d` (Odoo resolves both spellings to
    the same record id). Applied to arch attribute values AND locator exprs, the
    same xml-id becomes the same literal on both sides — so a locator matches
    exactly when Odoo's id substitution would make it match, with no id table."""
    return _INTERP.sub(
        lambda m: m.group(0) if "." in m.group("xmlid")
        else m.group(0).replace(f"%({m.group('xmlid')})", f"%({module}.{m.group('xmlid')})"),
        text)


def has_interpolation(expr: str | None) -> bool:
    """Whether an expr carries a `%(xmlid)d/s` reference (must be resolved before
    evaluation; view backlog item 3)."""
    return bool(expr) and _INTERP.search(expr) is not None


def unsupported_functions(expr: str | None) -> list[str]:
    """Names used in `expr` that are NOT in our known dialect (rule 2).

    "Known" = XPath 1.0 built-ins plus dialect entries marked "implemented". A
    dialect entry marked "unsupported", or a name from nowhere, IS returned. A
    non-empty result means: do not trust a no-match as 'target missing' — this is
    an engine limitation to surface (-> Resolution.DYNAMIC), not the user's bug.
    """
    # function names only — never text inside string literals (a shorthand
    # locator carries attribute values like [@t-if='len(lines)'])
    bare = re.sub(r"'[^']*'|\"[^\"]*\"", "''", expr or "")
    used = set(_FN_CALL.findall(bare))
    known = XPATH_1_0_FUNCTIONS | set(implemented_functions())
    return sorted(n for n in used if n not in known)
