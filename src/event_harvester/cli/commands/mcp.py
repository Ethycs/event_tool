"""MCP command — expose Event Harvester as a Model Context Protocol server."""

import logging

logger = logging.getLogger("event_harvester")


async def mcp_cmd(args, cfg) -> int:
    """Run the MCP server until the client disconnects or the process stops."""
    from event_harvester.servers.mcp_server import run_server_async

    log_level = "DEBUG" if args.verbose else "WARNING" if args.quiet else "INFO"
    logger.info("Starting MCP server with %s transport", args.transport)
    await run_server_async(
        transport=args.transport,
        host=args.host,
        port=args.port,
        path=args.path,
        log_level=log_level,
    )
    return 0
