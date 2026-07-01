"""XML parser: Odoo data files -> raw view records + inheritance locators,
recording Location (C4).

Phase 3. **Graduated from `tests/spike/xml_parser.py`** (validated in Phase 0.6:
99.98% parse, 99.64% inherit_id resolution). The *extraction logic* is what the
spike validated; the *output layer* is new and mandatory:

  - every RawView / Locator carries a precise `Location` (C4 — retrofitting ranges
    is the expensive-to-reverse mistake it warns against);
  - `mode` is captured and defaulted to primary/extension so the effective-arch
    builder can draw cohort boundaries (a `mode="primary"` view starts its own
    arch tree; see arch.py). Confirmed load-bearing on the live corpus: ignoring
    the boundary lumps unrelated primary trees into one cohort and `replace`
    directives clobber nodes, tanking record-view resolution;
  - `priority` is captured (Odoo's secondary apply-order key after module load
    order);
  - `inherit_id` via `eval=` -> `Resolution.DYNAMIC` (runtime-derived; route to the
    runtime bridge, never guess).

CRITICAL (Phase 0.6 finding): a locator's xpath must be evaluated with Odoo's
XPath *dialect* registered (see odx.engine.model.xpath), not stock XPath 1.0 — a
missing extension function silently fails to match and would produce a false
'xpath-target-missing' against the user's code. This parser only *records*
locators; the builder (arch.py) evaluates them with the dialect active.

Discipline preserved from the spike: never crash on a file — a malformed XML file
becomes `RawXmlFile.parse_error`, not an exception (`recover=True`).
"""

from __future__ import annotations

from pathlib import Path

from lxml import etree

from odx.engine.contracts import Location, Resolution
from odx.engine.model.raw import Locator, RawView, RawXmlFile

VIEW_MODEL = "ir.ui.view"
# positional inheritance verbs Odoo understands on a locator element
POSITIONS = {"after", "before", "inside", "replace", "attributes", "move"}


# --------------------------------------------------------------------------- #
# Location — recorded at parse time (C4). lxml gives us the source line; column
# isn't exposed per-element, so we record line with col 0 (a stable range for a
# squiggle / CI annotation, refined if lxml ever surfaces columns).
# --------------------------------------------------------------------------- #
def _loc(el: etree._Element, file: Path) -> Location:
    line = getattr(el, "sourceline", 0) or 0
    return Location(file, line, 0, line, 0)


def _norm_ref(ref: str, module: str) -> str:
    """Normalize an xml-id ref to full `module.id` form."""
    return ref if "." in ref else f"{module}.{ref}"


