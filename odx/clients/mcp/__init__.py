"""mcp — MCP server client (RFC §13, built second, §19 validation gate).

Exposes the public API as tools an AI calls, so it queries ground truth instead
of hallucinating fields / _inherit chains. Contracts serialize to JSON nearly
directly. Tool surface is TBD until built (Phase 5).
"""
