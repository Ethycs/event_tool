"""Message harvesting from sources with auto-caching."""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from event_harvester.config import AppConfig
from event_harvester.display import DIM, RESET
from event_harvester.sources import (
    fetch_gmail_messages,
    fetch_web_sources,
    read_discord_messages,
    read_signal_messages,
    read_telegram_messages,
)

logger = logging.getLogger("event_harvester")

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
CACHE_FILE = _DATA_DIR / ".message_cache.json"
CACHE_MAX_AGE = timedelta(hours=4)


_WEB_EVENT_DOMAINS = (
    r"eventbrite|lu\.ma|luma|meetup\.com|partiful"
    r"|facebook\.com/events|calendar\.google"
)


async def harvest_messages(
    cfg: AppConfig,
    *,
    load_path: str | None = None,
    no_discord: bool = False,
    no_telegram: bool = False,
    no_gmail: bool = False,
    no_signal: bool = False,
    no_web: bool = False,
    skip_cache: bool = False,
    web_source: str | None = None,
    no_cooldown: bool = False,
) -> list[dict]:
    """Fetch messages from sources, with auto-caching.

    Returns the list of message dicts, or an empty list on failure.
    """
    active_sources = sorted(
        s for s, skip in [
            ("discord", no_discord),
            ("telegram", no_telegram),
            ("gmail", no_gmail),
            ("signal", no_signal),
            ("web", no_web),
        ] if not skip
    )

    # ── Load from file ────────────────────────────────────────────────────
    if load_path:
        try:
            messages = json.loads(Path(load_path).read_text())
            logger.info("Loaded %d messages from %s", len(messages), load_path)
            return messages
        except Exception as e:
            logger.error("Failed to load %s: %s", load_path, e)
            return []

    # ── Try cache ─────────────────────────────────────────────────────────
    if not skip_cache and CACHE_FILE.exists():
        try:
            cache_data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            cached_at = datetime.fromisoformat(cache_data["cached_at"])
            cached_sources = sorted(cache_data.get("sources", []))
            if (
                datetime.now(timezone.utc) - cached_at < CACHE_MAX_AGE
                and cached_sources == active_sources
            ):
                messages = cache_data["messages"]
                age_min = int((datetime.now(timezone.utc) - cached_at).total_seconds() / 60)
                print(
                    f"{DIM}Using cached messages ({len(messages)} msgs, {age_min}m old). "
                    f"Delete data/.message_cache.json to force refresh.{RESET}\n"
                )
                return messages
            else:
                logger.debug("Message cache stale or sources changed, re-fetching.")
        except Exception:
            logger.debug("Message cache invalid, re-fetching.")

    # ── Harvest from sources ──────────────────────────────────────────────
    cutoff = datetime.now(timezone.utc) - timedelta(days=cfg.days_back)
    messages: list[dict] = []

    if not no_discord:
        print("[ Discord ]")
        messages.extend(
            read_discord_messages(cutoff, override_path=cfg.discord.cache_path)
        )
        print()

    if not no_telegram:
        print("[ Telegram ]")
        messages.extend(
            await read_telegram_messages(
                cutoff,
                cfg.telegram,
                channels_allowlist=cfg.telegram_channels,
                channels_blocklist=cfg.telegram_exclude,
            )
        )
        print()

    if not no_gmail:
        print("[ Gmail ]")
        messages.extend(fetch_gmail_messages(cfg.gmail, cutoff))
        print()

    if not no_signal:
        print("[ Signal ]")
        messages.extend(read_signal_messages(cutoff, override_path=cfg.signal.db_path))
        print()

    if not no_web:
        print("[ Web Sources ]")
        web_kw: dict = {
            "max_events": cfg.web.max_events,
            "timeout_ms": cfg.web.timeout_ms,
            "no_cooldown": no_cooldown,
        }
        if web_source:
            from event_harvester.sources.web_fetch import _load_web_sources
            all_web = _load_web_sources()
            matched = [s for s in all_web if s.get("name", "").lower() == web_source.lower()]
            if not matched:
                names = ", ".join(s.get("name", s["url"]) for s in all_web)
                logger.warning("Web source '%s' not found. Available: %s", web_source, names)
            else:
                web_kw["sources"] = matched
        web_msgs = fetch_web_sources(**web_kw)
        messages.extend(web_msgs)
        print()

    # ── Auto-save to cache (decimate web content to reduce cache size) ──
    if messages:
        try:
            from event_harvester.sources.web_fetch import _decimate_text

            cache_msgs = []
            for m in messages:
                if m.get("platform") == "web" and len(m.get("content", "")) > 2000:
                    m = {**m, "content": _decimate_text(m["content"])}
                cache_msgs.append(m)

            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(
                json.dumps({
                    "cached_at": datetime.now(timezone.utc).isoformat(),
                    "sources": active_sources,
                    "messages": cache_msgs,
                }, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.debug("Failed to write message cache: %s", e)

    return messages


def save_messages(messages: list[dict], path: str) -> None:
    """Save messages to JSON file."""
    Path(path).write_text(
        json.dumps(messages, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Raw messages saved -> {path}\n")
