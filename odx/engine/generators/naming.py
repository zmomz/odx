"""Naming helpers shared by every generator (pure string functions).

One place decides how an Odoo model name maps to a Python class name, a file
name, and an XML id, so generated artifacts agree with each other (the views
file references the same ``model_<x>`` the ACL row does, etc.).
"""

from __future__ import annotations

import keyword
import re

_NON_WORD = re.compile(r"[^0-9a-zA-Z]+")


def snake(model: str) -> str:
    """``library.book`` -> ``library_book`` (file names, xml id stems)."""
    return _NON_WORD.sub("_", model.strip()).strip("_").lower()


def class_name(model: str) -> str:
    """``library.book`` -> ``LibraryBook``; ``res.partner`` -> ``ResPartner``."""
    parts = [p for p in _NON_WORD.split(model.strip()) if p]
    name = "".join(p[:1].upper() + p[1:] for p in parts) or "Model"
    if name[0].isdigit():
        name = "M" + name
    return name


def xmlid(model: str) -> str:
    """XML-id-safe stem for a model: ``library.book`` -> ``library_book``."""
    return snake(model)


def model_xmlid(model: str) -> str:
    """The ir.model record Odoo creates: ``model_library_book``."""
    return f"model_{xmlid(model)}"


def humanize(name: str) -> str:
    """``library.book`` -> ``Library Book``; ``partner_id`` -> ``Partner``;
    ``tag_ids`` -> ``Tags``; ``is_active`` -> ``Is Active``."""
    n = name.strip()
    if n.endswith("_ids"):
        n = n[:-4] + "s"
    elif n.endswith("_id"):
        n = n[:-3]
    words = [w for w in _NON_WORD.split(n) if w]
    return " ".join(w[:1].upper() + w[1:] for w in words)


def slug(label: str) -> str:
    """Free text -> identifier fragment: ``"My Books!"`` -> ``my_books``."""
    s = snake(label)
    return s or "item"


def is_identifier(name: str) -> bool:
    """A legal, non-keyword Python identifier (field / method / module names)."""
    return name.isidentifier() and not keyword.iskeyword(name)


def is_model_name(name: str) -> bool:
    """Dotted lowercase model name such as ``library.book`` / ``x_foo``."""
    return bool(re.fullmatch(r"[a-z_][a-z0-9_]*(\.[a-z0-9_]+)*", name))
