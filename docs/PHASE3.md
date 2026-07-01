# Phase 3 — The Engine Pipeline (resolver skeleton)

*The hardest phase and the product (RFC §19). Scoped by the Phase 0.5 + 0.6
failure catalogs. This document records the **structure** and the **ordering
constraints** those spikes surfaced; the skeleton under `odx/engine/` implements
that structure with typed stubs.*

## The one non-obvious structural decision: dependency-graph-first

Both validated halves depend on **module load order**:

- **Phase 0.5** — model field overrides apply in dependency order (last module
  wins); `_inherits` provenance needs the graph.
- **Phase 0.6** — a view's *effective arch* is its base plus its whole inheriting
  cohort applied **in load order**, then `sequence`.

So the pipeline does **not** start at the parser. It starts at the graph:

```
        ┌────────────────────────────┐
        │  loadorder.ModuleGraph      │  ← foundation stone (built FIRST)
        │  manifests → deps → order   │
        └───────────┬────────────────┘
                    │ load_order(), dependents_of()  (both consumers query this)
     ┌──────────────┴───────────────┐
     ▼                              ▼
┌──────────────┐            ┌───────────────────────┐
│ ModelResolver│            │ EffectiveArchBuilder   │
│ raw → resolved            │ base + cohort, ordered │
│ (fields, _inherit,        │ → effective arch/view  │
│  _inherits, provenance)   │ (the resolution UNIT)  │
└──────┬───────┘            └───────────┬───────────┘
       └───────────────┬────────────────┘
                       ▼
              index (§7)  →  semantic_api  →  analyzers / generators
```

`odx/engine/loadorder/` is a working implementation, not a stub — it's small and
everything sits on it. The rest are typed skeletons graduating from the spikes.

## Module map (`odx/engine/`)

| module | role | seed / status |
|---|---|---|
| `loadorder/` | manifest graph, topo `load_order()`, `dependents_of()` | **implemented** |
| `contracts.py` | `Resolution`, `Provenance` added for Phase 3 | implemented |
| `parser/python.py` | .py → `RawModule` (+ Location + Resolution); `delegate=True` → `_inherits` | **graduated** (100% parse, Location on 100% of models, catalog reproduced as Resolution states) |
| `parser/xml.py` | XML → `RawView` + locators (+ Location + mode/priority + Resolution) | **graduated** (99.98% parse; top-level directives only; primary/extension boundary + `priority` captured; eval `inherit_id` → DYNAMIC) |
| `parser/csv.py` | ACL CSV → `AccessRule` | new (unspiked, low risk) |
| `model/raw.py` | public `RawModel`/`RawField`/`RawView` types | implemented |
| `model/resolver.py` | `RawModel → ResolvedModel`, provenance, `related=` worklist fixpoint, field-override attribute merge, one2many inverse discovery | **implemented** (field-resolution backlog closed — items 1, 2 & 3 done) |
| `model/xpath.py` | Odoo XPath **dialect gate** + `%(id)d` interpolation | **audited complete** (`hasclass` is the only extension used corpus-wide; `assert_complete()` gate + full XPath-1.0 builtin set; unknown fn → announced) |
| `model/arch.py` | `EffectiveArchBuilder` — ordered cohort application | **implemented** (registry-wide, primary-aware, load-order+priority; honest `unresolved_locators`, no Diagnostics) |

## Hard constraints (violate these and you erode trust)

### 1. The XPath dialect must be provably complete
Odoo view locators use an XPath *dialect*, not XPath 1.0. A missing extension
function does **not** error — it silently fails to match, yielding a *false*
`view.xpath-target-missing` that blames the user's code for our gap (the Phase
0.6 `hasclass` episode, caught only by aggregate measurement). Therefore:
`model/xpath.py` enumerates the dialect authoritatively; anything unimplemented
is surfaced via `unsupported_functions()` and marked `Resolution.DYNAMIC`, never
degraded to a silent no-match.

### 2. Order is correctness, not convenience
Field overrides and view inheritances apply in `ModuleGraph.load_order()` (then
`sequence`). Wrong order → a later override is missed → stale value or false
missing-target. Both resolvers consume the graph directly; neither reimplements
ordering.

### 3. Applicator-before-analyzer (a sequencing dependency)
`view.xpath-target-missing` (Phase 4) is only trustworthy **after** the effective-arch
applicator is complete (registry-wide, correctly ordered) **and** the XPath
dialect is complete. Turning the analyzer on earlier converts every applicator
gap into a false accusation. This dependency is explicit: the missing-target
analyzer must not ship until `EffectiveArchBuilder.build` is done and dialect
completeness is asserted.

### 4. Dynamic is a first-class state, not an error
`Resolution.{STATIC,DYNAMIC,PARTIAL,UNRESOLVED}` rides on every resolved
model/field/view. DYNAMIC → route to the runtime bridge; PARTIAL → improvable
(e.g. related comodel not yet followed); UNRESOLVED → candidate Diagnostic once
the resolver is complete. Clients branch on this (muted LSP hint / MCP defer /
separate `report` bucket).

