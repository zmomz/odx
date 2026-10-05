"""Doctor (RFC §15) — the engine half of the runtime fresh-install probe.

The runtime installs the modules into a throwaway DB and hands back the raw
`InstallProbe` (return code + log). This turns the log into Diagnostics and,
where the message names a file/model/field the engine knows, attaches the
static `Location` — so "works on my machine" failures land on a line.
"""

from __future__ import annotations

import re
from pathlib import Path

from odx.engine.contracts import Diagnostic, DoctorReport, InstallProbe, Location, Severity

_LINE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ \d+ (?P<level>[A-Z]+) \S+ (?P<logger>\S+): (?P<msg>.*)$")
_FILE_LINE = re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+)')
_XML_LOC = re.compile(r"(?P<file>[\w/.-]+\.xml):(?P<line>\d+)")
_FIELD = re.compile(r"[Ff]ield [`'\"]?(?P<field>[\w.]+)[`'\"]? does not exist")
_MODEL_FIELD = re.compile(r"[`'\"](?P<model>[a-z_]+(?:\.[a-z_0-9]+)+)[`'\"]")

_CODES = [
    (re.compile(r"ParseError|while parsing", re.I), "doctor.parse-error"),
    (re.compile(r"External ID not found", re.I), "doctor.xmlid-not-found"),
    (re.compile(r"does not exist|Invalid field", re.I), "doctor.unknown-field"),
    (re.compile(r"Element .* cannot be located in parent view", re.I), "doctor.xpath-target-missing"),
    (re.compile(r"has no access rules", re.I), "doctor.missing-acl"),
    (re.compile(r"ImportError|ModuleNotFoundError", re.I), "doctor.import-error"),
    (re.compile(r"psycopg2|IntegrityError|violates", re.I), "doctor.sql-error"),
    (re.compile(r"Traceback", re.I), "doctor.traceback"),
]


def _code(msg: str, level: str) -> str:
    for rx, code in _CODES:
        if rx.search(msg):
            return code
    return "doctor.error" if level in ("ERROR", "CRITICAL") else "doctor.warning"


def _locate(api, block: str) -> Location | None:
    for rx in (_XML_LOC, _FILE_LINE):
        for m in rx.finditer(block):
            f, line = m.group("file"), int(m.group("line"))
            if "/odoo/" in f and "/addons/" not in f:
                continue
            cand = Path(f)
            if api is not None:
                for ms in api.ws.modules.values():
                    p = ms.path / cand
                    if p.is_file():
                        return Location(p, line, 0, line, 0)
                    if cand.is_absolute() and str(cand).startswith(str(ms.path)):
                        return Location(cand, line, 0, line, 0)
            return Location(cand, line, 0, line, 0)
    return None


def doctor_report(api, probe: InstallProbe) -> DoctorReport:
    diags: list[Diagnostic] = []
    blocks: list[tuple[str, str]] = []                 # (level, text incl. traceback)
    for raw in (probe.log or "").splitlines():
        m = _LINE.match(raw)
        if m:
            blocks.append((m.group("level"), m.group("msg")))
        elif blocks:
            lvl, txt = blocks[-1]
            blocks[-1] = (lvl, txt + "\n" + raw)
        elif raw.strip():
            blocks.append(("ERROR" if "Error" in raw else "INFO", raw))
    seen = set()
    for level, text in blocks:
        if level not in ("WARNING", "ERROR", "CRITICAL"):
            continue
        head = text.splitlines()[0][:400]
        key = (level, head)
        if key in seen:
            continue
        seen.add(key)
        fm = _FIELD.search(text)
        mm = _MODEL_FIELD.search(text)
        diags.append(Diagnostic(
            Severity.ERROR if level in ("ERROR", "CRITICAL") else Severity.WARNING,
            _code(text, level), head, model=mm.group("model") if mm else None,
            field=fm.group("field") if fm else None, location=_locate(api, text)))
    ok = probe.returncode == 0 and not any(d.severity is Severity.ERROR for d in diags)
    if probe.returncode not in (0,) and not diags:
        diags.append(Diagnostic(Severity.ERROR, "doctor.install-failed",
                                f"install exited with code {probe.returncode}"))
    tail = "\n".join((probe.log or "").splitlines()[-40:])
    return DoctorReport(database=probe.database, modules=probe.modules, ok=ok,
                        diagnostics=diags, log_excerpt=tail)
