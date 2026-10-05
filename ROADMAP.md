# Roadmap

Derived from RFC §19. Ordering: validate the core assumption (0.5) → ship a real
client to shape the API (0–4) → gate-check the abstraction with a second client
(5) → add clients cheaply (6+).

| Phase | What | Status |
|------:|------|--------|
| 0   | Skeleton + `init`; engine/runtime/clients split; `test_arch/` | ✅ done |
| 0.5 | Resolver spike: Python model/field/`_inherit` (→ **GO**) | ✅ done |
| 0.6 | Resolver spike: XML view inheritance (`inherit_id`/`xpath`) (→ **GO**) | ✅ done |
| 1   | Daily-loop runtime wrappers (`run`/`logs`/`shell`/`test`/`db`) | ✅ done |
| 2   | Scaffolding via `EditPlan` (`new`/`add`/`inherit`, field-type matrix) | ✅ done |
| 3   | Engine pipeline: parser → resolver → effective arch → index → semantic API | ✅ done |
| 4   | Analyzers + `check` / `report` / `fix` (shapes `Diagnostic`) | ✅ done |
| 5   | MCP client + API validation gate ([docs/API_GATE.md](docs/API_GATE.md)) | ✅ done |
| 6   | Runtime-backed introspection (`doctor`, `diff db`, `report --with-runtime`, `db dump/restore/anonymize`) | ✅ done |
| 7+  | LSP · migration (`upgrade`) · web · CI · durable cache & incremental | ✅ done |

## Live numbers (733-module corpus: Odoo 17 + 3 OCA repos)

| Measure | Value |
|---|---|
| Files parsed without crashing | 100% `.py`, 99.98% `.xml` |
| Models resolved | 1,304 |
| §7 references indexed | ~148,000 across 33 enumerated kinds |
| Record-view locators resolved by the effective-arch applicator | **2,261 / 2,261 (100%)** |
| QWeb template locators resolved | 876 / 900 (the 24 others belong to optional *inactive* views: PARTIAL, not applied by default) |
| `odx check --all` findings on the whole corpus | 61 (0 xpath false accusations; e.g. genuine: `hr_skills` missing compute methods, `l10n_latam_check` undeclared `account_payment` dependency) |
| Cold build / warm build (durable cache) | ~19 s / ~11 s for 733 modules; a single project re-check is incremental |

Phase 3/4 details: [docs/PHASE3.md](docs/PHASE3.md). Clients: CLI (`odx`), MCP
(`odx-mcp`), LSP (`odx-lsp`), web (`odx-web`), CI (`odx check -f github|sarif`,
[docs/CI.md](docs/CI.md)).

## Decisions locked (RFC §23 open questions)

- **Binary/package name:** `odx`.
- **Config format:** TOML (`.odx.toml`) — stdlib `tomllib`, no read dependency.
- **Phase 0.5 corpus:** cloned OCA repos under `tests/corpus/` (gitignored).
- **Resolver depth for v1:** set by the catalog — static where the corpus proves
  it, `Resolution.DYNAMIC` (announced, never guessed) beyond.
- **Runtime comms:** XML-RPC for `diff db` snapshots (stdlib, works against any
  instance), `odoo-bin --stop-after-init` + log for `doctor`.
- **LSP host:** in-process, stdlib JSON-RPC (no pygls dependency).
- **Web/CI transport:** in-process — web is a read-only stdlib HTTP layer over
  the Engine; CI renders `Diagnostic`s as GitHub annotations / SARIF.
- **Incrementality & cache:** activated — per-file content-hash re-parse +
  invalidation record; durable parse cache outside the project (opt-in, on by
  default in configs written by `odx init`).

Remaining open (§23): time budget, RFC split timing.
