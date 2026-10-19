"""Effective-arch builder — the UNIT of view resolution (Phase 0.6 finding).

The spike proved you cannot resolve a locator by walking a view's own ancestor
chain. Odoo builds a view's *effective arch* by applying, to the base view, ALL
views that inherit it — the whole inheriting cohort — in module load order, then
`priority`. Only against that effective arch do a view's own field references and
a child view's locators resolve.

Confirmed live before building (docs/PHASE3.md method — the confirm-live-state
step overturned the doc's framing a fourth time): a *correct* primary-aware,
load-ordered applicator resolves 96.39% of record-view locators (2136/2216) on
the corpus — NOT the 97.29% the spike reported, which came from its looser
"target exists anywhere in the ancestor chain" test. The honest residual is
characterized, not hand-waved:
  - 288 QWeb `<template>` locators — own `t-` inheritance mechanism, view backlog
    item 4 (deferred): recorded PARTIAL + `qweb-deferred`, never a defect.
  - 7 `%(xmlid)d` interpolation locators — view backlog item 3 (deferred):
    DYNAMIC + `interpolation-deferred` (announced, not a silent no-match).
  - ~72 genuine cross-tree / dangling record locators — honest UNRESOLVED, the
    Phase-4 `view.xpath-target-missing` candidates (NOT emitted here).
  - 0 dialect gaps beyond `hasclass` (the audit; see xpath.py).

Consequences baked into this design:
  - The unit of resolution is "base view + its inheriting cohort applied in
    order", not "this view + its parents". A `mode="primary"` view starts its own
    tree (it is NOT applied onto its parent's arch) — ignoring that boundary lumps
    unrelated primary trees together and `replace` directives clobber nodes
    (measured: record-view residual 234 -> 79 once the boundary is respected).
  - ORDER IS CORRECTNESS: application order = ModuleGraph.load_order() (then
    per-view `priority`). Wrong order => missed override => false
    `xpath-target-missing`. Hence this consumes `odx.engine.loadorder` directly,
    reusing the same graph the model resolver rides — neither reimplements order.
  - Locators are evaluated with the Odoo XPath dialect registered
    (odx.engine.model.xpath) or matches silently fail; an unimplemented construct
    ANNOUNCES itself (DYNAMIC), never degrades to a silent no-match (constraint 1).

Applicator-before-analyzer (constraint 3): this STOPS at discovering and
representing the effective arch and its `unresolved_locators` as honest state. It
does NOT emit `view.xpath-target-missing` Diagnostics — that is Phase 4, gated on
this being complete (docs/PHASE3.md). The engine returns data, never Diagnostics
(RFC §6).
"""

from __future__ import annotations

import copy
import json
from collections import defaultdict
from dataclasses import dataclass, field

from lxml import etree

from odx.engine.contracts import Location, Resolution
from odx.engine.loadorder import ModuleGraph
from odx.engine.model import xpath
from odx.engine.model.raw import Locator, RawView

# positional inheritance verbs Odoo understands on a locator element
POSITIONS = {"after", "before", "inside", "replace", "attributes", "move"}
_BIG = 1 << 30


@dataclass
class UnresolvedLocator:
    """Honest state for a directive that did not apply — a candidate Phase-4
    Diagnostic, recorded here but NOT accused (RFC §6 layering)."""
    view_xmlid: str | None            # the inheriting view that declared the directive
    reason: str                       # target-not-found | dialect-unsupported:<fns>
                                      # | interpolation-deferred | qweb-deferred
                                      # | xpath-error:<kind> | base-outside-corpus
    resolution: Resolution            # UNRESOLVED (genuine dangle) | DYNAMIC/PARTIAL (announced gap)
    expr: str | None = None
    position: str | None = None
    location: Location | None = None


@dataclass
class EffectiveView:
    xmlid: str
    model: str | None
    arch = None                        # fully-applied arch element (lxml), or None
    resolution: Resolution = Resolution.STATIC
    # per-node provenance filled during application:
    contributors: list[str] = field(default_factory=list)   # xmlids applied, in order
    unresolved_locators: list[UnresolvedLocator] = field(default_factory=list)