## Field-resolution backlog (from the Phase 0.5 catalog, in priority order)
1. ~~`related=` comodel/type inference via the related chain — 271 sites.~~ **Done.**
   Implemented as a **worklist fixpoint** in `resolver.py` (a related field can
   depend on another related field across an `_inherit` boundary, so this is
   iterate-until-stable, not a single walk). Cycles → `field.related-cycle`
   Diagnostic, never a guess. Validated on the corpus: 271 `comodel-via-related`
   PARTIALs collapsed to STATIC, 0 false cycles, 13 honest UNRESOLVED (all hop
   through fields defined in modules outside the corpus — the resolver declined
   to fabricate a comodel rather than guess).
2. ~~Reference / Many2oneReference / comodel-less handling — 82 sites.~~
   **Re-scoped by measurement, then done as field-override *attribute merge*.**
   Sorting the 80 `comodel-absent` own-fields on the corpus inverted the assumption:
   they are NOT mostly polymorphic References awaiting a DYNAMIC verdict. The
   measured split (confirmed live, before coding):
     - **33** — comodel exists in an *earlier same-model* contribution; a later
       attribute-only override drops it. **Resolver bug.**
     - **41** — comodel lives on an *inherited/mixin* field the override refines
       (e.g. `sale.order.campaign_id` overrides `utm.mixin.campaign_id`).
       **Resolver bug.** Live, this splits into **37** one-hop and **4** transitive
       (`alias_id`: `mail.alias.mixin.optional` -> `mail.alias.mixin` -> `hr.job`,
       comodel flows two hops) — which is why the merge is transitive + parent-first.
     - **6** — genuine `Many2oneReference` (`res_id`/`trg_field_ref`): the comodel
       is chosen at runtime from a sibling `res_model` field. Correctly **DYNAMIC
       by nature** — marked, not fabricated.
   Implemented as **Odoo field-override attribute merge**: a field re-declared
   without restating an attribute inherits it from the field it overrides — whether
   that original is an earlier same-model contribution or an inherited field.
   Comodel is only the visible symptom; the same bug silently dropped any
   unspecified attribute (`help`/`domain`/`string`/`required`/`groups`/...), so the
   parser now captures those and the merge preserves them (adversarial fixtures
   assert survival of the *untracked* attributes, not just comodel). One
   `_merge_field` serves both seams in load order: same-model (`_absorb`) and
   inheritance (`_InheritanceMerger`). `_settle_comodel` re-derives state after.
   **Result on the corpus:** 80 comodel-absent PARTIALs -> 0 (74 STATIC, 6 DYNAMIC);
   restoring `related=` that overrides dropped also rescued 11 previously-UNRESOLVED
   fields (related lift 254 -> 263, unresolved 13 -> 2). The 2 remaining UNRESOLVED
   hop through a field defined outside the corpus — honestly declined, not guessed.
3. ~~one2many inverse discovery by scanning the comodel — "6 sites".~~
   **Re-scoped by measurement, then done.** The catalog's "6 sites" was the
   flagged tail, not the work: the live corpus has **684** own `One2many` fields,
   **618** with a literal `inverse_name` to verify. Sorting them before coding
   (the confirm-live-state step) overturned the framing a third time — the split:
     - **599** — inverse names a Many2one declared on the comodel directly.
     - **18** — inverse names a back-pointer the comodel only has through
       inheritance/delegation: **14** via `_inherit`, **4** via `_inherits`. This
       is why the lookup must consume the comodel's *effective* fields
       (`_EffectiveFields`), never its raw own defs.
     - **6** — inverse names a real but **non-relational** Integer field on the
       comodel (`res_id`/`sms_id_int`/`create_uid`/`parent_res_id`): the legitimate
       reference-style / soft-FK inverse (a deleted-record id, or paired with a
       `res_model`). The comodel is solidly known, so this is left **STATIC** with
       an honest `inverse-nonrelational` note — not fabricated into a defect.
     - **1 → 0** — the sole *apparent* dangling inverse (`res.partner.daughter_ids`
       → `test.inherit.daughter.partner_id`) was **not** a defect: the comodel
       reaches `partner_id` through a `delegate=True` delegation the parser didn't
       model. Marking it UNRESOLVED would have been a false accusation, so the fix
       was to teach the parser that `Many2one(..., delegate=True)` is the modern
       `_inherits` spelling (Odoo reflects it into `_inherits`). That dissolved the
       case (→ resolved via delegation) **and** correctly modelled `ir.cron`'s
       delegation to `ir.actions.server`. Live genuinely-dangling inverses: **0**.
   Implemented as `ModelResolver._verify_inverses` (pass 2.5), reusing
   `_EffectiveFields` and the *merged* `inverse_name` (an override that drops it is
   restored by `_merge_field` first). Honest in both directions: present+relational
   → `inverse-resolved`; present+non-relational → STATIC + note; absent from a
   fully-visible comodel → UNRESOLVED + `inverse-dangling`; comodel outside the
   corpus → known literal, not downgraded, `inverse-comodel-outside-corpus`.
   Scoreboard locks it: `inverse-resolved` 618, `inverse-nonrelational` 6,
   `inverse-dangling` 0. **This closes the field-resolution backlog.**