def _int_or(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _synth_xpath(el: etree._Element) -> str | None:
    """Best-effort xpath for a shorthand locator element.

    Odoo matches the first element of the same tag with the same identifying
    attributes (name, or otherwise). Mirror that heuristically.
    """
    tag = el.tag
    if not isinstance(tag, str):
        return None
    name = el.get("name")
    if name is not None:
        return f"//{tag}[@name='{name}']"
    for attr in ("id", "t-name", "class"):
        v = el.get(attr)
        if v is not None:
            return f"//{tag}[@{attr}='{v}']"
    return f"//{tag}"


def _extract_locators(arch: etree._Element | None, file: Path) -> list[Locator]:
    """Collect the inheritance directives declared *directly* under an arch.

    Only top-level children are directives (Odoo applies the arch's direct
    children as a spec); nested `xpath` elements inside a directive's payload are
    content, not separate locators. This mirrors the spike, which iterated the
    whole subtree — but scoping to top-level is what the builder needs to apply
    them one at a time in order.
    """
    locators: list[Locator] = []
    if arch is None:
        return locators
    for el in arch:
        tag = el.tag
        if not isinstance(tag, str):            # comment / PI
            continue
        if tag == "xpath":
            expr = el.get("expr")
            locators.append(Locator(
                kind="xpath", expr=expr, position=el.get("position"),
                location=_loc(el, file),
                resolution=Resolution.STATIC if expr else Resolution.UNRESOLVED,
            ))
        elif el.get("position") in POSITIONS:
            expr = _synth_xpath(el)
            locators.append(Locator(
                kind="shorthand", expr=expr, position=el.get("position"),
                location=_loc(el, file),
                resolution=Resolution.STATIC if expr else Resolution.UNRESOLVED,
            ))
    return locators


def _effective_mode(explicit: str | None, inherit_ref: str | None) -> str:
    """Odoo's rule: a view is `primary` iff it declares no `inherit_id`; with an
    `inherit_id` and no explicit mode it defaults to `extension`."""
    if explicit in ("primary", "extension"):
        return explicit
    return "extension" if inherit_ref else "primary"


def _view_from_record(rec: etree._Element, module: str, file: Path) -> RawView:
    xid = rec.get("id")
    vr = RawView(
        xmlid=_norm_ref(xid, module) if xid else None,
        module=module, location=_loc(rec, file),
    )
    explicit_mode = None
    for fld in rec.findall("field"):
        fname = fld.get("name")
        if fname == "model":
            vr.model = (fld.text or fld.get("eval") or "").strip() or None
        elif fname == "inherit_id":
            ref = fld.get("ref")
            if ref:
                vr.inherit_ref = _norm_ref(ref, module)
            elif fld.get("eval") is not None:
                # inherit_id computed at load — runtime-derived, don't guess.
                vr.resolution = Resolution.DYNAMIC
        elif fname == "mode":
            explicit_mode = (fld.text or "").strip() or None
        elif fname == "priority":
            vr.priority = _int_or((fld.text or fld.get("eval") or "").strip(), 16)
        elif fname == "arch":
            vr.arch = fld
            vr.locators = _extract_locators(fld, file)
    vr.mode = _effective_mode(explicit_mode, vr.inherit_ref)
    return vr


def _view_from_template(tpl: etree._Element, module: str, file: Path) -> RawView:
    """`<template ...>` is sugar for an ir.ui.view record (QWeb)."""
    tid = tpl.get("id")
    inh = tpl.get("inherit_id")
    inherit_ref = _norm_ref(inh, module) if inh else None
    # `primary="1"` on a template forces a primary derived view.
    explicit_mode = "primary" if tpl.get("primary") else None
    vr = RawView(
        xmlid=_norm_ref(tid, module) if tid else None,
        module=module, location=_loc(tpl, file), is_template=True,
        inherit_ref=inherit_ref,
        priority=_int_or(tpl.get("priority"), 16),
    )
    vr.mode = _effective_mode(explicit_mode, inherit_ref)
    vr.arch = tpl
    vr.locators = _extract_locators(tpl, file)
    return vr


def _views_from_root(root: etree._Element, module: str, file: Path) -> list[RawView]:
    views: list[RawView] = []
    for rec in root.iter("record"):
        if rec.get("model") == VIEW_MODEL:
            views.append(_view_from_record(rec, module, file))
    for tpl in root.iter("template"):
        views.append(_view_from_template(tpl, module, file))
    return views


def parse_string(src: str, file: Path, module: str) -> RawXmlFile:
    """Parse a data file from a source string (mirrors the Python parser's
    `parse_source` — the in-memory seam unit tests build against). Never raises."""
    result = RawXmlFile(file=file, module=module)
    parser = etree.XMLParser(recover=True, resolve_entities=False)
    try:
        root = etree.fromstring(src.encode("utf-8"), parser)
    except (etree.XMLSyntaxError, ValueError) as e:
        result.parse_error = f"{type(e).__name__}: {e}"
        return result
    if root is None:
        result.parse_error = "empty or unparseable document"
        return result
    result.views = _views_from_root(root, module, file)
    return result


def parse_xml(path: Path, module: str) -> RawXmlFile:
    """Parse one Odoo data file into its view records + inheritance locators.

    Never raises: a malformed file becomes `RawXmlFile.parse_error` (spike
    never-crash discipline, `recover=True`).
    """
    result = RawXmlFile(file=path, module=module)
    try:
        parser = etree.XMLParser(recover=True, resolve_entities=False)
        tree = etree.parse(str(path), parser)
    except (etree.XMLSyntaxError, OSError) as e:
        result.parse_error = f"{type(e).__name__}: {e}"
        return result
    root = tree.getroot()
    if root is None:
        result.parse_error = "empty or unparseable document"
        return result
    result.views = _views_from_root(root, module, path)
    return result
