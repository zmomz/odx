"""Failure catalog types (RFC §3).

The catalog — not the scoreboard number — is the spike's real deliverable. Each
failure is tagged so the go/no-go decision reads the *shape* of the failures:

  fixable       parser/resolver bug we can close (raises the score with effort)
  hard          resolvable statically but expensive (cross-module _inherits,
                xpath view inheritance, monkeypatch linking)
  out-of-scope  fundamentally undecidable statically (runtime-data-driven
                names/fields) -> runtime bridge or explicitly unsupported
  boundary      not a resolver failure: the referenced thing lives outside the
                scanned corpus (an addon/repo we didn't clone). Reported so it
                never masquerades as a real failure.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field as dc_field
from enum import Enum


class Tag(str, Enum):
    FIXABLE = "fixable"
    HARD = "hard"
    OUT_OF_SCOPE = "out-of-scope"
    BOUNDARY = "boundary"


@dataclass
class Failure:
    tag: Tag
    pattern: str          # short stable key, e.g. "dynamic-inherit"
    detail: str           # human context
    where: str            # file:line or model name


@dataclass
class Catalog:
    failures: list[Failure] = dc_field(default_factory=list)

    def add(self, tag: Tag, pattern: str, detail: str, where: str) -> None:
        self.failures.append(Failure(tag, pattern, detail, where))

    def by_tag(self) -> dict[str, int]:
        return dict(Counter(f.tag.value for f in self.failures))

    def by_pattern(self) -> dict[str, int]:
        return dict(Counter(f.pattern for f in self.failures))

    def pattern_tags(self) -> dict[str, str]:
        return {f.pattern: f.tag.value for f in self.failures}
