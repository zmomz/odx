"""XML parser: Odoo data files -> raw view records + inheritance locators + the
other §7 reference-bearing records (actions, menus, records with domains),
recording Location (C4).

Phase 3. **Seed:** graduate `tests/spike/xml_parser.py` (validated in Phase 0.6:
99.98% parse, 99.64% inherit_id resolution). Keep `recover=True` and the
never-crash discipline; capture `<record>`, `<template>`, `<menuitem>`, and
`inherit_id`/`xpath`/`position` locators.

CRITICAL (Phase 0.6 finding): locator xpath must be evaluated with Odoo's XPath
*dialect* registered (see odx.engine.model.xpath), not stock XPath 1.0 — a
missing extension function silently fails to match and would produce a false
'xpath-target-missing' against the user's code.
"""

from __future__ import annotations

from pathlib import Path

from odx.engine.model.raw import RawXmlFile


def parse_xml(path: Path, module: str) -> RawXmlFile:
    raise NotImplementedError("Phase 3 — graduate tests/spike/xml_parser.py")
