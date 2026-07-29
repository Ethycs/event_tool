"""Web mode handlers (one file per mode).

Each mode is invoked by ``web_fetch.fetch_web_sources`` based on the
``mode`` field in the source dict (or auto-detected via
``web_utils._detect_mode``). The five OTel-wrapped entry points
re-exported here are the canonical handles.
"""

from event_harvester.sources.web_modes.calendar import _do_calendar
from event_harvester.sources.web_modes.chat import _do_chat
from event_harvester.sources.web_modes.feed import _do_feed
from event_harvester.sources.web_modes.ics import _do_ics
from event_harvester.sources.web_modes.raw import _do_raw
from event_harvester.sources.web_modes.search import _do_search

__all__ = ["_do_calendar", "_do_chat", "_do_feed", "_do_ics", "_do_raw", "_do_search"]
