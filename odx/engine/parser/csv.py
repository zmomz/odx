"""CSV parser: `ir.model.access.csv` and other CSV data files -> rows + Location.

Phase 3. Odoo loads a CSV data file named `<model>.csv` as records of that model:
the `id` column defines external ids, and `<field>:id` / `<field>/id` columns
reference them. `ir.model.access.csv` is the important case (ACL rows feed the
security analyzers and the §7 index), but every CSV contributes to the xml-id
graph — a group or country defined in CSV must not read as a dangling ref.

Never raises: a malformed file becomes `RawCsvFile.parse_error`.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from odx.engine.contracts import AccessRule, Location
from odx.engine.model.raw import RawCsvFile, XmlIdRef

_TRUE = {"1", "true", "True", "TRUE", "yes"}


def _norm(ref: str, module: str) -> str:
    return ref if "." in ref else f"{module}.{ref}"


def parse_csv_text(text: str, file: Path, module: str) -> RawCsvFile:
    model = file.stem
    out = RawCsvFile(file=file, module=module, model=model)
    try:
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
    except csv.Error as e:
        out.parse_error = f"csv.Error: {e}"
        return out
    if not rows:
        return out
    header = [h.strip() for h in rows[0]]
    for i, raw in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in raw):
            continue
        row = {h: (raw[j].strip() if j < len(raw) else "") for j, h in enumerate(header)}
        loc = Location(file, i, 0, i, sum(len(c) for c in raw) + max(len(raw) - 1, 0))
        out.rows.append(row)
        out.row_locations.append(loc)
        xid = row.get("id")
        if xid and not xid.startswith(("__export__.", "__import__.")):
            out.xmlids.append((_norm(xid, module), loc))
        for h, v in row.items():
            if not v or not (h.endswith(":id") or h.endswith("/id")):
                continue
            for part in v.split(","):
                part = part.strip()
                if part and not part.startswith(("__export__.", "__import__.")):
                    out.xmlid_refs.append(XmlIdRef(
                        xmlid=_norm(part, module), module=module, location=loc,
                        context=f"csv:{h}", record_model=model))
    return out


def parse_csv(path: Path, module: str) -> RawCsvFile:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as e:
        r = RawCsvFile(file=path, module=module, model=path.stem)
        r.parse_error = f"{type(e).__name__}: {e}"
        return r
    return parse_csv_text(text, path, module)


def access_rules_from(raw: RawCsvFile) -> list[AccessRule]:
    """ACL rows of an `ir.model.access.csv`. `model` holds the model's external
    id as written until the index maps it to a model name (`model_ref` keeps
    the raw value either way)."""
    if raw.model != "ir.model.access":
        return []
    out: list[AccessRule] = []
    for row, loc in zip(raw.rows, raw.row_locations):
        mref = row.get("model_id:id") or row.get("model_id/id") or row.get("model_id") or ""
        gref = row.get("group_id:id") or row.get("group_id/id") or row.get("group_id") or ""
        xid = row.get("id")
        out.append(AccessRule(
            model=mref,
            group=_norm(gref, raw.module) if gref else None,
            perm_read=row.get("perm_read", "0") in _TRUE,
            perm_write=row.get("perm_write", "0") in _TRUE,
            perm_create=row.get("perm_create", "0") in _TRUE,
            perm_unlink=row.get("perm_unlink", "0") in _TRUE,
            location=loc,
            xmlid=_norm(xid, raw.module) if xid else None,
            name=row.get("name"),
            model_ref=_norm(mref, raw.module) if mref else None,
            module=raw.module,
        ))
    return out


def parse_access_csv(path: Path, module: str) -> list[AccessRule]:
    return access_rules_from(parse_csv(path, module))
