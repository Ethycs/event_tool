"""Watch mode — poll for new messages continuously."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from event_harvester.config import AppConfig
from event_harvester.display import BOLD, DIM, RESET, print_message
from event_harvester.sources.discord import find_discord_cache, read_discord_messages
from event_harvester.sources.telegram import create_persistent_client, read_telegram_messages

logger = logging.getLogger("event_harvester.watch")

# Cap the seen set to prevent unbounded memory growth
_MAX_SEEN_IDS = 50_000


async def watch_mode(
    cfg: AppConfig,
    interval: int,
    no_telegram: bool,
    no_discord: bool,
) -> None:
    """Poll cache + Telegram every interval seconds, print new messages live."""
    seen: set[str] = set()
    cache_path = find_discord_cache(cfg.discord.cache_path) if not no_discord else None
    seed_cutoff = datetime.now(timezone.utc) - timedelta(hours=1)

    print(f"\n{BOLD}Watching for new messages (poll every {interval}s).{RESET}")
    print(f"{DIM}Ctrl-C to stop.{RESET}\n")

    # Keep a persistent Telegram client for watch mode
    tg_client = None
    if not no_telegram and cfg.telegram.is_configured:
        tg_client = await create_persistent_client(cfg.telegram)

    try:
        # Seed with recent IDs
        if not no_discord:
            for m in read_discord_messages(seed_cutoff, cache_path):
                seen.add(m["id"])
        if tg_client:
            for m in await read_telegram_messages(
                seed_cutoff, cfg.telegram, client=tg_client
            ):
                seen.add(m["id"])

        print(f"{DIM}Seeded {len(seen)} recent ID(s). Watching ...{RESET}\n")

        while True:
            await asyncio.sleep(interval)
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            new: list[dict] = []

            if not no_discord:
                for m in read_discord_messages(cutoff, cache_path):
                    if m["id"] not in seen:
                        seen.add(m["id"])
                        new.append(m)
            if tg_client:
                for m in await read_telegram_messages(
                    cutoff, cfg.telegram, client=tg_client
                ):
                    if m["id"] not in seen:
                        seen.add(m["id"])
                        new.append(m)

            for msg in sorted(new, key=lambda m: m["timestamp"]):
                print_message(msg)

            # Cap memory usage
            if len(seen) > _MAX_SEEN_IDS:
                excess = len(seen) - _MAX_SEEN_IDS
                to_remove = list(seen)[:excess]
                seen -= set(to_remove)
                logger.debug("Trimmed %d stale IDs from seen set", excess)

    finally:
        if tg_client:
            await tg_client.disconnect()
