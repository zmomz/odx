"""odx — Odoo Developer Semantic Engine.

Top-level package. The architecture is a hard three-way split (see RFC §5, §20):

    engine/   pure static semantics; imports nothing from runtime/ or clients/
    runtime/  infrastructure (rpc/shell/docker/pg); a *sibling* of the engine
    clients/  thin renderers (cli first, then mcp/lsp/ci/web)

`tests/test_arch/` enforces this split from line one.
"""

__version__ = "0.0.1"
