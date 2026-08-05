"""Entry point for `pixi run gateway`.

The gateway itself lives in `event_harvester.servers.gateway` — it moved into
the package so it could be tested and so the auth and exposure guards are not
sitting in a loose script at the repo root. This file is the launcher and
nothing else.

    # 1. the harvester, as a persistent MCP backend
    pixi run event-harvester mcp --transport streamable-http --port 8765

    # 2. this gateway
    pixi run gateway

Then point any client at  http://127.0.0.1:9000/mcp  (single endpoint).

To run both as Windows services instead:

    event-harvester service install --service mcp
    event-harvester service install --service gateway

Configuration is entirely environment-driven; see the module docstring in
`src/event_harvester/servers/gateway.py` for the variables, and
`docs/02 - Implementation/02 - Deploy Playbook.md` §5c for exposing it as a
Claude custom connector.
"""

from __future__ import annotations

import argparse
import logging
import sys

from event_harvester.servers.gateway import GatewayError, run_gateway

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Serve the MCP dispatch gateway.")
    parser.add_argument(
        "--reload", action="store_true",
        help="Dev: restart on source changes (uvicorn --reload). The exposure "
             "guard still applies — it runs in the worker that binds.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        sys.exit(run_gateway(reload=args.reload))
    except GatewayError as exc:
        print(f"\n  gateway: {exc}\n", file=sys.stderr)
        sys.exit(2)