class EffectiveArchBuilder:
    """Applies inheriting-view cohorts to bases, in load order.

    Registry-wide: give it every RawView and the ModuleGraph; it groups by root
    (primary-aware), orders each cohort, applies `xpath`/`position` directives,
    and returns one EffectiveView per primary view. The residue (locators that
    still don't match after correct, complete application) is genuine and becomes
    Phase 4 Diagnostics — never before this is complete (docs/PHASE3.md,
    applicator-before-analyzer).
    """

    def __init__(self, graph: ModuleGraph) -> None:
        self.graph = graph
        # ensure Odoo's XPath dialect is active before any locator eval
        xpath.register_dialect()
        self._reg: dict[str, RawView] = {}
        self._rank: dict[str, int] = {}
        self._cohorts: dict[str, list[RawView]] = {}
        self._built: dict[str, EffectiveView] = {}

    # ------------------------------------------------------------------ #
    # public entry
    # ------------------------------------------------------------------ #
    def build(self, views: list[RawView]) -> dict[str, EffectiveView]:
        # registry: first definition of an xmlid wins (mirrors the spike)
        self._reg = {}
        for v in views:
            if v.xmlid:
                self._reg.setdefault(v.xmlid, v)
            tag_provenance(v)
        self._rank = {m: i for i, m in enumerate(self.graph.load_order())}
        self._built = {}

        # cohorts: every extension view grouped under its root (primary/base)
        self._cohorts = defaultdict(list)
        orphans: list[RawView] = []          # extensions whose base is outside the corpus
        for v in self._reg.values():
            if not v.is_inheriting or v.is_primary or v.resolution is Resolution.DYNAMIC:
                continue
            root = self._root_of(v)
            if root is None:
                orphans.append(v)
            else:
                self._cohorts[root.xmlid].append(v)

        # build every primary/base root, in load order so a derived primary's
        # parent root is built before the primary is seeded from it.
        roots = [v for v in self._reg.values() if v.is_primary
                 or v.resolution is Resolution.DYNAMIC]
        for r in sorted(roots, key=self._order_key):
            self._build_root(r.xmlid)

        # extensions whose base isn't in the corpus can't be resolved statically:
        # honest DYNAMIC state keyed by the extension itself (base-outside-corpus).
        for v in orphans:
            self._built.setdefault(v.xmlid, self._boundary_view(v))
        return dict(self._built)

    # ------------------------------------------------------------------ #
    # root building
    # ------------------------------------------------------------------ #
    def _build_root(self, xmlid: str, stack: frozenset[str] = frozenset()) -> EffectiveView | None:
        if xmlid in self._built:
            return self._built[xmlid]
        r = self._reg.get(xmlid)
        if r is None:
            return None
        ev = EffectiveView(xmlid=xmlid, model=r.model)
        self._built[xmlid] = ev            # register early (cycle guard)

        if r.resolution is Resolution.DYNAMIC:      # inherit_id via eval / dynamic
            ev.resolution = Resolution.DYNAMIC
            ev.unresolved_locators.append(UnresolvedLocator(
                view_xmlid=xmlid, reason="dynamic-inherit-id",
                resolution=Resolution.DYNAMIC, location=r.location))
            return ev

        arch = self._seed_arch(r, ev, stack)
        if arch is None:
            return ev                       # boundary/cycle already recorded on ev

        ev.contributors.append(xmlid)
        inactive: list[RawView] = []
        for v in self._cohort_order(xmlid, self._cohorts.get(xmlid, [])):
            if not v.active:
                inactive.append(v)            # optional view: not applied by default
                continue
            self._apply_view(arch, v, ev)
            if v.xmlid:
                ev.contributors.append(v.xmlid)
        self._recheck_in_closure(xmlid, ev)
        # An inactive (optional / customize_show) extension is applied only when
        # a user enables it — and alternatives are often mutually exclusive
        # (website headers). Check each alone against the default tree; a miss
        # is PARTIAL ("depends on which options are enabled"), never accused.
        for v in inactive:
            probe = EffectiveView(xmlid=xmlid, model=ev.model)
            self._apply_view(copy.deepcopy(arch), v, probe)
            for u in probe.unresolved_locators:
                if u.resolution is Resolution.UNRESOLVED:
                    u.resolution = Resolution.PARTIAL
                    u.reason = "inactive-view:" + u.reason
                ev.unresolved_locators.append(u)

        ev.arch = arch
        self._roll_up(ev, is_template=r.is_template)
        return ev

    def _seed_arch(self, r: RawView, ev: EffectiveView,
                   stack: frozenset[str]) -> etree._Element | None:
        """Seed a root's arch: a base copies its own arch; a derived primary
        (mode=primary WITH inherit_id) copies its parent-root's effective arch and
        then applies its own directives onto the copy."""
        if r.inherit_ref and r.mode == "primary":
            parent = self._reg.get(r.inherit_ref)
            proot = self._root_of(parent) if parent is not None else None
            if proot is None:                       # base outside the corpus
                ev.resolution = Resolution.DYNAMIC
                ev.unresolved_locators.append(UnresolvedLocator(
                    view_xmlid=r.xmlid, reason="base-outside-corpus",
                    resolution=Resolution.DYNAMIC, location=r.location))
                return None
            if proot.xmlid in stack:                # inheritance cycle — bail honestly
                ev.resolution = Resolution.UNRESOLVED
                return None
            parent_ev = self._build_root(proot.xmlid, stack | {r.xmlid or ""})
            base_arch = parent_ev.arch if parent_ev else None
            if base_arch is None:
                ev.resolution = Resolution.DYNAMIC
                return None
            arch = copy.deepcopy(base_arch)
            ev.contributors.extend(parent_ev.contributors)
            self._apply_view(arch, r, ev)           # the primary's own spec
            return arch
        if r.arch is None:
            return None
        if r.is_template:
            return _template_arch(r)
        return copy.deepcopy(r.arch)

    def _boundary_view(self, v: RawView) -> EffectiveView:
        ev = EffectiveView(xmlid=v.xmlid, model=v.model,
                           resolution=Resolution.DYNAMIC)
        ev.unresolved_locators.append(UnresolvedLocator(
            view_xmlid=v.xmlid, reason="base-outside-corpus",
            resolution=Resolution.DYNAMIC, location=v.location))
        return ev

    # ------------------------------------------------------------------ #
    # co-install conflicts. The registry-wide tree applies EVERY extension in
    # the corpus — including ones that are never installed together (the
    # localizations all rewriting account.report_invoice_document). Odoo only
    # ever applies the installed ones, so a directive that fails on the global
    # tree is re-checked on the tree as its own module sees it (only cohort
    # members within its dependency closure). If it applies there, the miss is
    # an artifact of co-installing alternatives: PARTIAL, never an accusation.
    # ------------------------------------------------------------------ #
    def _closure(self, module: str) -> set[str]:
        memo = self.__dict__.setdefault("_closure_memo", {})
        if module in memo:
            return memo[module]
        seen: set[str] = set()
        stack = [module, "base", "web"]
        while stack:
            m = stack.pop()
            if m in seen:
                continue
            seen.add(m)
            stack.extend(self.graph.depends_of(m))
        memo[module] = seen
        return seen

    def _scoped_tree(self, xmlid: str, allowed: set[str], stop_before: str | None,
                     depth: int = 0) -> etree._Element | None:
        r = self._reg.get(xmlid)
        if r is None or depth > 20 or r.resolution is Resolution.DYNAMIC:
            return None
        scratch = EffectiveView(xmlid=xmlid, model=r.model)
        if r.inherit_ref and r.mode == "primary":
            parent = self._reg.get(r.inherit_ref)
            proot = self._root_of(parent) if parent is not None else None
            if proot is None or proot.xmlid == xmlid:
                return None
            base = self._scoped_tree(proot.xmlid, allowed, None, depth + 1)
            if base is None:
                return None
            arch = copy.deepcopy(base)
            self._apply_view(arch, r, scratch)
        elif r.arch is None:
            return None
        else:
            arch = _template_arch(r) if r.is_template else copy.deepcopy(r.arch)
        for v in self._cohort_order(xmlid, self._cohorts.get(xmlid, [])):
            if v.xmlid == stop_before:
                break
            if v.active and v.module in allowed:
                self._apply_view(arch, v, scratch)
        return arch

    def _recheck_in_closure(self, root: str, ev: EffectiveView) -> None:
        failing = {u.view_xmlid for u in ev.unresolved_locators
                   if u.resolution is Resolution.UNRESOLVED and u.view_xmlid}
        for vx in failing:
            v = self._reg.get(vx)
            if v is None:
                continue
            allowed = self._closure(v.module)
            stop = None if vx == root else vx
            arch = self._scoped_tree(root, allowed, stop)
            if arch is None:
                continue
            if vx == root:
                ok = all(u.resolution is not Resolution.UNRESOLVED
                         for u in self._probe(arch, v, own_spec_only=True))
            else:
                ok = all(u.resolution is not Resolution.UNRESOLVED for u in self._probe(arch, v))
            if not ok:
                continue
            for u in ev.unresolved_locators:
                if u.view_xmlid == vx and u.resolution is Resolution.UNRESOLVED:
                    u.resolution = Resolution.PARTIAL
                    u.reason = "co-install-conflict:" + u.reason

    def _probe(self, arch, v: RawView, own_spec_only: bool = False) -> list[UnresolvedLocator]:
        scratch = EffectiveView(xmlid=v.xmlid or "", model=v.model)
        if own_spec_only:
            # a derived primary's own spec was applied while seeding: the scoped
            # tree already contains its result; failures would have been recorded
            # by _scoped_tree's scratch, so re-run on a fresh scoped parent
            r = v
            parent = self._reg.get(r.inherit_ref) if r.inherit_ref else None
            proot = self._root_of(parent) if parent is not None else None
            if proot is None:
                return [UnresolvedLocator(v.xmlid, "x", Resolution.UNRESOLVED)]
            base = self._scoped_tree(proot.xmlid, self._closure(v.module), None)
            if base is None:
                return [UnresolvedLocator(v.xmlid, "x", Resolution.UNRESOLVED)]
            self._apply_view(copy.deepcopy(base), v, scratch)
            return scratch.unresolved_locators
        self._apply_view(copy.deepcopy(arch), v, scratch)
        return scratch.unresolved_locators

    # ------------------------------------------------------------------ #
    # directive application
    # ------------------------------------------------------------------ #
    def _apply_view(self, arch: etree._Element, view: RawView, ev: EffectiveView) -> None:
        """Apply each of `view`'s directives onto `arch` in document order,
        recording any that fail to apply as honest unresolved state. Applying at
        each directive's turn (not a lenient re-scan of the final arch) mirrors
        Odoo: a directive that can't find its target when its turn comes is
        genuinely unresolvable, exactly as Odoo would raise."""
        if view.arch is None:
            return
        from odx.engine.parser.xml import spec_elements
        directives = spec_elements(view.arch)
        locs = view.locators
        for i, el in enumerate(directives):
            loc = locs[i] if i < len(locs) else None
            ok, reason = self._apply_directive(arch, el, view.is_template, view.module)
            if not ok:
                self._record(ev, view, loc, el, reason)

    def _apply_directive(self, arch: etree._Element, spec: etree._Element,
                         is_template: bool, spec_module: str | None = None) -> tuple[bool, str]:
        if spec.tag == "xpath":
            expr = spec.get("expr")
            position = spec.get("position") or "inside"
        else:
            expr = _synth_xpath(spec)
            position = spec.get("position") or "inside"

        if not expr:
            return False, "no-expr"
        # `%(xmlid)d` -> canonical literal, matching the canonicalized arch values
        if xpath.has_interpolation(expr):
            expr = xpath.normalize_interpolation(expr, spec_module or "")
        gaps = xpath.unsupported_functions(expr)
        if gaps:
            return False, "dialect-unsupported:" + ",".join(gaps)

        targets, err = _find_targets(arch, expr)
        if err:
            return False, err
        if not targets:
            return False, "target-not-found"

        _mutate(targets[0], spec, position, arch)
        return True, ""

    def _record(self, ev: EffectiveView, view: RawView, loc: Locator | None,
                spec: etree._Element, reason: str) -> None:
        head = reason.split(":", 1)[0]
        if head in ("interpolation-deferred", "dialect-unsupported"):
            res = Resolution.DYNAMIC          # announced engine limitation
        elif head == "qweb-deferred":
            res = Resolution.PARTIAL          # improvable (not-yet), not a defect
        else:                                 # target-not-found | xpath-error | no-expr
            res = Resolution.UNRESOLVED       # genuine dangle -> Phase-4 candidate
        expr = loc.expr if loc else spec.get("expr")
        pos = loc.position if loc else spec.get("position")
        location = loc.location if loc else None
        ev.unresolved_locators.append(UnresolvedLocator(
            view_xmlid=view.xmlid, reason=reason, resolution=res,
            expr=expr, position=pos, location=location))
        if loc is not None:
            loc.resolution = res

    def _roll_up(self, ev: EffectiveView, is_template: bool) -> None:
        """The effective arch's confidence: STATIC when everything applied; else
        the worst honest state among its unresolved locators. A template we don't
        yet fully model is PARTIAL (improvable), never a false STATIC."""
        worst = Resolution.STATIC
        order = {Resolution.STATIC: 0, Resolution.PARTIAL: 1,
                 Resolution.DYNAMIC: 2, Resolution.UNRESOLVED: 3}
        for u in ev.unresolved_locators:
            if order[u.resolution] > order[worst]:
                worst = u.resolution
        ev.resolution = worst

    # ------------------------------------------------------------------ #
    # cohort membership & ordering
    # ------------------------------------------------------------------ #
    def _root_of(self, v: RawView) -> RawView | None:
        """The nearest ancestor that is a base (no inherit_id) or a primary view.
        A primary view starts its own tree; extensions attach to the primary/base
        above them. None if a ref along the way is outside the corpus (boundary)."""
        cur = v
        seen: set[str] = set()
        while cur.inherit_ref and not cur.is_primary and (cur.xmlid or "") not in seen:
            seen.add(cur.xmlid or "")
            nxt = self._reg.get(cur.inherit_ref)
            if nxt is None:
                return None                   # base outside the corpus
            cur = nxt
        return cur

    def _order_key(self, v: RawView) -> tuple[int, int, str]:
        return (self._rank.get(v.module, _BIG), v.priority, v.xmlid or "")

    def _cohort_order(self, base_xmlid: str, cohort: list[RawView]) -> list[RawView]:
        """Order an inheriting cohort by (module load order, then priority)."""
        return sorted(cohort, key=self._order_key)


