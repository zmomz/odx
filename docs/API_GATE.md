# Phase 5 — API validation gate

*RFC §19: "Stop adding to the API; audit for client-neutrality (anything read
CLI-shaped?). Build MCP against the held-still version; its friction reveals API
flaws. Clean drop-in = right; fights = fix the API now, before LSP/CI/web."*

## Method

1. Froze `odx.engine.api.Engine` + `odx.engine.contracts` as they stood after
   Phase 4 (CLI the only client).
2. Built the MCP client (`odx/clients/mcp/`) as a *tool layer*
   (`OdxTools`: JSON in, JSON out) plus a thin FastMCP registration.
3. Every place the MCP layer had to do more than call `Engine` and serialize a
   contract was logged as friction, then fixed **in the engine**.
4. Locked the result with architecture tests (`tests/test_arch`): clients may
   import only `odx.engine.api`, `odx.engine.contracts`, `odx.engine.config`,
   and may not reach through `Engine.api` (the SemanticAPI).

## Findings → fixes

| # | Friction (MCP / CLI had to…) | Verdict | Fix |
|---|---|---|---|
| 1 | list models with kind / definer / completeness by reaching into `engine.api.resolved_model` | API gap | `Engine.list_models(prefix, module) -> list[ModelSummary]` + `ModelSummary` contract |
| 2 | fetch one field's info via `engine.api.get_field` | API gap | `Engine.get_field(model, field) -> FieldInfo` |
| 3 | trigger the build to show progress by touching `engine.api` | API gap | `Engine.warm()` |
| 4 | resolve the default `--module` with `ModuleGraph.discover` / `MANIFEST_NAMES` (engine internals) | API gap | `Engine.discover_modules()`, `Engine.module_containing(path)` |
| 5 | build report/controller specs from `odx.engine.generators` | contract in the wrong place | `ReportSpec` / `ControllerSpec` moved to `contracts.py` |
| 6 | call `guess_type` / `normalize_type` from generators | API gap | `Engine.guess_field_type`, `Engine.normalize_field_type` |
| 7 | catch `GeneratorError` from an internal module | API gap | re-exported from `odx.engine.api` |
| 8 | `field_exists` returns `bool`, but an AI must distinguish "absent" from "can't tell" | contract too narrow | `Engine.field_state() -> Resolution` (kept `field_exists` for the RFC signature); MCP passes the state + a "ask the runtime" hint verbatim |
| 9 | apply an `EditPlan` | **not a flaw** — application is a client concern (RFC §10) | shared, client-side `odx/clients/edits.py` (plain text, no Rich) used by CLI *and* MCP |
| 10 | trim long diagnostic lists for a model's context window | **not a flaw** — presentation | done in the MCP layer |

Nothing in the API was CLI-shaped: no method returns formatted text, takes a
Rich/Typer object, or depends on cwd. The one CLI-ism found was in a *client*
(`scaffold_cmds` re-deriving module discovery), now routed through the API.

## Result

The MCP client is a clean drop-in: 18 tools, each a call to one `Engine` method
plus `serialize.to_jsonable` — verified end-to-end over real stdio
(`tests/test_mcp/test_mcp_tools.py::test_stdio_round_trip`). Mutation stays
two-step (C3): `plan_*` tools return an EditPlan + diff + id; only `apply_plan`
writes, so the MCP host's tool-approval UI is the approval step.

Not a compatibility freeze (RFC §17): zero external consumers, so the API stays
"stabilized, not frozen" — the architecture tests are what hold it still.
