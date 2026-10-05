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
import re

from odx.engine.model.raw import (
    Locator,
    RawRecord,
    RawRecordField,
    RawView,
    RawXmlFile,
    XmlIdRef,
)

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


# --------------------------------------------------------------------------- #
# generic records + the xml-id graph (RFC §7). Every data-file declaration is a
# RawRecord; every xml-id USE is an XmlIdRef. Views stay RawViews too (above).
# --------------------------------------------------------------------------- #
RECORD_TAGS = ("record", "menuitem", "template", "report", "act_window",
               "function", "delete")
_REF_CALL = re.compile(r"""\bref\(\s*['"]([\w.]+)['"]\s*\)""")
_INTERP = re.compile(r"%\(([\w.]+)\)[ds]")
_TCALL = re.compile(r"^[\w]+\.[\w.]+$|^[\w]+$")
# implicit external ids that are never declared in data files
_IGNORED_PREFIXES = ("__export__.", "__import__.")


def _is_noupdate(el: etree._Element) -> bool:
    cur = el.getparent()
    while cur is not None:
        if cur.tag in ("data", "odoo", "openerp") and cur.get("noupdate") in ("1", "True", "true"):
            return True
        cur = cur.getparent()
    return False


def _record_from_el(el: etree._Element, module: str, file: Path) -> RawRecord:
    tag = el.tag
    xid = el.get("id")
    rec = RawRecord(tag=tag, module=module, location=_loc(el, file),
                    xmlid=_norm_ref(xid, module) if xid else None,
                    attrs={k: v for k, v in el.attrib.items() if isinstance(k, str)},
                    noupdate=_is_noupdate(el))
    rec.element = el
    if tag == "record":
        rec.model = el.get("model")
        for fld in el.findall("field"):
            name = fld.get("name")
            if not name:
                continue
            ref = fld.get("ref")
            rf = RawRecordField(
                name=name, location=_loc(fld, file),
                text=(fld.text or "").strip() if len(fld) == 0 else None,
                ref=_norm_ref(ref, module) if ref else None,
                eval=fld.get("eval"))
            rf.element = fld
            rec.fields[name] = rf
    elif tag == "menuitem":
        rec.model = "ir.ui.menu"
        parent = el.getparent()
        if parent is not None and parent.tag == "menuitem" and parent.get("id") \
                and "parent" not in rec.attrs:
            rec.attrs["parent"] = parent.get("id")
    elif tag == "template":
        rec.model = "ir.ui.view"
    elif tag == "report":
        rec.model = "ir.actions.report"
    elif tag == "act_window":
        rec.model = "ir.actions.act_window"
    elif tag in ("function", "delete"):
        rec.model = el.get("model")
    return rec


def _add_ref(out: list[XmlIdRef], raw: str | None, module: str, el: etree._Element,
             file: Path, context: str, record_model: str | None) -> None:
    if not raw:
        return
    raw = raw.strip()
    if not raw or raw.startswith(_IGNORED_PREFIXES) or not re.match(r"^[\w.]+$", raw):
        return
    out.append(XmlIdRef(xmlid=_norm_ref(raw, module), module=module,
                        location=_loc(el, file), context=context,
                        record_model=record_model))


def _refs_from_root(root: etree._Element, module: str, file: Path) -> list[XmlIdRef]:
    """Every xml-id use in the file. Walks every element once; the containing
    record's model is tracked so a ref can be attributed (e.g. a `groups_id`
    ref inside a `res.users` record)."""
    out: list[XmlIdRef] = []

    def record_model_of(el: etree._Element) -> str | None:
        cur = el
        while cur is not None:
            if cur.tag == "record":
                return cur.get("model")
            if cur.tag in ("menuitem",):
                return "ir.ui.menu"
            if cur.tag == "template":
                return "ir.ui.view"
            cur = cur.getparent()
        return None

    for el in root.iter():
        tag = el.tag
        if not isinstance(tag, str):
            continue
        rmodel = None
        if tag in ("field", "value"):
            ref = el.get("ref")
            if ref:
                rmodel = record_model_of(el)
                _add_ref(out, ref, module, el, file, f"field:{el.get('name') or ''}", rmodel)
        if tag == "menuitem":
            _add_ref(out, el.get("parent"), module, el, file, "menuitem:parent", "ir.ui.menu")
            _add_ref(out, el.get("action"), module, el, file, "menuitem:action", "ir.ui.menu")
        if tag == "template":
            _add_ref(out, el.get("inherit_id"), module, el, file, "template:inherit_id", "ir.ui.view")
        if tag == "report":
            pass
        for attr, val in el.attrib.items():
            if not isinstance(attr, str) or not val:
                continue
            if attr == "groups":
                for g in val.split(","):
                    g = g.strip().lstrip("!-").strip()
                    _add_ref(out, g, module, el, file, "groups",
                             rmodel or record_model_of(el))
            elif attr == "t-call":
                if _TCALL.match(val.strip()):
                    _add_ref(out, val, module, el, file, "t-call", "ir.ui.view")
            elif attr == "eval" or attr in ("domain", "context", "filter_domain"):
                for x in _REF_CALL.findall(val):
                    _add_ref(out, x, module, el, file,
                             f"eval:{el.get('name') or attr}", rmodel or record_model_of(el))
            if "%(" in val:
                for x in _INTERP.findall(val):
                    _add_ref(out, x, module, el, file, "interpolation",
                             rmodel or record_model_of(el))
        # %(x)d inside text (e.g. <field name="arch"> payloads rarely; action
        # names in domain text bodies)
        if tag == "field" and el.text and "%(" in el.text and len(el) == 0:
            for x in _INTERP.findall(el.text):
                _add_ref(out, x, module, el, file, "interpolation", record_model_of(el))
        if tag == "field" and el.text and "ref(" in el.text and len(el) == 0 \
                and el.get("name") in ("domain", "domain_force", "context", "code"):
            for x in _REF_CALL.findall(el.text):
                _add_ref(out, x, module, el, file, f"eval:{el.get('name')}", record_model_of(el))
    return out


def _records_from_root(root: etree._Element, module: str, file: Path) -> list[RawRecord]:
    out: list[RawRecord] = []
    for el in root.iter(*RECORD_TAGS):
        # a <field> named e.g. "report" is not a record; only real declarations
        parent = el.getparent()
        if el.tag in ("template", "report", "function", "delete", "menuitem", "act_window"):
            # only top-level declarations (direct child of <odoo>/<data>/<openerp>)
            # (menuitems may nest: a child menuitem's parent is implicit)
            allowed = ("odoo", "data", "openerp") + (("menuitem",) if el.tag == "menuitem" else ())
            if parent is not None and parent.tag not in allowed:
                continue
        elif el.tag == "record" and parent is not None and parent.tag not in ("odoo", "data", "openerp"):
            continue
        out.append(_record_from_el(el, module, file))
    return out


def _fill(result: RawXmlFile, root: etree._Element, module: str, file: Path) -> None:
    result.views = _views_from_root(root, module, file)
    result.records = _records_from_root(root, module, file)
    result.xmlid_refs = _refs_from_root(root, module, file)


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
    _fill(result, root, module, file)
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
    _fill(result, root, module, path)
    return result
