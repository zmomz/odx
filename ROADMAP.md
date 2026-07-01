# Roadmap

Derived from RFC §19. Ordering: validate the core assumption (0.5) → ship a real
client to shape the API (0–4) → gate-check the abstraction with a second client
(5) → add clients cheaply (6+).

| Phase | What | Status |
|------:|------|--------|
| 0   | Skeleton + `init`; engine/runtime/clients split; `test_arch/` | ✅ done |
| 0.5 | Resolver spike: Python model/field/`_inherit` (→ **GO**) | ✅ done |
| 0.6 | Resolver spike: XML view inheritance (`inherit_id`/`xpath`) (→ **GO**) | ✅ done |
| 1   | Daily-loop runtime wrappers (`run`/`logs`/`shell`/`db`) | ⬜ |
| 2   | Scaffolding via `EditPlan` (`new`/`add`, field-type matrix) | ⬜ |
| 3   | Engine pipeline: parser → resolver → index → semantic API | 🔄 in progress |
| 4   | Analyzers + first `report` (shapes `Diagnostic`) | ⬜ |
| 5   | MCP client + API validation gate | ⬜ |
| 6   | Runtime-backed introspection (`diff`/`doctor`/`report --with-runtime`) | ⬜ |
| 7+  | LSP · migration · web · CI · durable cache & incremental | ⬜ |

**Phase 3 sub-status** (see `docs/PHASE3.md` for the backlog): load graph ✅ ·
Python parser ✅ (graduated) · model resolver ✅ (field-resolution backlog closed:
`related=` fixpoint ✅, field-override attribute merge ✅, one2many inverse
discovery ✅) · XML parser ✅ (graduated) · XPath dialect gate ✅ (audited
complete — `hasclass` only) · XML view `EffectiveArchBuilder` 🔄 (registry-wide
ordered record-view application done — 98.59% live; items 3–5 deferred: `%(xmlid)d`
interpolation, QWeb `<template>` inheritance, and the Phase-4
`view.xpath-target-missing` analyzer) · semantic index ⬜ · semantic API ⬜.

## Decisions locked (RFC §23 open questions)

- **Binary/package name:** `odx`.
- **Config format:** TOML (`.odx.toml`) — stdlib `tomllib`, no read dependency.
- **Phase 0.5 corpus:** cloned OCA repos under `tests/corpus/` (gitignored).

Remaining open questions (§23): resolver depth for v1 (set by the 0.5 catalog),
runtime comms (XML-RPC vs JSON-RPC vs `odoo shell`), web/CI transport, LSP host,
RFC split timing.
