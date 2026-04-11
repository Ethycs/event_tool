"""Watch command — poll continuously for new messages."""

import logging

from event_harvester.cli.parse_helpers import resolve_platforms
from event_harvester.watch import watch_mode

logger = logging.getLogger("event_harvester")


async def watch_cmd(args, cfg) -> int:
    """Run watch mode. Currently supports Discord and Telegram only."""
    platform_kwargs = resolve_platforms(args.only, args.skip)
    no_telegram = platform_kwargs["no_telegram"]
    no_discord = platform_kwargs["no_discord"]

    # Warn if user asked for sources watch_mode doesn't support
    unsupported = {
        p for p in ("gmail", "signal", "web")
        if not platform_kwargs[f"no_{p}"]
    }
    if unsupported and (args.only is not None):
        logger.warning(
            "Watch mode only supports Discord and Telegram. Ignoring: %s",
            sorted(unsupported),
        )

    await watch_mode(cfg, args.interval, no_telegram, no_discord)
    return 0
