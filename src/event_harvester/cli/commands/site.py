"""Site command — serve the events site, refreshed on a schedule."""

import logging

from event_harvester.observability.tracing import get_tracer
from event_harvester.ui.display import BOLD, DIM, RESET, YELLOW

logger = logging.getLogger("event_harvester")
tracer = get_tracer("event_harvester.cli.commands.site")


async def site_cmd(args, cfg) -> int:
    """Build the events site, then serve it unless --build-only."""
    from event_harvester.servers.site import (
        SITE_CALENDAR,
        SITE_HTML,
        SiteState,
        normalise_prefix,
        refresh,
        run_site,
        run_site_reload,
    )

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
            print(f"  {DIM}{'':>{len(str(snapshot['events']))}}   calendar -> "
                  f"{SITE_CALENDAR}{RESET}")
            print(f"  {DIM}Open it directly, or serve it: event-harvester site{RESET}\n")
            return 0

        prefix = normalise_prefix(getattr(args, "root_path", ""))
        base = f"http://{args.host}:{args.port}{prefix}"
        print(f"\n  {BOLD}Events site{RESET}  {base}")
        # Named here because a page nobody can find is a page nobody has. Both
        # /calendar and /admin were reachable and unlinked before this.
        print(f"  {DIM}{base}/calendar   month grid{RESET}")
        print(f"  {DIM}{base}/admin      caps, keywords, profiles{RESET}")
        if prefix:
            print(f"  {DIM}also unprefixed on this port; the pages link "
                  f"relatively either way{RESET}")
        print(f"  {DIM}refresh every {args.interval_hours}h · Ctrl-C to stop{RESET}")
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            print(f"  {YELLOW}No authentication — do not expose this without a "
                  f"proxy in front.{RESET}")
        print()

        if getattr(args, "reload", False):
            # Dev affordance: restarts on source change, and does *not* harvest
            # per restart unless asked — a save every few seconds would
            # otherwise open browsers and spend tokens every few seconds.
            print(f"  {DIM}reload: watching src/ · serving cached events{RESET}\n")
            return run_site_reload(
                host=args.host,
                port=args.port,
                interval_hours=args.interval_hours,
                harvest_on_start=not args.no_harvest_on_start and args.harvest_on_reload,
                schedule=not args.no_schedule and args.harvest_on_reload,
                root_path=prefix,
            )

        return await run_site(
            cfg,
            host=args.host,
            port=args.port,
            interval_hours=args.interval_hours,
            harvest_on_start=not args.no_harvest_on_start,
            schedule=not args.no_schedule,
            root_path=prefix,
        )
