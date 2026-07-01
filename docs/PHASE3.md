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
| `parser/python.py` | .py → `RawModule` (+ Location + Resolution) | **graduated** (100% parse, Location on 100% of models, catalog reproduced as Resolution states) |
| `parser/xml.py` | XML → `RawView` + locators (+ Location) | graduate `tests/spike/xml_parser.py` |
| `parser/csv.py` | ACL CSV → `AccessRule` | new (unspiked, low risk) |
| `model/raw.py` | public `RawModel`/`RawField`/`RawView` types | implemented |
| `model/resolver.py` | `RawModel → ResolvedModel`, provenance, `related=` worklist fixpoint, field-override attribute merge | **implemented** (backlog items 1 & 2 done) |
| `model/xpath.py` | Odoo XPath **dialect gate** + `%(id)d` interpolation | `hasclass` impl'd |
| `model/arch.py` | `EffectiveArchBuilder` — ordered cohort application | stub + scope |

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
3. one2many inverse discovery by scanning the comodel — 6 sites.

## View-resolution backlog (from the Phase 0.6 catalog)
1. Register the full Odoo XPath dialect (audit beyond `hasclass`).
2. Registry-wide ordered effective-arch application (base + cohort).
3. `%(xmlid)d/s` interpolation before evaluation.
4. QWeb `<template>` inheritance (shares the mechanism; own `t-` directives).
5. Genuine dangling locators → `view.xpath-target-missing` (Phase 4, gated on #1–2).

## Not in Phase 3 scope (deferred per RFC)
- Durable cache + true incremental activation (structures exist now; turned on at
  LSP, RFC §8/§23). `resolve_one()` and `dependents_of()` are the seams.
- CSV ACL / record-rule / QWeb `t-` semantics beyond inheritance — spiked or
  fixtured when Phase 3 reaches them.
