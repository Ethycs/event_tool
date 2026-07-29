"""Central FastMCP gateway.

Fronts one or more backend MCP servers behind a single endpoint, so any
number of editors (VSCode, Cursor, etc.) register just this gateway's URL
instead of each backend separately. Built on FastMCP 3.x ``create_proxy``.

Backends are declared in the standard MCPConfig ``mcpServers`` shape — the
same JSON you'd paste into a client. event-harvester is proxied over its
streamable-http service so the heavy pipeline process stays warm and is
shared across every gateway client (no per-session cold start, no leaked
stdio subprocesses).

Run order
---------
    # 1. the harvester, as a persistent service
    pixi run event-harvester mcp --transport streamable-http --port 8765

    # 2. this gateway
    pixi run gateway

Then point any editor at  http://127.0.0.1:9000/mcp  (single endpoint).

Env overrides: EH_MCP_URL, GATEWAY_HOST, GATEWAY_PORT.
"""

from __future__ import annotations

import os

from fastmcp.server import create_proxy

# Backend(s) behind the gateway. Add more entries here and they all surface
# behind the one gateway endpoint, namespaced by their key
# (e.g. ``event-harvester_harvest_events``).
config = {
    "mcpServers": {
        "event-harvester": {
            "url": os.environ.get("EH_MCP_URL", "http://127.0.0.1:8765/mcp"),
        },
        # Examples of additional backends you can centralize here:
        # "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]},
        # "files":  {"command": "uvx", "args": ["mcp-server-filesystem", "C:/some/dir"]},
        # "remote": {"url": "http://127.0.0.1:9100/mcp"},
    }
}

gateway = create_proxy(config, name="gateway")


if __name__ == "__main__":
    gateway.run(
        transport="http",
        host=os.environ.get("GATEWAY_HOST", "127.0.0.1"),
        port=int(os.environ.get("GATEWAY_PORT", "9000")),
        path="/mcp",
    )
