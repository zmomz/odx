# Phase 0.5 — Resolver Spike: Results & Go/No-Go

**Decision: GO.** ✅

The central bet of the RFC (§2, §3) — *"we can statically reproduce enough of
Odoo's semantics to understand a real codebase without booting Odoo"* — holds.
On a large, messy, real corpus, static resolution is strong and the failures
cluster into a small number of well-understood, scope-able mechanisms. The
truly-undecidable set is tiny and confined to framework internals and Odoo's own
test fixtures — exactly the shape §3 requires to proceed.

> Reproduce: `python -m tests.spike.run` (after cloning the corpus — see
> [README.md](README.md)). Raw output in [out/](out/).

---

## Corpus

Real code, cloned at tag **17.0** (gitignored under `tests/corpus/`):

| repo | what | modules | .py files |
|---|---|--:|--:|
| `odoo/odoo` | Odoo core + all bundled addons | ~700 | 6,991 |
| `OCA/server-tools` | framework/utility mixins (heavy `_inherit`) | 46 | 357 |
| `OCA/account-financial-tools` | accounting (heavy `_inherit`) | 32 | 262 |
| `OCA/web` | web/UI addons | 42 | 178 |
| **total** | | **729** | **7,788** |

3,604 model class definitions · 14,169 field definitions · 1,297 distinct
models resolved into the registry.

---

## Scoreboard (orientation, not the gate)

| metric | result | RFC §3 target | |
|---|--:|--:|:--:|
| Files parsed without crashing | **100.0%** | 100% | ✅ |
| Models resolved (name determinable) | **99.42%** | ≥95% | ✅ |
| `_inherit` references resolved | **97.27%** | ≥90% | ✅ |
| Relational fields w/ literal comodel | 91.98% | (see note) | ⚠️ |

Per-repo (all four exceed the model/inherit targets):

| repo | parse | models named | inherit resolved | rel. literal comodel |
|---|--:|--:|--:|--:|
| odoo | 100% | 99.37% | 97.22% | 91.7% |
| server-tools | 100% | 100% | 100% | 99.02% |
| account-financial-tools | 100% | 100% | 96.08% | 93.69% |
| web | 100% | 100% | 100% | 100% |

**Note on the "fields ≥95%" target.** *Field extraction* is ~100% (every
`fields.X(...)` is captured). The 91.98% figure is a stricter sub-quality: the
share of relational fields whose comodel is a **literal string**. The ~8%
remainder is not undecidable — it's `related=` fields whose comodel is inferred
from the related chain (271 cases) or comodel-less Reference-style fields (82).
Both are resolvable with a related-chain follower (Phase 3 work), which is why
they are tagged **hard**, not out-of-scope. See below.

---

## Failure catalog (the actual deliverable)

394 flagged sites total, tagged by nature:

| tag | count | meaning |
|---|--:|---|
| **hard** | 359 | resolvable statically, but needs real resolver work |
| **out-of-scope** | 33 | fundamentally undecidable statically |
| **boundary** | 2 | not a failure — target lives in an un-cloned repo |
| **fixable** | 0 | *(one bug found and fixed during the spike — see below)* |

### hard (359) — clusters into exactly two mechanisms

| pattern | count | resolution path (Phase 3) |
|---|--:|---|
| `comodel-via-related` | 271 | follow the `related=` chain to infer the comodel |
| `comodel-absent` | 82 | Reference/Many2oneReference + inferred-comodel cases |
| `one2many-inverse-absent` | 6 | scan the comodel for the inverse many2one |

They do **not** scatter across ordinary modules — they are two field-resolution
features (related-chain inference, one2many inverse lookup), both well-understood.
This *is* the Phase 3 scope for field resolution.

### out-of-scope (33) — the genuine static/runtime boundary

| pattern | count | why undecidable |
|---|--:|---|
| `dynamic-name` | 16 | `_name` not a literal (built from runtime data) |
| `dynamic-inherit` | 11 | `_inherit` not a literal |
| `dynamic-comodel` | 6 | relational comodel not a literal |

**Where they live matters more than the count.** 30 of 33 are inside Odoo's own
*test fixtures* (`test_new_api`, `test_impex`, `test_base_import`) that
deliberately exercise metaprogramming. In real, non-test code the truly-dynamic
cases number exactly **3**, all in framework internals:

- `base/models/ir_model.py` — the meta-model that builds models from DB rows.
  This is *the* canonical runtime-data-driven case the RFC calls out; it is
  correctly out-of-scope for static analysis and belongs to the runtime bridge.
- `website/models/ir_module_module.py`, `base/models/res_partner.py` — isolated
  dynamic constructs.

Across all of sale / purchase / mrp / stock / account / hr and the entire OCA
sample, ordinary business modules are ~100% statically resolvable.

### boundary (2) — corpus artifacts, not failures

`_inherit` targets defined in OCA repos we didn't clone:
`website.published.multi.mixin` (13 refs, → `OCA/website`) and
`report.report_xlsx.abstract` (1 ref, → `OCA/reporting-engine`). They vanish if
those repos are added to the corpus.

### fixable (0) — one bug found and closed mid-spike

The first run reported 36 `nameless-model` failures (models = 98.4%). All 36
traced to **one** parser gap: chained assignment `_name = _description = '...'`
(common in Odoo test fixtures) wasn't handled. Fixing it raised models to
99.42% and emptied the fixable bucket — a textbook example of §3's warning that
*"a 70% caused by one fixable bug is better than a 92% caused by ten unfixable
patterns."* The number moved; the catalog shape is what mattered.

---

## Why this is GO (per RFC §3 gate)

The gate is the **catalog's shape**, not the percentage:

1. **Core resolution is promising** — `_inherit` chains and effective models
   resolve at 97–99% on real, cross-module, cross-repo code.
2. **Failures cluster in `fixable` / `hard` / cleanly `out-of-scope`**, not
   scattered through ordinary modules. `hard` = two field-resolution features;
   `out-of-scope` = a handful of framework/test constructs.
3. **The static/runtime boundary is sharp and small** — the undecidable set is
   ~0.9% of models / 0.23% of fields, and in production code it is confined to
   `ir_model`-style meta-machinery, which the runtime bridge (§12) is designed
   to cover.

No `hard`/`out-of-scope` failures riddle ordinary modules → the §3 "rethink the
approach" condition is not triggered.

---

## What this scopes for Phase 3

The catalog is the Phase 3 resolver backlog:

- **Field resolution must include** (in priority order):
  1. `related=` comodel/type inference by walking the related chain (271 sites).
  2. Reference / Many2oneReference / comodel-less handling (82 sites).
  3. one2many inverse discovery by scanning the comodel (6 sites).
- **`_inherits` (delegation)** works in the spike but needs first-class field
  provenance in Phase 3 (RFC §17 flags it as co-evolving with the builder).
- **Explicitly unsupported (route to runtime bridge, don't fake it):**
  `dynamic-name`, `dynamic-inherit`, `dynamic-comodel`, fields-in-loops,
  `setattr`/monkeypatch field injection. Emit a clear "dynamic — not statically
  resolvable" marker rather than a wrong answer.
- **Corpus for the permanent harness** should add `OCA/website` and
  `OCA/reporting-engine` to close the two boundary refs.

## Known limitations of the *spike* (not the engine)

This spike is deliberately rough (RFC §19: throwaway-grade). It does **not** yet
parse XML view inheritance (`xpath`/`position`), CSV ACLs, or QWeb — those are
Phase 3 index concerns (§7) and a separate, larger source of "hard" work not
measured here. The Python-side model/field/inherit resolution measured above is
the load-bearing assumption, and it holds.
