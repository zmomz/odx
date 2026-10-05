# odx — Odoo Developer Semantic Engine

An engine that statically reproduces enough of Odoo's semantics to understand a
real codebase — resolved models, effective views, and an index of every
reference to every field and model — surfaced through thin clients: CLI, MCP,
LSP, web and CI. No Odoo boot required. See [RFC.md](RFC.md) for the design and
[ROADMAP.md](ROADMAP.md) for status and live numbers.

## Quick start

```bash
python -m pip install -e ".[dev]"      # or: pip install -e .  (add [mcp] for odx-mcp)

odx init                    # detect runtime, write .odx.toml (point source_path at odoo)
odx check                   # static analysis of your modules (exit 1 on errors)
odx why sale.order.partner_id        # where is this used? (terse)
odx impact sale.order.partner_id     # every location, by reference kind
odx report                  # scored health report (+ --with-runtime)
odx fix --dry-run           # safe fixes (ACL rows, manifest depends) as a diff
odx upgrade check --to 18.0 # migration findings; `upgrade apply` rewrites the mechanical ones
```

Scaffolding (`new module|model|wizard|report|controller`, `add field|view|menu`,
`inherit view`) shows the plan before writing; daily loop (`run --watch`, `logs`,
`shell`, `test`, `db list|new|drop|copy|dump|restore|anonymize`) wraps docker /
venv / source runtimes; `doctor` installs into a throwaway DB and maps failures
back to source; `diff db` compares code with a live database.

## Clients

| client | entry point | what it does |
|---|---|---|
| CLI | `odx` | the grammar of RFC §14 — `odx <verb> <noun> [target] [--flags]` |
| MCP | `odx-mcp --root .` | tools for AI assistants: tri-state `field_exists`, `find_references`, `check`, `plan_*` + `apply_plan` |
| LSP | `odx-lsp` | diagnostics, hover, go-to-definition, find-references, field completion, quick fixes |
| Web | `odx-web --root .` | read-only model browser, reference graph, report, fix preview |
| CI | `odx check -f github` / `-f sarif` | PR annotations / code scanning ([docs/CI.md](docs/CI.md)) |

## Architecture (RFC §5–§7)

```
clients/  cli · mcp · lsp · web · ci  — render only
   │ call (public surface only: Engine, contracts, config — enforced by tests/test_arch)
odx.engine.api.Engine
   │
engine/   loadorder → parser (py/xml/csv) → model resolver + effective-arch builder
          → semantic index (33 reference kinds + XML-ID graph) → SemanticAPI
          → analyzers (Diagnostic) · generators (EditPlan) · report · migration
runtime/  docker/venv/source commands · xml-rpc snapshots · install probe
          (a sibling of the engine — never imported by it)
```

Every fact carries a `Resolution` (STATIC / DYNAMIC / PARTIAL / UNRESOLVED): the
engine only accuses what it can prove; what it cannot see is announced, never
guessed.

## Develop

```bash
pytest                          # all tests (corpus tests skip without tests/corpus)
pytest tests/test_arch          # architecture + API-gate rules
python -m tests.spike.run_arch  # effective-arch scoreboard (after cloning a corpus)
```
