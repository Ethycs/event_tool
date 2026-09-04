"""ICS mode: fetch an iCalendar feed over HTTP(S) and emit one message per VEVENT.

Contract-compatible with the other ``web_modes`` handlers (accepts
``uc``, ``page``, ``tracer``, ``capture``) even though it doesn't need
a browser -- ``uc`` and ``page`` are unused.  The outer dispatch in
``web_fetch.py`` still spawns a browser for the source; treat that as
~1s of wasted setup per ICS source until we refactor to skip browser
allocation for browser-free modes.

Accepted schemes: ``webcal://``, ``webcals://``, ``http://``, ``https://``.
The ``webcal(s)://`` prefixes are normalized to ``https://`` (RFC 5545
convention -- webcal is just a subscribe-hint for calendar apps).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from icalendar import Calendar

from event_harvester.observability.tracing import get_tracer

logger = logging.getLogger("event_harvester.web_fetch")

# Feeds store timed events in UTC (or a foreign TZID); a 6pm Pacific event is
# 01:00 UTC the *next* day. Converting to the venue's local zone before we take
# the date/time keeps both the day and the hour correct. SF/Oakland sources are
# Pacific; a source may override with a `tz` key.
_DEFAULT_TZ = "America/Los_Angeles"

# Spreadsheet-backed Google Calendars (tamarackoakland) leak their scaffolding
# into the feed: #REF! cells, unfilled "Event Name" placeholder rows, and rows
# whose date never got set surface as the Unix epoch (1970). None are events.
_NOISE_SUMMARIES = frozenset({
    "", "#ref!", "event name", "reserved", "reserved meeting", "tbd",
    "private", "private event", "hold", "closed",
})
_DEFAULT_FORWARD_DAYS = 90


def _do_ics(uc, src: dict, page=None, tracer=None, capture=None) -> list[dict]:
    """ICS mode (OTel-wrapped).  Body in :func:`_do_ics_inner`."""
    name = src.get("name", "ics")
    mode = "ics"
    otel_tracer = get_tracer("event_harvester.web_fetch")
    with otel_tracer.start_as_current_span(f"web.{name}.{mode}") as span:
        span.set_attribute("source.name", name)
        span.set_attribute("source.mode", mode)
        try:
            messages = _do_ics_inner(src)
            span.set_attribute("message.count", len(messages))
            return messages
        except Exception as e:
            span.set_attribute("error.message", str(e))
            span.record_exception(e)
            raise


def _normalize_url(raw: str) -> str:
    """Convert webcal://... to https://... (webcal is not a real transport)."""
    if raw.startswith("webcal://"):
        return "https://" + raw[len("webcal://"):]
    if raw.startswith("webcals://"):
        return "https://" + raw[len("webcals://"):]
    return raw


def _iso(dt) -> str:
    """Normalize an icalendar date/datetime to an ISO 8601 string."""
    if dt is None:
        return ""
    if hasattr(dt, "isoformat"):
        return dt.isoformat()
    return str(dt)


def _localize(dt, tz: ZoneInfo):
    """Return ``dt`` in the venue's local zone.

    A timezone-aware ``datetime`` is converted (UTC 01:00 -> Pacific 18:00 the
    previous day). A naive ``datetime`` is assumed already local. A date-only
    value (all-day event) is returned unchanged -- it has no zone.
    """
    if isinstance(dt, datetime) and dt.tzinfo is not None:
        return dt.astimezone(tz)
    return dt


def _start_date(dt) -> date | None:
    """The calendar date of a VEVENT start (a ``date`` or ``datetime``), or None."""
    if isinstance(dt, datetime):
        return dt.date()
    if isinstance(dt, date):
        return dt
    return None


def _start_time(dt) -> str | None:
    """``HH:MM`` for a timed VEVENT start; None for an all-day (date-only) event."""
    if isinstance(dt, datetime):
        return dt.strftime("%H:%M")
    return None