## View-resolution backlog (from the Phase 0.6 catalog)
1. ~~Register the full Odoo XPath dialect (audit beyond `hasclass`).~~ **Done.**
   Audited every function name appearing in a real inheritance locator across the
   corpus: exactly one is non-XPath-1.0 — `hasclass`. Odoo registers no other
   custom XPath function for view inheritance (stock XPath 1.0 + `hasclass`; see
   `ir_ui_view.py`). So the dialect is **complete**. `xpath.py` makes it *provable*
   rather than assumed: `ODOO_XPATH_FUNCTIONS` is authoritative, `register_dialect`
   and the catalog share one `_implementations()` source, and `assert_complete()`
   (asserted by a test) fails if catalog-'implemented' ≠ registered in either
   direction. The old partial XPath-1.0 builtin list (missing `name`/`local-name`/
   `floor`/…) was completed so an ordinary locator is never mis-flagged as a gap;
   an unknown/unsupported fn is surfaced by `unsupported_functions()` → the
   applicator marks it **DYNAMIC (announced)**, never a silent no-match.
2. ~~Registry-wide ordered effective-arch application (base + cohort).~~ **Done.**
   **Re-scoped by measurement first** (the confirm-live-state step overturned the
   doc's framing a fourth time). The spike's headline 97.29% came from a *loose*
   test — "does the locator's target exist anywhere in the ancestor chain's raw
   arches" — not from a real applicator. A **correct** applicator forced two
   corrections, each confirmed live before coding:
     - **The primary boundary is load-bearing.** A `mode="primary"` view starts its
       own arch tree (Odoo does NOT apply it onto its parent). Walking through
       primaries lumps unrelated trees into one cohort and `replace` directives
       clobber nodes: record-view residual measured **234 → 79** the moment the
       boundary is respected. So the parser captures `mode` (defaulted
       primary/extension) and the builder groups by *root* (nearest primary/base),
       seeding a derived primary from its parent-root's effective arch.
     - **Only TOP-LEVEL arch children are directives.** Odoo applies the inheriting
       view's arch's direct children as the spec; nested `xpath` is payload. The
       spike's `arch.iter()` over-counted nested elements as fake locators,
       inflating both denominator and failures. Counting top-level only is both
       correct and what produced the honest number.
   `EffectiveArchBuilder.build` is registry-wide: it groups every extension view
   under its root, orders each cohort by `ModuleGraph.load_order()` then `priority`
   (reusing the same graph the model resolver rides — order is correctness, not
   reimplemented), applies `xpath`/`position` directives **at each directive's turn**
   (Odoo-faithful: a directive that can't find its target when its turn comes is
   genuinely unresolvable), and returns one `EffectiveView` per primary with
   `contributors` provenance. **Confirmed live on the corpus:** 3487 effective
   views built, **record-view locators 2131 → 2101 resolved = 98.59%**, dangling
   30, dialect gaps 0. Honest state in both directions (RFC §10): a match →
   resolved; a genuine dangle → `unresolved_locators` UNRESOLVED (Phase-4
   candidate, not accused here); a limitation (dialect/interpolation/eval-inherit/
   base-outside-corpus) → DYNAMIC announced.
3. `%(xmlid)d/s` interpolation before evaluation. **Deferred** (17 live locators).
   `xpath.resolve_interpolation`/`has_interpolation` exist; the applicator detects
   and marks them **DYNAMIC (`interpolation-deferred`)** rather than silently
   no-matching. Resolving needs the XML-ID → id map (the semantic index).
4. QWeb `<template>` inheritance (shares the mechanism; own `t-` directives).
   **Deferred** (48 live template locators unresolved of ~900). Templates build an
   effective view but are marked **PARTIAL (`qweb-deferred`)** — improvable, not a
   defect and not runtime-derived.
5. Genuine dangling locators → `view.xpath-target-missing` (Phase 4, gated on #1–2,
   now met). The 30 record danglers are recorded as honest UNRESOLVED state; the
   accusation is the Phase-4 analyzer's job (applicator-before-analyzer). The live
   residue is dominated by the `res.users` multi-form family (targets added in a
   sibling primary tree) plus a couple of real Odoo source typos
   (`res_useurs_view_form_profile`).

## Not in Phase 3 scope (deferred per RFC)
- Durable cache + true incremental activation (structures exist now; turned on at
  LSP, RFC §8/§23). `resolve_one()` and `dependents_of()` are the seams.
- CSV ACL / record-rule / QWeb `t-` semantics beyond inheritance — spiked or
  fixtured when Phase 3 reaches them.
