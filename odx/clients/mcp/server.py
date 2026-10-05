"""`odx-mcp` — the MCP server (stdio). Thin registration of `OdxTools` methods
as MCP tools; all behaviour lives in tools.py (and, below it, the engine).

Configure in an MCP host, e.g.:
    {"mcpServers": {"odx": {"command": "odx-mcp", "args": ["--root", "/path/to/project"]}}}
"""

from __future__ import annotations

import argparse
import inspect
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from odx.clients.mcp.tools import OdxTools

INSTRUCTIONS = """\
odx statically understands the Odoo addons in this project (models, fields,
_inherit/_inherits chains, effective views, every reference to a field/model,
security, xml-ids) WITHOUT booting Odoo. Query it instead of guessing:
- before using a field: field_exists / get_model
- before renaming or removing something: find_references
- after editing: check (and reload with the changed files)
A `state` of "dynamic" means the answer is not knowable statically: say so or ask
the running instance; never invent fields. Changes are proposed with plan_* tools
and only written by apply_plan after the user approves."""

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
PROPOSE = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)

TOOLS: dict[str, ToolAnnotations] = {
    "list_modules": READ_ONLY, "list_models": READ_ONLY, "get_model": READ_ONLY,
    "field_exists": READ_ONLY, "find_references": READ_ONLY, "check": READ_ONLY,
    "report": READ_ONLY, "validate_field": READ_ONLY, "upgrade_check": READ_ONLY,
    "reload": READ_ONLY,
    "plan_add_field": PROPOSE, "plan_new_model": PROPOSE, "plan_new_module": PROPOSE,
    "plan_add_view": PROPOSE, "plan_add_menu": PROPOSE, "plan_fix": PROPOSE,
    "plan_upgrade": PROPOSE,
    "apply_plan": WRITE,
}


def build_server(root: Path | str = ".") -> FastMCP:
    tools = OdxTools(root)
    server = FastMCP("odx", instructions=INSTRUCTIONS)
    for name, ann in TOOLS.items():
        fn = getattr(tools, name)
        server.add_tool(fn, name=name, description=inspect.getdoc(fn) or name,
                        annotations=ann)
    server._odx_tools = tools            # for tests / embedding
    return server


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="odx-mcp", description="odx MCP server (stdio)")
    ap.add_argument("--root", default=".", help="project root (.odx.toml dir or addons dir)")
    args = ap.parse_args(argv)
    build_server(args.root).run(transport="stdio")


if __name__ == "__main__":
    main()
