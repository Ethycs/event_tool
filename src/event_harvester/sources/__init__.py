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
    apply_label,
    clean_body,
    ensure_label,
    event_links,
    expand_event_links,
    fetch_full_bodies,
    filter_read_sent,
    format_event_facts,
    hydrate_bodies,
    label_event_messages,
)
from event_harvester.sources.gmail import (
    fetch_messages as fetch_gmail_messages,
)
from event_harvester.sources.gmail import (
    reply as gmail_reply,
)
from event_harvester.sources.gmail import (
    trash as gmail_trash,
)
from event_harvester.sources.signal import read_signal_messages
from event_harvester.sources.telegram import (
    create_persistent_client,
    read_telegram_messages,
)
from event_harvester.sources.uc_browser import UCBrowser
from event_harvester.sources.web_fetch import fetch_web_sources
from event_harvester.sources.web_session import web_login

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
    "clean_body",
    "event_links",
    "hydrate_bodies",
    "apply_label",
    "ensure_label",
    "label_event_messages",
    "expand_event_links",
    "format_event_facts",
    "gmail_reply",
    "gmail_trash",
    "read_signal_messages",
    "create_persistent_client",
    "read_telegram_messages",
    "fetch_web_sources",
    "web_login",
    "UCBrowser",
]
