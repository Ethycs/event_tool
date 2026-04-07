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
WATERMARK_FILE = _DATA_DIR / ".watermarks.json"
CACHE_MAX_AGE = timedelta(hours=4)


def _load_watermarks() -> dict[str, dict]:
    """Load per-channel watermarks.

    Format: {
        "platform:channel": {
            "last_ts": "ISO timestamp of newest seen message",
            "seen_ids": ["id1", "id2", ...]  (last N message IDs)
        }
    }
    """
    from event_harvester.utils import load_json
    return load_json(WATERMARK_FILE)


def _save_watermarks(watermarks: dict[str, dict]) -> None:
    """Save per-channel watermarks."""
    from event_harvester.utils import save_json
    save_json(WATERMARK_FILE, watermarks)


_MAX_IDS_PER_CHANNEL = 500  # Keep last N IDs to avoid unbounded growth


def _update_watermarks(messages: list[dict], watermarks: dict[str, dict]) -> None:
    """Update watermarks with IDs and timestamps from fetched messages."""
    for m in messages:
        key = f"{m['platform']}:{m.get('channel', '?')}"
        wm = watermarks.setdefault(key, {"last_ts": "", "seen_ids": []})

        ts = m.get("timestamp", "")
        if ts > wm["last_ts"]:
            wm["last_ts"] = ts

        mid = m["id"]
        if mid not in wm["seen_ids"]:
            wm["seen_ids"].append(mid)

    # Trim to last N IDs per channel
    for wm in watermarks.values():
        if len(wm["seen_ids"]) > _MAX_IDS_PER_CHANNEL:
            wm["seen_ids"] = wm["seen_ids"][-_MAX_IDS_PER_CHANNEL:]


def filter_seen(messages: list[dict], watermarks: dict[str, dict]) -> list[dict]:
    """Filter out messages already seen in a previous run.

    A message is "seen" if its ID is in the watermark's seen_ids set
    OR its timestamp is <= the watermark timestamp (redundancy check).
    """
    if not watermarks:
        return messages

    # Pre-convert seen_ids lists to sets for O(1) lookup
    seen_sets: dict[str, set] = {}
    for wm_key, wm in watermarks.items():
        seen_sets[wm_key] = set(wm.get("seen_ids", []))

    new = []
    n_skipped = 0
    for m in messages:
        key = f"{m['platform']}:{m.get('channel', '?')}"
        wm = watermarks.get(key)
        if not wm:
            new.append(m)
            continue

        mid = m["id"]
        ts = m.get("timestamp", "")

        # Skip if ID already seen OR timestamp is at/before the watermark
        if mid in seen_sets.get(key, set()) or ts <= wm.get("last_ts", ""):
            n_skipped += 1
            continue

        new.append(m)

    if n_skipped:
        logger.info("Watermarks: skipped %d already-seen messages, %d new.", n_skipped, len(new))

    return new


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
        web_msgs = fetch_web_sources(
            max_events=cfg.web.max_events,
            timeout_ms=cfg.web.timeout_ms,
        )
        messages.extend(web_msgs)
        print()

    # ── Filter already-seen messages ──────────────────────────────────────
    watermarks = _load_watermarks()
    new_messages = filter_seen(messages, watermarks)
    if len(messages) != len(new_messages):
        print(
            f"{DIM}Watermarks: {len(messages) - len(new_messages)} already-seen skipped, "
            f"{len(new_messages)} new.{RESET}\n"
        )

    # ── Update watermarks with all fetched messages ────────────────────
    _update_watermarks(messages, watermarks)
    _save_watermarks(watermarks)

    # ── Auto-save to cache (decimate web content to reduce cache size) ──
    if new_messages:
        try:
            from event_harvester.sources.web_fetch import _decimate_text

            cache_msgs = []
            for m in new_messages:
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

    return new_messages


def save_messages(messages: list[dict], path: str) -> None:
    """Save messages to JSON file."""
    Path(path).write_text(
        json.dumps(messages, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Raw messages saved -> {path}\n")
