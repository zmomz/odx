"""XmlIdAnalyzer — the XML-ID graph (RFC §7): defined vs referenced.

  - `xmlid.unknown-ref`: an xml-id whose module IS indexed, but which no data
    file, CSV or implicit (model_/field_/module_) id defines. A ref into a
    module outside the corpus is a boundary, never accused.
  - `xmlid.missing-dependency`: the id exists, but its module is not in the
    referencing module's dependency closure — works only if that module happens
    to be installed first (a classic install-order bug). Fix: add the depends.
  - `xmlid.duplicate`: the same id declared twice by the same module (the second
    silently overwrites the first).
"""

from __future__ import annotations

from collections import defaultdict

from odx.engine.contracts import Diagnostic, Scope, Severity

_IMPLICIT_PREFIXES = ("model_", "field_", "module_", "selection__", "access_",
                      "base.module_category_")


class XmlIdAnalyzer:
    code_prefix = "xmlid"
    categories = ("xmlids", "data")

    def run(self, api, scope: Scope) -> list[Diagnostic]:
        from odx.engine.analyzers.fixes import add_dependency_fix
        g = api.xmlids
        known_modules = set(api.ws.graph.manifests)
        out: list[Diagnostic] = []
        seen = set()
        for r in g.refs:
            if not api.in_scope(scope, module=r.module, location=r.location):
                continue
            if "." not in r.xmlid:
                continue
            owner, local = r.xmlid.split(".", 1)
            key = (r.xmlid, r.location)
            if key in seen:
                continue
            seen.add(key)
            d = g.definition(r.xmlid)
            if d is None and r.context == "python:env.ref":
                continue        # code refs are often guarded (raise_if_not_found=False)
            if d is None:
                if owner not in known_modules or not api.closure_is_complete(r.module):
                    continue                     # boundary: can't see that module
                if local.startswith(_IMPLICIT_PREFIXES[:4]):
                    # implicit ids for things we may not model (selection values,
                    # dynamic fields) — only accuse model_/field_ ids we can check
                    if not (local.startswith("model_") or local.startswith("field_")):
                        continue
                    if owner in api.ws.modules and any(
                            "dynamic" in n for rmod in api.ws.modules[owner].python
                            for rm in rmod.models for n in rm.notes):
                        continue
                out.append(Diagnostic(
                    Severity.ERROR, "xmlid.unknown-ref",
                    f"`{r.xmlid}` ({r.context}) is not defined by module `{owner}`; "
                    "install/upgrade will fail with 'External ID not found'.",
                    location=r.location))
                continue
            if r.context == "python:env.ref":
                continue        # code refs are often guarded by an installed-module check
            defining = {x.module for x in g.defined.get(r.xmlid, [])}
            closure = api.dependency_closure(r.module)
            if (r.module not in defining and not (defining & closure)
                    and api.closure_is_complete(r.module)):
                dep = owner if owner in known_modules else sorted(defining)[0]
                if dep in closure:
                    continue
                out.append(Diagnostic(
                    Severity.WARNING if r.context in ("groups", "python:groups")
                    else Severity.ERROR, "xmlid.missing-dependency",
                    f"`{r.xmlid}` is defined by `{dep}`, which `{r.module}` does not depend on; "
                    f"add '{dep}' to {r.module}'s manifest depends.",
                    location=r.location,
                    suggested_fix=add_dependency_fix(api, r.module, dep)))
        # Redefinitions within one module. Re-declaring an id to UPDATE it (set a
        # parent created later, demo tweaking data) is an Odoo idiom; only a
        # second declaration that sets an already-set field to a DIFFERENT
        # value (a conflicting copy-paste) or repeats it identically is flagged.
        recs = defaultdict(list)
        for r in api.all_records():
            if r.xmlid and r.tag == "record":
                recs[(r.module, r.xmlid)].append(r)
        for (mod, xid), rs in recs.items():
            if len(rs) < 2 or not api.in_scope(scope, module=mod):
                continue
            first = rs[0]

            def val(f):
                return (f.ref, f.eval, f.text)
            for r in rs[1:]:
                if (r.location and first.location
                        and str(r.location.file) != str(first.location.file)):
                    continue        # a later file updating the record: Odoo idiom
                shared = set(first.fields) & set(r.fields)
                conflicts = sorted(n for n in shared if val(first.fields[n]) != val(r.fields[n])
                                   and first.fields[n].element is not None
                                   and len(first.fields[n].element) == 0)
                identical = shared and not conflicts and set(first.fields) == set(r.fields)
                if conflicts:
                    msg = (f"`{xid}` is declared again with different "
                           f"{', '.join(conflicts[:4])} (first at {first.location}); the later "
                           "values silently win.")
                elif identical:
                    msg = f"`{xid}` is declared twice identically (first at {first.location})."
                else:
                    continue
                out.append(Diagnostic(Severity.WARNING if identical else Severity.INFO,
                                      "xmlid.duplicate", msg,
                                      location=r.location,
                                      related=[first.location] if first.location else []))
        return out
