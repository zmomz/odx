"""Phase 0.6 spike parser: Odoo XML data files -> view records + inheritance
locators, via lxml.

Focus is view inheritance (RFC §7 index concern, unmeasured by the Phase 0.5
Python spike): `<record model="ir.ui.view">` with an `inherit_id`, and the arch's
`xpath`/`position` locators. Records the xml-ids needed to resolve `inherit_id`
across modules, and every inheritance locator so the resolver can test whether
it selects a real node in the parent arch.

Never crashes on a file: a malformed XML file becomes a recorded failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from pathlib import Path

from lxml import etree

VIEW_MODEL = "ir.ui.view"
# positional inheritance verbs Odoo understands on a locator element
POSITIONS = {"after", "before", "inside", "replace", "attributes", "move"}


@dataclass
class Locator:
    """One inheritance directive inside an inheriting view's arch."""
    kind: str                 # "xpath" | "shorthand"
    expr: str | None          # xpath expression (given, or synthesized)
    position: str | None
    line: int
    synthesized: bool = False  # expr we built from a shorthand element
    unresolvable_expr: bool = False  # xpath present but we couldn't build/eval it


@dataclass
class ViewRecord:
    xmlid: str | None          # full "module.id"
    module: str
    file: Path
    line: int
    model: str | None = None
    inherit_ref: str | None = None   # normalized full xmlid of parent, if literal
    inherit_dynamic: bool = False    # inherit_id present but not a plain ref=
    mode: str | None = None          # primary | extension (Odoo default)
    is_template: bool = False        # <template> QWeb view
    arch = None                      # lxml element (the arch root), or None
    locators: list[Locator] = dc_field(default_factory=list)

    @property
    def is_inheriting(self) -> bool:
        return self.inherit_ref is not None or self.inherit_dynamic


@dataclass
class XmlFileResult:
    file: Path
    module: str
    views: list[ViewRecord] = dc_field(default_factory=list)
    failure: str | None = None


def _norm_ref(ref: str, module: str) -> str:
    """Normalize an xml-id ref to full `module.id` form."""
    return ref if "." in ref else f"{module}.{ref}"


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
    # fall back to any single distinguishing attribute
    for attr in ("id", "t-name", "class"):
        v = el.get(attr)
        if v is not None:
            return f"//{tag}[@{attr}='{v}']"
    return f"//{tag}"


def _extract_locators(arch: etree._Element) -> list[Locator]:
    locators: list[Locator] = []
    if arch is None:
        return locators
    for el in arch.iter():
        tag = el.tag
        if not isinstance(tag, str):
            continue
        if tag == "xpath":
            expr = el.get("expr")
            locators.append(Locator(
                kind="xpath", expr=expr, position=el.get("position"),
                line=el.sourceline or 0, unresolvable_expr=expr is None,
            ))
        elif el.get("position") in POSITIONS:
            expr = _synth_xpath(el)
            locators.append(Locator(
                kind="shorthand", expr=expr, position=el.get("position"),
                line=el.sourceline or 0, synthesized=True,
                unresolvable_expr=expr is None,
            ))
    return locators


def _view_from_record(rec: etree._Element, module: str, file: Path) -> ViewRecord:
    xid = rec.get("id")
    vr = ViewRecord(
        xmlid=_norm_ref(xid, module) if xid else None,
        module=module, file=file, line=rec.sourceline or 0,
    )
    for fld in rec.findall("field"):
        fname = fld.get("name")
        if fname == "model":
            vr.model = (fld.text or fld.get("eval") or "").strip() or None
        elif fname == "inherit_id":
            ref = fld.get("ref")
            if ref:
                vr.inherit_ref = _norm_ref(ref, module)
            elif fld.get("eval") is not None:
                vr.inherit_dynamic = True
        elif fname == "mode":
            vr.mode = (fld.text or "").strip() or None
        elif fname == "arch":
            vr.arch = fld
            vr.locators = _extract_locators(fld)
    return vr


def _view_from_template(tpl: etree._Element, module: str, file: Path) -> ViewRecord:
    """<template ...> is sugar for an ir.ui.view record."""
    tid = tpl.get("id")
    vr = ViewRecord(
        xmlid=_norm_ref(tid, module) if tid else None,
        module=module, file=file, line=tpl.sourceline or 0, is_template=True,
    )
    inh = tpl.get("inherit_id")
    if inh:
        vr.inherit_ref = _norm_ref(inh, module)
    vr.arch = tpl
    vr.locators = _extract_locators(tpl)
    return vr


def parse_xml_file(path: Path, module: str) -> XmlFileResult:
    result = XmlFileResult(file=path, module=module)
    try:
        parser = etree.XMLParser(recover=True, resolve_entities=False)
        tree = etree.parse(str(path), parser)
    except (etree.XMLSyntaxError, OSError) as e:
        result.failure = f"{type(e).__name__}: {e}"
        return result
    root = tree.getroot()
    if root is None:
        result.failure = "empty or unparseable document"
        return result

    for rec in root.iter("record"):
        if rec.get("model") == VIEW_MODEL:
            result.views.append(_view_from_record(rec, module, path))
    for tpl in root.iter("template"):
        result.views.append(_view_from_template(tpl, module, path))
    return result