# --------------------------------------------------------------------------- #
# Per-node provenance. Every element of a raw arch is stamped (in place, once)
# with the view that declared it and its source line, so after application the
# effective arch still knows, node by node, which file:line contributed it —
# the index (RFC §7) attributes a `<field>` inside an inherited payload to the
# inheriting view, in the correct nested model context. The attributes use an
# `odx-` prefix no Odoo locator matches on.
# --------------------------------------------------------------------------- #
SRC_ATTR = "odx-src"
LINE_ATTR = "odx-line"
ATTR_SRC_PREFIX = "odx-asrc-"
ATTR_LINE_PREFIX = "odx-aline-"
TOKEN_PREFIX = "odx-atok-"


def _token_map(el, attr: str) -> dict[str, str]:
    raw = el.get(TOKEN_PREFIX + attr)
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def name_origin(el, attr: str, name: str) -> str | None:
    """For an attribute assembled from several views' `add=` parts, the view
    that contributed the part reading `name` (None: no per-part provenance)."""
    import re
    for token, src in _token_map(el, attr).items():
        if name in re.findall(r"[A-Za-z_]\w*", token):
            return src or None
    return None


def tag_provenance(view: RawView) -> None:
    arch = view.arch
    if arch is None or not view.xmlid:
        return
    if arch.get(SRC_ATTR) is not None:
        return
    module = view.module
    for el in arch.iter():
        if isinstance(el.tag, str):
            # canonical `%(module.id)d` so locators can match interpolated values
            for k, v in el.attrib.items():
                if "%(" in v:
                    el.set(k, xpath.normalize_interpolation(v, module))
            el.set(SRC_ATTR, view.xmlid)
            el.set(LINE_ATTR, str(el.sourceline or 0))


