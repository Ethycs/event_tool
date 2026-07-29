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
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from icalendar import Calendar

from event_harvester.observability.tracing import get_tracer

logger = logging.getLogger("event_harvester.web_fetch")


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

    for component in cal.walk("VEVENT"):
        summary = str(component.get("summary", "")).strip()
        dtstart = component.get("dtstart")
        if not summary or dtstart is None:
            continue

        start_iso = _iso(dtstart.dt)
        end_iso = ""
        dtend = component.get("dtend")
        if dtend is not None:
            end_iso = _iso(dtend.dt)

        location = str(component.get("location", "")).strip()
        description = str(component.get("description", "")).strip()
        event_url = str(component.get("url", "")).strip()
        uid = str(component.get("uid", "")).strip()

        # Pack event fields into content for the downstream LLM extractor.
        # Format is stable + line-delimited so extraction prompts can rely on it.
        parts = [summary, f"Start: {start_iso}"]
        if end_iso:
            parts.append(f"End: {end_iso}")
        if location:
            parts.append(f"Location: {location}")
        if event_url:
            parts.append(f"URL: {event_url}")
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
        })

    logger.info("%s: ics parsed %d events from %s", name, len(messages), url)
    return messages
