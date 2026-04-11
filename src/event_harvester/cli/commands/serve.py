"""Serve command — run a local web server to review and approve events."""

import logging

from event_harvester.analysis import extract_events_llm
from event_harvester.cli.parse_helpers import resolve_platforms
from event_harvester.sources import filter_read_sent

logger = logging.getLogger("event_harvester")


async def serve_cmd(args, cfg) -> int:
    """Harvest, extract events, then start the local review server."""
    if args.days is not None:
        cfg.days_back = args.days

    from event_harvester.harvest import harvest_messages

    platform_kwargs = resolve_platforms(args.only, args.skip)
    all_messages = await harvest_messages(cfg, **platform_kwargs)
    if not all_messages:
        print("No messages found.")
        return 0

    actionable = filter_read_sent(all_messages)
    summary, events = extract_events_llm(
        actionable, cfg.days_back, cfg.llm, caps=cfg.caps,
    )

    if not events:
        print("No events extracted.")
        return 0

    from event_harvester.server import serve_events
    serve_events(events)
    return 0
