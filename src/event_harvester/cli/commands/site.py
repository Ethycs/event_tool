"""Site command — serve the events site, refreshed on a schedule."""

import logging

from event_harvester.observability.tracing import get_tracer
from event_harvester.ui.display import BOLD, DIM, RESET, YELLOW

logger = logging.getLogger("event_harvester")
tracer = get_tracer("event_harvester.cli.commands.site")


async def site_cmd(args, cfg) -> int:
    """Build the events site, then serve it unless --build-only."""
    from event_harvester.servers.site import SITE_HTML, SiteState, refresh, run_site

    with tracer.start_as_current_span("cli.site") as span:
        span.set_attribute("site.host", args.host)
        span.set_attribute("site.port", args.port)
        span.set_attribute("site.interval_hours", args.interval_hours)

        if args.build_only:
            state = SiteState()
            snapshot = await refresh(state, cfg, harvest=not args.no_harvest_on_start)
            if snapshot["error"]:
                print(f"  {YELLOW}Build failed: {snapshot['error']}{RESET}")
                return 1
            print(f"\n  {BOLD}{snapshot['events']}{RESET} event(s) -> {SITE_HTML}")
            print(f"  {DIM}Open it directly, or serve it: event-harvester site{RESET}\n")
            return 0

        print(f"\n  {BOLD}Events site{RESET}  http://{args.host}:{args.port}")
        print(f"  {DIM}refresh every {args.interval_hours}h · Ctrl-C to stop{RESET}")
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            print(f"  {YELLOW}No authentication — do not expose this without a "
                  f"proxy in front.{RESET}")
        print()

        return run_site(
            cfg,
            host=args.host,
            port=args.port,
            interval_hours=args.interval_hours,
            harvest_on_start=not args.no_harvest_on_start,
            schedule=not args.no_schedule,
        )
