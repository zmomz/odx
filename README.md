# odx — Odoo Developer Semantic Engine

An engine that statically reproduces enough of Odoo's semantics to understand a
real codebase, surfaced through thin clients (CLI first; MCP, LSP, CI, web
later). See [RFC.md](RFC.md) for the full design and [ROADMAP.md](ROADMAP.md)
for the phased plan.

## Status

- **Phase 0** — skeleton & config: ✅ CLI runs, verbs stubbed, `init` works,
  engine/runtime/clients split enforced by `tests/test_arch/`.
- **Phase 0.5** — resolver spike on real modules (validate RFC §3): see
  [tests/spike/README.md](tests/spike/README.md).

## Quick start

```bash
python -m pip install -e ".[dev]"

odx --help
odx init                 # detect runtime, write .odx.toml
odx check                # (stub until Phase 4)
```

## Architecture (RFC §5)

```
clients/  thin renderers (cli, then mcp/lsp/ci/web)
   │ call
public API  (odx.engine.api.Engine — typed, returns plain data)
   │
engine/   pure static semantics    runtime/  infrastructure (sibling, not a dep)
```

`engine/` imports nothing from `runtime/` or `clients/` — enforced by
`tests/test_arch/`.

## Develop

```bash
pytest                          # all tests
pytest tests/test_arch          # architecture rules only
python -m tests.spike.run       # Phase 0.5 spike (after cloning a corpus)
```
