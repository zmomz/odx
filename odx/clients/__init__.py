"""clients — thin renderers over the public API (RFC §13).

Each client calls odx.engine.api.Engine (and, where needed, odx.runtime) and
renders results in its idiom. No domain logic lives here. cli is first; mcp
second (to validate the abstraction, §19); lsp/ci/web later.
"""
