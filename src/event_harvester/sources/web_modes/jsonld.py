"""JSON-LD mode: read the schema.org events a listing page publishes about itself.

Browser-free. Many venue sites embed their whole listing as schema.org
``Event`` items in ``<script type="application/ld+json">``, rendered by the
server, so one plain HTTP GET returns every event with its start, venue and
link.

**What probing settled, 2026-11-24:**

* ``themidwaysf.com/events/`` carries 47 ``MusicEvent`` items in one
  ``@graph``. Its visible listing yields only 2 dated links, which is why
  ``calendar`` mode reported the source deviated.
* ``makerfaire.com/bay-area/`` carries one ``Event``: the faire itself.
* The items include events already over (Midway's reached back four weeks),
  so anything that has ended is dropped here.

The expectation is this handler's own: the page publishes at least one Event
item with a name and a start. A page without one is a deviation, not an empty
listing. A page whose events have all ended is an empty listing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

from event_harvester.observability.tracing import get_tracer
from event_harvester.sources.web_utils import (
    _extract_text,
    is_bot_check,
    make_deviation,
    structured_events,
)

logger = logging.getLogger("event_harvester.web_fetch")
_TRACER = "event_harvester.sources.web_modes.jsonld"

UA = "event-harvester/1.0 (+jsonld)"
DESC_LIMIT = 400
EXPECTED = "schema.org Event items in the page's JSON-LD"
#: Zone assumed for a start or end that carries no offset.
DEFAULT_TZ = "America/Los_Angeles"


def _when(raw: str, *, end_of_day: bool = False) -> datetime | None:
    """An aware datetime for a schema.org date or date-time, or ``None``.

    ``end_of_day``: a bare date used as an end means the whole of that day.
    """
    raw = (raw or "").strip()
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=ZoneInfo(DEFAULT_TZ))
    if end_of_day and "T" not in raw:
        when += timedelta(days=1)
    return when


def _message(event: dict, name: str, domain: str, start: datetime) -> dict:
    parts = [event["title"], f"Date: {event['date']}"]
    if event["time"]:
        parts.append(f"Time: {event['time']}")
    if event["location"]:
        parts.append(f"Venue: {event['location']}")
    if event["link"]:
        parts.append(f"URL: {event['link']}")
    details = " ".join(event["details"].split())
    if details:
        parts.append(details[:DESC_LIMIT])
    return {
        "platform": "web",
        "id": f"jsonld:{event['link'] or event['title'] + event['start']}"[:200],
        "timestamp": start.isoformat(),
        "author": domain,
        "channel": name,
        "content": "\n".join(parts),
    }


def _do_jsonld_inner(src: dict, now: datetime | None = None) -> list[dict]:
    name = src.get("name", "jsonld")
    url = src["url"]
    now = now or datetime.now(timezone.utc)
    forward = src.get("forward_days")
    horizon = now + timedelta(days=int(forward)) if forward else None

    r = requests.get(url, timeout=int(src.get("timeout_s", 20)), headers={"User-Agent": UA})
    if is_bot_check(r.text):
        raise make_deviation(EXPECTED, "cloudflare_challenge", f"HTTP {r.status_code}")
    r.raise_for_status()

    listed = [e for e in structured_events(r.text) if e["title"] and _when(e["start"])]
    if not listed:
        raise make_deviation(
            EXPECTED, "content",
            f"none found; the page reads: {_extract_text(r.text)[:160]!r}",
        )

    messages: list[dict] = []
    ended = beyond = 0
    for event in listed:
        start = _when(event["start"])
        if (_when(event["end"], end_of_day=True) or start + timedelta(hours=6)) < now:
            ended += 1
        elif horizon is not None and start > horizon:
            beyond += 1
        else:
            messages.append(_message(event, name, urlparse(url).netloc, start))

    logger.info(
        "%s: page published %d event(s) -> %d current (%d ended, %d beyond the window)",
        name, len(listed), len(messages), ended, beyond,
    )
    return messages


def _do_jsonld(src: dict, tracer=None, capture=None) -> list[dict]:
    with get_tracer(_TRACER).start_as_current_span("web.jsonld") as span:
        span.set_attribute("source.name", src.get("name", "jsonld"))
        msgs = _do_jsonld_inner(src)
        span.set_attribute("message.count", len(msgs))
        return msgs