def _do_ics_inner(src: dict) -> list[dict]:
    url = _normalize_url(src["url"])
    name = src.get("name", "ics")
    timeout = src.get("timeout_s", 20)

    try:
        r = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": "event-harvester/1.0 (+ICS)"},
        )
        r.raise_for_status()
    except Exception as e:
        logger.error("%s: ics fetch failed for %s: %s", name, url, e)
        return []

    try:
        cal = Calendar.from_ical(r.content)
    except Exception as e:
        logger.error("%s: ics parse failed for %s: %s", name, url, e)
        return []

    domain = urlparse(url).netloc
    messages: list[dict] = []

    # Only keep events from today out to the source's forward window. This both
    # honors the source's `forward_days` and, crucially, discards the flood of
    # epoch-dated (1970) placeholder rows that spreadsheet-backed feeds emit.
    today = date.today()
    forward_days = int(src.get("forward_days", _DEFAULT_FORWARD_DAYS))
    horizon = today + timedelta(days=forward_days)
    try:
        tz = ZoneInfo(str(src.get("tz") or _DEFAULT_TZ))
    except Exception:
        tz = ZoneInfo(_DEFAULT_TZ)
    # A per-event `URL:` wins; otherwise fall back to the source's public page so
    # the event carries a real link and can skip the LLM via the row bypass.
    fallback_link = str(src.get("link") or src.get("site") or "").strip()

    dropped_noise = dropped_window = 0
    for component in cal.walk("VEVENT"):
        summary = str(component.get("summary", "")).strip()
        dtstart = component.get("dtstart")
        if not summary or dtstart is None:
            continue
        if summary.strip().lower() in _NOISE_SUMMARIES or "#ref!" in summary.lower():
            dropped_noise += 1
            continue

        start_local = _localize(dtstart.dt, tz)
        start_date = _start_date(start_local)
        if start_date is None or start_date < today or start_date > horizon:
            dropped_window += 1
            continue

        start_iso = _iso(start_local)
        end_iso = ""
        dtend = component.get("dtend")
        if dtend is not None:
            end_iso = _iso(_localize(dtend.dt, tz))

        location = str(component.get("location", "")).strip()
        description = str(component.get("description", "")).strip()
        uid = str(component.get("uid", "")).strip()
        start_time = _start_time(start_local)

        # A per-event `URL:` is authoritative. Otherwise fall back to the
        # source's page, but with a per-event `#fragment` so each event keeps a
        # *distinct* link: dedup keys on the exact link and would otherwise
        # collapse every event that shares the bare venue URL into one. The
        # fragment is stable (derived from the uid) so re-pulls dedup cleanly,
        # and the browser still lands on the venue calendar.
        event_url = str(component.get("url", "")).strip()
        if not event_url and fallback_link:
            token = hashlib.sha1((uid or f"{summary}|{start_iso}").encode()).hexdigest()[:12]
            event_url = f"{fallback_link}#{token}"

        # Emit in the line-delimited calendar-row format the analysis bypass
        # recognizes (`date:`/`time:`/`location:` prefixes). With a real `url`
        # these events are emitted directly and never reach the LLM.
        parts = [summary, f"date: {start_date.isoformat()}"]
        if start_time:
            parts.append(f"time: {start_time}")
        if location:
            parts.append(f"location: {location}")
        if end_iso:
            parts.append(f"End: {end_iso}")
        if description:
            parts.append(description[:2000])
        content = "\n".join(parts)

        if uid:
            msg_id = f"ics:{name}:{uid}"
        else:
            digest = hashlib.sha1(f"{summary}|{start_iso}".encode()).hexdigest()[:16]
            msg_id = f"ics:{name}:{digest}"

        # timestamp = event start when we have a datetime; else "now" so
        # date-only events don't confuse downstream time-based filters.
        ts = start_iso if "T" in start_iso else datetime.now(timezone.utc).isoformat()

        messages.append({
            "platform": "web",
            "id": msg_id,
            "timestamp": ts,
            "author": domain,
            "channel": name,
            "content": content,
            "url": event_url,
        })

    logger.info(
        "%s: ics parsed %d events from %s (dropped %d noise, %d out-of-window)",
        name, len(messages), url, dropped_noise, dropped_window,
    )
    return messages
