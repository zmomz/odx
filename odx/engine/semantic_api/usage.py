"""Where-used for `why` / `impact` (RFC §7, §14): the same index query at two
verbosities. Returns a `UsageReport`; the client picks how much to show."""

from __future__ import annotations

from collections import Counter

from odx.engine.contracts import Resolution, UsageReport


def usage_report(api, target: str) -> UsageReport:
    parts = target.strip().split(".")
    model = field = None
    for i in range(len(parts), 0, -1):
        cand = ".".join(parts[:i])
        if cand in api.resolved_models():
            model, rest = cand, parts[i:]
            field = ".".join(rest) if rest else None
            break
    if model is None:
        return UsageReport(target=target, model=None, known=False,
                           resolution=Resolution.DYNAMIC,
                           message=(f"no model named like '{target}' in the indexed addons; "
                                    "check the spelling or add its module's path to "
                                    "[odoo].addons_paths / context_paths in .odx.toml"))
    m = api.resolved_model(model)
    if field and "." in field:
        # a path: report on its final hop
        pr = api.resolve_path(model, field)
        if pr.hops and pr.hops[-1].found is not None:
            model, field = pr.hops[-1].model, pr.hops[-1].field
    if field:
        state = api.field_state(model, field)
        info = api.get_field(model, field)
        entries = api.entries_to(model, field)
        definition = info.location if info else None
        mods = [info.defined_in] if info and info.defined_in else []
        if info is None:
            meths = api.methods(model).get(field)
            if meths:
                entries = api.method_references(model, field)
                state = Resolution.STATIC
                mods = sorted({mod for _m, mod in meths})
                definition = None
                for mod, meth in m.methods.get(field, []) if m else []:
                    definition = meth.location
                    break
        msg = "" if state is Resolution.STATIC else (
            f"'{field}' is not a field of {model}" if state is Resolution.UNRESOLVED
            else f"{model} is not fully visible; '{field}' may be defined outside the indexed addons")
    else:
        state = Resolution.STATIC
        entries = api.entries_to(model)
        definition = m.location if m else None
        mods = []
        for p in (m.contributors if m else []):
            if p.module not in mods:
                mods.append(p.module)
        msg = ""
    entries = sorted(entries, key=lambda e: (e.kind, str(e.location.file) if e.location else "",
                                             e.location.start_line if e.location else 0))
    return UsageReport(
        target=target, model=model, field=field, known=state is Resolution.STATIC,
        resolution=state, definition=definition, defined_in=mods,
        by_kind=dict(Counter(e.kind for e in entries)),
        references=[e.to_reference() for e in entries], message=msg)
