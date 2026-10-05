"""Code-vs-database drift (`odx diff db`, RFC §12/§15) — the engine half.

The runtime reads the live registry into a `DbSnapshot`; this compares it with
what the code defines. Static knowledge decides what is drift:

  - `drift.model-not-in-db`   a project model is defined in code but absent in
                              the DB (module not upgraded / not installed);
  - `drift.field-not-in-db`   a stored field in code, absent in the DB
                              (needs `-u module`);
  - `drift.field-type-changed` type differs (migration needed);
  - `drift.field-not-in-code` the DB has a field the code no longer defines for
                              a module we index (leftover column / removed field);
  - `drift.module-not-installed` a project module isn't installed.
"""

from __future__ import annotations

from odx.engine.contracts import DbSnapshot, Diagnostic, Scope, Severity

_TYPE = {"Many2oneReference": "many2one_reference"}


def _ttype(ftype: str) -> str:
    return _TYPE.get(ftype, ftype.lower())


def diff_snapshot(api, snap: DbSnapshot, scope: Scope) -> list[Diagnostic]:
    out: list[Diagnostic] = []
    installed = {m for m, st in snap.installed_modules.items() if st in ("installed", "to upgrade")}
    for mod in api.project_modules():
        if not api.in_scope(scope, module=mod):
            continue
        if snap.installed_modules and mod not in installed:
            out.append(Diagnostic(Severity.INFO, "drift.module-not-installed",
                                  f"Module `{mod}` is not installed in `{snap.database}`."))
    for name in api.model_names():
        m = api.resolved_model(name)
        mods = {p.module for p in m.contributors}
        in_scope_mods = [x for x in mods if api.in_scope(scope, module=x)]
        if not in_scope_mods:
            continue
        if snap.installed_modules and not (mods & installed):
            continue                                   # not installed: nothing to compare
        dbm = snap.models.get(name)
        if dbm is None:
            if m.defined and m.defined_in in installed and m.kind != "AbstractModel":
                out.append(Diagnostic(Severity.WARNING, "drift.model-not-in-db",
                                      f"`{name}` is defined in code but missing in "
                                      f"`{snap.database}`; upgrade `{m.defined_in}`.",
                                      model=name, location=m.location))
            continue
        for fname, rf in m.fields.items():
            if snap.installed_modules and rf.provenance.module not in installed:
                continue
            stored = rf.info.attributes.get("store")
            computed = rf.info.attributes.get("compute") or rf.info.attributes.get("related")
            if computed and stored is not True:
                continue                                # non-stored compute: no column
            dbf = dbm.fields.get(fname)
            if dbf is None:
                out.append(Diagnostic(Severity.WARNING, "drift.field-not-in-db",
                                      f"`{name}.{fname}` exists in code but not in "
                                      f"`{snap.database}`; upgrade `{rf.provenance.module}`.",
                                      model=name, field=fname, location=rf.info.location))
            elif dbf.type and _ttype(rf.info.type) != dbf.type:
                out.append(Diagnostic(Severity.WARNING, "drift.field-type-changed",
                                      f"`{name}.{fname}` is {rf.info.type} in code but "
                                      f"{dbf.type} in `{snap.database}`; a migration is needed.",
                                      model=name, field=fname, location=rf.info.location))
        known_mods = set(api.ws.modules)
        for fname, dbf in dbm.fields.items():
            if fname in m.fields or fname in ("id", "display_name", "create_uid", "create_date",
                                              "write_uid", "write_date", "__last_update"):
                continue
            if fname.startswith("x_"):
                continue                                # studio/custom
            owners = set(dbf.modules)
            if owners and owners <= known_mods and owners & set(in_scope_mods):
                out.append(Diagnostic(Severity.INFO, "drift.field-not-in-code",
                                      f"`{snap.database}` still has `{name}.{fname}` "
                                      f"(from {', '.join(sorted(owners))}) but the code no longer "
                                      "defines it; it is removed on the next upgrade.",
                                      model=name, field=fname, location=m.location))
    return out