def node_origin(el, attr: str | None = None) -> tuple[str | None, int]:
    """(declaring view xmlid, source line) of an effective-arch node — or, with
    `attr`, of that attribute's value when an extension rewrote it."""
    if attr is not None and el.get(ATTR_SRC_PREFIX + attr) is not None:
        try:
            line = int(el.get(ATTR_LINE_PREFIX + attr) or 0)
        except ValueError:
            line = 0
        return el.get(ATTR_SRC_PREFIX + attr), line
    try:
        line = int(el.get(LINE_ATTR) or 0)
    except ValueError:
        line = 0
    return el.get(SRC_ATTR), line


# --------------------------------------------------------------------------- #
# lxml helpers — locate & mutate. Kept module-level (pure, testable) and dialect-
# aware via the caller registering register_dialect() first.
# --------------------------------------------------------------------------- #
def _scoped(expr: str) -> str:
    """Scope an absolute path to the arch subtree (Odoo: 'within this view'):
    `//x` (any number of leading slashes >= 2) -> `.//x`; `/x` -> `./x`."""
    if expr.startswith("//"):
        return ".//" + expr.lstrip("/")
    if expr.startswith("/"):
        return "." + expr
    return expr


def _template_arch(r: RawView) -> etree._Element:
    """A `<template id=x>` becomes the arch `<t t-name="x">…content…</t>` (Odoo's
    convert.py), held in the same `<field name="arch">` container records use,
    so `//t//table`-style locators see the real root."""
    container = etree.Element("field", name="arch")
    root = etree.SubElement(container, "t")
    root.set("t-name", r.xmlid or "")
    src = r.arch
    if src.get(SRC_ATTR):
        root.set(SRC_ATTR, src.get(SRC_ATTR))
        root.set(LINE_ATTR, src.get(LINE_ATTR) or "0")
    root.text = src.text
    for c in src:
        root.append(copy.deepcopy(c))
    return container


