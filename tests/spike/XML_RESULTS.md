# Phase 0.6 — XML View-Inheritance Spike: Results & Go/No-Go

**Decision: GO.** ✅

Phase 0.5 validated the *cleaner* half (Python-side model/field/`_inherit`
resolution). This spike validates the messier half the RFC §7 index depends on —
resolving `inherit_id` xml-ids across modules and evaluating `xpath`/`position`
locators against the parent arch. It holds, and it holds for the same reason
Phase 0.5 did: strong core resolution, failures clustering into a few
well-understood mechanisms, a negligible truly-undecidable set.

> Reproduce: `python -m tests.spike.run_xml`. Raw output in
> [out/xml_scoreboard.json](out/) and [out/xml_catalog.md](out/).

---

## Corpus

Same corpus as Phase 0.5 (Odoo core 17.0 + OCA server-tools /
account-financial-tools / web). 4,472 XML files · 5,541 view records (of which
1,937 QWeb `<template>`) · 2,495 inheriting views · 4,214 inheritance locators.

---

## Scoreboard

| metric | result | note |
|---|--:|---|
| XML files parsed without crashing | **99.98%** | 1 empty/malformed file |
| `inherit_id` resolved (cross-module xml-id) | **99.64%** | the load-bearing question |
| Locator target found (of all locators) | **97.29%** | with `hasclass` + chain applied |
| Locator target found (of evaluable) | **97.46%** | |

Per-repo:

| repo | parse | inheriting | `inherit_id` resolved | locators | target found |
|---|--:|--:|--:|--:|--:|
| odoo | 99.98% | 2,375 | 99.79% | 4,072 | 97.27% |
| server-tools | 100% | 42 | 100% | 45 | 100% |
| account-financial-tools | 100% | 49 | 93.88% | 66 | 95.45% |
| web | 100% | 29 | 96.55% | 31 | 100% |

**The headline:** cross-module `inherit_id` resolution — the thing that could
have been a fuzzy frontier — is **99.64%**. Given an addons tree, we can nearly
always resolve which view an inheriting view extends. That was the open risk; it
isn't one.

---

## Two fixable findings that moved the numbers (§3 in action)

The spike's first run looked far worse until two fixable issues were closed —
the same pattern as Phase 0.5's chained-assignment bug:

1. **`hasclass()` is an Odoo XPath extension, not XPath 1.0.** 332 of the initial
   335 "failures" were lxml rejecting `//div[hasclass('...')]`. Registering
   Odoo's `hasclass` as a custom lxml XPath function lifted locator-found
   **77% → 84.5%** and emptied the fixable bucket to 3. *The engine must register
   Odoo's full XPath extension set* (`hasclass`, and check for others) — a small,
   known task.

2. **Locators resolve against the *effective* arch, not the base.** Testing a
   locator only against the base view's arch misses nodes added by intermediate
   inheritances. Evaluating against the whole applied chain lifted locator-found
   **84.5% → 97.29%** and collapsed the "hard" bucket **644 → 105**. Those 644
   were never failures — they were the spike not yet applying inheritance. *This
   is the Phase 3 resolver's core job, not a limitation.*

Neither changed the decision; both show the raw number is noise until the catalog
is understood.

---

## Failure catalog (the deliverable)

117 flagged sites after the two fixes:

| tag | count | meaning |
|---|--:|---|
| **hard** | 105 | `xpath-target-not-found` in this view's ancestor chain |
| **boundary** | 7 | `inherit_id` → a view in an un-cloned OCA repo |
| **fixable** | 3 | 1 empty file, 1 source typo (`////div`), 1 `%(xmlid)d` interpolation |
| **out-of-scope** | 2 | `inherit_id` via `eval=` (runtime-derived) |

### hard (105) — upper bound, mostly still resolvable

`xpath-target-not-found` means the locator's target wasn't in the view's own
ancestor chain. The dominant cause is **sibling inheritance**: Odoo applies *all*
views inheriting a base, in order, so a node added by view *B* is visible to
view *C* (both inheriting the same base) when *C* applies after *B*. The spike
only walks a view's own ancestor path, not its siblings — so these 105 are an
**upper bound** on genuine problems. A real registry-wide applicator (Phase 3)
resolves most; the true residue is genuinely-dangling locators, which is exactly
what the Phase 4 analyzer *should* flag as `view.xpath-target-missing`. Either
way: resolvable or a real defect worth reporting — not an undecidable wall.

### out-of-scope (2) — the static/runtime boundary

Two views set `inherit_id` via `eval=` (computed at load). Correctly out of
scope; route to the runtime bridge. Two, in 2,495 inheriting views.

### boundary (7)

`inherit_id` refs into un-cloned repos (`account_statement_base`, `date_range`,
plus `web.assets_*` QWeb asset bundles which are a distinct mechanism). Corpus
artifacts; they vanish when those repos are added.

---

## Why this is GO

1. **Cross-module xml-id resolution is essentially solved** (99.64%). This was
   the real risk in the XML half and it evaporated.
2. **Locator resolution is 97%+** once Odoo's XPath extensions are registered and
   inheritance is applied — both being ordinary Phase 3 engine work, already
   scoped by this catalog.
3. **Truly out-of-scope is 2 sites.** The static/runtime boundary in XML is as
   sharp and small as it was in Python.

Combined with Phase 0.5, **both halves of the resolver assumption are now
validated on real, messy, cross-repo code.** The RFC's §2 bet holds end-to-end
for the Python + XML surface that §7's index is built on.

---

## What this adds to the Phase 3 scope

- **Register Odoo's XPath extension functions** with lxml (`hasclass`, audit for
  the rest) before evaluating any view locator.
- **Apply inheritance registry-wide, in order** (base + all inheriting views,
  respecting `mode="primary"` vs extension), producing an *effective arch* per
  view — the artifact locators and field-reference indexing run against.
- **Preprocess `%(xmlid)d` / `%(xmlid)s`** id-interpolation in data files before
  xpath evaluation.
- **QWeb `<template>` inheritance** (1,937 templates here) shares the mechanism
  but has its own `t-` directives; treat as a sibling of record-view inheritance.
- **Genuinely-dangling locators** become a first-class `Diagnostic`
  (`view.xpath-target-missing`) in Phase 4 — the honest residue of the 105.
- Add `OCA/website`, `OCA/reporting-engine`, `OCA/date-range`,
  `OCA/account-statement` to the permanent corpus to close boundary refs.

## Still unmeasured (deliberately)

CSV ACLs (`ir.model.access.csv`) and record-rule/domain resolution, and QWeb
`t-`-directive semantics beyond inheritance. These are narrower and lower-risk
than view inheritance was; they can be spiked when Phase 3 reaches them, or
validated directly by the Phase 3 fixtures.
