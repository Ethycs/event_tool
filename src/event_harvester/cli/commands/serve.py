"""Serve command — launch the Flet events browser."""

import logging

from event_harvester.analysis import extract_events_llm
from event_harvester.cli.parse_helpers import resolve_platforms
from event_harvester.event_match import find_fingerprint
from event_harvester.sources import filter_read_sent

logger = logging.getLogger("event_harvester")


async def serve_cmd(args, cfg) -> int:
    """Harvest, extract events, then launch the Flet events browser."""
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

    # Filter out events already approved/declined on a previous run
    events = [ev for ev in events if not find_fingerprint(ev)]
    if not events:
        print("No new events (all previously handled).")
        return 0

    # Gather pipeline stats and rejects for the UI
    from event_harvester.analysis import _extract_events_cloud

    rejects = getattr(extract_events_llm, "_last_rejects", {})
    rejects["llm_past"] = getattr(_extract_events_cloud, "_last_llm_dropped", [])
    rejects["llm_no_events"] = getattr(_extract_events_cloud, "_last_llm_no_events", [])

    stats = {
        "total_messages": len(all_messages),
        "actionable": len(actionable),
        "events": len(events),
    }

    import flet as ft

    from event_harvester.app import EventsApp

    app = EventsApp(events, rejects, stats, cfg)
    await ft.app_async(target=app.main, view=ft.AppView.WEB_BROWSER, port=8550)
    return 0