def _view_root(arch: etree._Element) -> etree._Element:
    """The view's root element (`<form>`/`<tree>`/…) inside the arch container."""
    if arch.tag == "field" and arch.get("name") == "arch":
        for c in arch:
            if isinstance(c.tag, str):
                return c
    return arch


def _find_targets(arch: etree._Element, expr: str) -> tuple[list, str | None]:
    # Odoo evaluates a locator on the view's ROOT element: an absolute path
    # (`//x`, `/form/x`) is document-wide, a relative one (`field[@name='a']`,
    # `./x`) is relative to the root element itself.
    try:
        if expr.startswith("/"):
            r = arch.xpath(_scoped(expr))
        else:
            r = _view_root(arch).xpath(expr)
    except (etree.XPathError, ValueError) as e:
        return [], f"xpath-error:{type(e).__name__}"
    if isinstance(r, list):
        return r, None
    return ([] if not r else [r]), None


def _synth_xpath(el: etree._Element) -> str | None:
    """Odoo's shorthand locator: the first `<tag>` whose attributes equal ALL of
    the spec's attributes except `position`/`version` (ir_ui_view locate_node)."""
    tag = el.tag
    if not isinstance(tag, str):
        return None
    preds = []
    for k, v in el.attrib.items():
        if not isinstance(k, str) or k in ("position", "version") or k.startswith("odx-"):
            continue
        if "'" not in v:
            lit = f"'{v}'"
        elif '"' not in v:
            lit = f'"{v}"'
        else:
            parts = v.split("'")
            lit = "concat(" + ", \"'\", ".join(f"'{p}'" for p in parts) + ")"
        preds.append(f"[@{k}={lit}]")
    return f"//{tag}" + "".join(preds)


