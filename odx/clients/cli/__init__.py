"""cli — the first client: Typer + Rich (RFC §14).

Renders the engine's contracts and owns the CLI grammar
(`odx <verb> <noun> [target] [--flags]`). Read-only vs mutating is a hard law
mirroring the API's return types.
"""
