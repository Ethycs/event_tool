from event_harvester.sources.chromium_cache import (
    find_browser_cache,
    find_chrome_cache,
    find_edge_cache,
    read_cache_entries,
    read_cache_messages,
    read_cached_pages,
)
from event_harvester.sources.discord import find_discord_cache, read_discord_messages
from event_harvester.sources.gmail import (
    fetch_full_bodies,
    fetch_messages as fetch_gmail_messages,
    filter_read_sent,
    reply as gmail_reply,
    trash as gmail_trash,
)
from event_harvester.sources.signal import read_signal_messages
from event_harvester.sources.telegram import (
    create_persistent_client,
    read_telegram_messages,
)
from event_harvester.sources.web_fetch import fetch_event_pages

__all__ = [
    # Generic Chromium cache reader
    "find_browser_cache",
    "find_chrome_cache",
    "find_edge_cache",
    "read_cache_entries",
    "read_cache_messages",
    "read_cached_pages",
    # Platform-specific
    "find_discord_cache",
    "read_discord_messages",
    "fetch_gmail_messages",
    "fetch_full_bodies",
    "filter_read_sent",
    "gmail_reply",
    "gmail_trash",
    "read_signal_messages",
    "create_persistent_client",
    "read_telegram_messages",
    "fetch_event_pages",
]