def _mutate(target: etree._Element, spec: etree._Element, position: str,
            arch: etree._Element | None = None) -> None:
    """Apply one directive's payload relative to `target` (Odoo positions)."""
    if position == "attributes":
        src = spec.get(SRC_ATTR)
        for att in spec.findall("attribute"):
            nm = att.get("name")
            if nm is None:
                continue
            if att.get("add") or att.get("remove"):
                # Odoo 17 add/remove semantics on separator-joined values
                sep = att.get("separator", ",")
                split_sep = None if sep == " " else sep
                to_add = [x for x in (y.strip() for y in (att.get("add") or "").split(split_sep)) if x]
                to_remove = {y.strip() for y in (att.get("remove") or "").split(split_sep)}
                values = [y.strip() for y in (target.get(nm) or "").split(split_sep)]
                kept = [v for v in values if v and v not in to_remove]
                # token provenance: each part keeps the view that contributed it
                tokens = _token_map(target, nm)
                prior = target.get(ATTR_SRC_PREFIX + nm) or target.get(SRC_ATTR) or ""
                tmap = {v: tokens.get(v, prior) for v in kept}
                for v in to_add:
                    tmap[v] = src or ""
                target.set(TOKEN_PREFIX + nm, json.dumps(tmap))
                target.set(nm, (sep if split_sep else " ").join(kept + to_add))
            elif att.text:
                target.set(nm, att.text)
                if TOKEN_PREFIX + nm in target.attrib:
                    del target.attrib[TOKEN_PREFIX + nm]
            elif nm in target.attrib:
                del target.attrib[nm]
            # per-attribute provenance: the value now comes from the extension
            if src and nm in target.attrib:
                target.set(ATTR_SRC_PREFIX + nm, src)
                target.set(ATTR_LINE_PREFIX + nm, str(att.sourceline or 0))
        return

    # payload children; a child with position="move" pulls an existing node
    # of the arch into this spot (Odoo `move`), instead of copying content
    children: list[etree._Element] = []
    for c in spec:
        if not isinstance(c.tag, str):
            continue
        if c.get("position") == "move" and arch is not None:
            expr = c.get("expr") if c.tag == "xpath" else _synth_xpath(c)
            found, err = _find_targets(arch, expr) if expr else ([], "no-expr")
            if found and not err:
                node = found[0]
                if node.getparent() is not None:
                    node.getparent().remove(node)
                children.append(node)
            continue
        children.append(copy.deepcopy(c))
    parent = target.getparent()

    if position == "inside":
        for c in children:
            target.append(c)
        return
    if position == "replace" and spec.get("mode") == "inner":
        for c in list(target):
            target.remove(c)
        target.text = spec.text
        for c in children:
            target.append(c)
        return
    if parent is None:                        # can't move a rootless target
        if position == "replace" and children:
            # replacing the root: keep the element, swap its content/attrs
            new = children[0]
            target.clear()
            target.tag = new.tag
            for k, v in new.attrib.items():
                target.set(k, v)
            target.text = new.text
            for c in list(new):
                target.append(c)
        return
    idx = parent.index(target)
    if position == "replace":
        # `$0` inside the payload stands for the replaced node itself (wrap)
        for c in children:
            for loc in c.iter():
                if isinstance(loc.tag, str) and (loc.text or "").strip() == "$0":
                    loc.text = ""
                    loc.append(copy.deepcopy(target))
                    break
        tail = target.tail
        parent.remove(target)
        for i, c in enumerate(children):
            parent.insert(idx + i, c)
        if children and tail:
            children[-1].tail = (children[-1].tail or "") + tail
    elif position == "after":
        for i, c in enumerate(children):
            parent.insert(idx + 1 + i, c)
    elif position == "before":
        for i, c in enumerate(children):
            parent.insert(idx + i, c)
