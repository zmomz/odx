"""ViewAnalyzer — effective-arch defects.

`view.xpath-target-missing` is gated by docs/PHASE3.md constraint #3
(applicator-before-analyzer): it ships only because `EffectiveArchBuilder.build`
is registry-wide and load-ordered AND the XPath dialect is asserted complete
(`xpath.assert_complete()` runs here, so a dialect regression disables the
accusation rather than producing false ones). Only `target-not-found` /
`xpath-error` locators — UNRESOLVED after correct, complete application — are
accused; announced limitations (DYNAMIC: interpolation, dialect, base outside
the corpus) and QWeb (PARTIAL) are not.
"""

from __future__ import annotations

from odx.engine.contracts import Diagnostic, Resolution, Scope, Severity
from odx.engine.model import xpath


class ViewAnalyzer:
    code_prefix = "view"
    categories = ("views",)

    def run(self, api, scope: Scope) -> list[Diagnostic]:
        try:
            xpath.assert_complete()
        except AssertionError:
            return []                       # gate: never accuse with an incomplete dialect
        out: list[Diagnostic] = []
        seen = set()
        for ev in api.effective_views().values():
            for u in ev.unresolved_locators:
                if u.resolution is not Resolution.UNRESOLVED:
                    continue
                rv = api.raw_view(u.view_xmlid) if u.view_xmlid else None
                module = rv.module if rv else None
                if not api.in_scope(scope, module=module, location=u.location):
                    continue
                key = (u.view_xmlid, u.expr, u.location)
                if key in seen:
                    continue
                seen.add(key)
                if u.reason.startswith("xpath-error"):
                    code, msg = "view.xpath-invalid", (
                        f"Invalid XPath `{u.expr}` in view `{u.view_xmlid}` ({u.reason}).")
                else:
                    code, msg = "view.xpath-target-missing", (
                        f"`{u.expr}` (position={u.position or 'inside'}) in view "
                        f"`{u.view_xmlid}` matches nothing in the inherited arch "
                        f"(`{ev.xmlid}` after applying its earlier extensions); "
                        "Odoo will raise on install/upgrade.")
                out.append(Diagnostic(Severity.ERROR, code, msg, model=ev.model,
                                      location=u.location or (rv.location if rv else None)))
        return out
