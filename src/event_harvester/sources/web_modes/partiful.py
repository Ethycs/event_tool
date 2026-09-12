"""Partiful mode: read the trending events Partiful server-renders per city.

Browser-free, and a sibling of `luma.py` in mechanism — a Next.js page inlines
the props it rendered with, and on `partiful.com/explore` those props are the
event list.

**What probing settled, 2026-08-10:**

* `/discover` 308s to `/explore`.
* `/explore` buckets events by city under `props.pageProps.trendingSections`,
  and the cities are exactly `NYC`, `LA`, `SF`. **The default reading order is
  NYC-first**, so a naive scrape of this page yields New York events for a San
  Francisco calendar — the `ma.to` trap from the blueprint, which is why the
  city is a required, explicit config field here rather than a default.
* Each city carries **5 events**. That is the whole public surface: Partiful is
  invitation-shaped, so unlike Luma there is no discovery API and no
  pagination to find. Five good events is the correct expectation, not a
  symptom of a broken fetch.

The payload is richer than Luma's — a real `description` and a resolved postal
address — so the message carries them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from event_harvester.observability.tracing import get_tracer
from event_harvester.sources.web_utils import parse_next_data

logger = logging.getLogger("event_harvester.web_fetch")
_TRACER = "event_harvester.sources.web_modes.partiful"

UA = "event-harvester/1.0 (+partiful)"
DEFAULT_FORWARD_DAYS = 30
DEFAULT_CITY = "SF"
DESC_LIMIT = 400

# `startDate` is UTC; the per-event `timezone` field is unreliable (it read
# America/New_York for SF and LA events alike, 2026-09-05), so the *configured
# city* is the trustworthy zone. An 8pm-Pacific event is stored 03:00Z the next
# day — slicing the raw string put it at 3am, a day early.
_CITY_TZ = {
    "SF": "America/Los_Angeles",
    "LA": "America/Los_Angeles",
    "NYC": "America/New_York",
}
DEFAULT_TZ = "America/Los_Angeles"


def _local_start(start_raw: str, city: str) -> tuple[str, str]:
    """``(date, time)`` of a UTC ``startDate`` in the configured city's zone."""
    tz_name = _CITY_TZ.get((city or "").upper(), DEFAULT_TZ)
    try:
        dt = datetime.fromisoformat((start_raw or "").replace("Z", "+00:00"))
    except ValueError:
        return start_raw[:10], start_raw[11:16]
    try:
        local = dt.astimezone(ZoneInfo(tz_name))
    except Exception:
        local = dt.astimezone(ZoneInfo(DEFAULT_TZ))
    return local.strftime("%Y-%m-%d"), local.strftime("%H:%M")


def city_sections(html: str) -> dict[str, list[dict]]:
    """`{city: [event, ...]}` from an /explore page's rendered props."""
    data = parse_next_data(html)
    if not data:
        return {}
    try:
        sections = data["props"]["pageProps"]["trendingSections"]
    except (KeyError, TypeError):
        return {}
    if not isinstance(sections, dict):
        return {}

    out: dict[str, list[dict]] = {}
    for city, section in sections.items():
        items = (section or {}).get("items") or []
        events = [i["event"] for i in items if isinstance(i, dict) and i.get("event")]
        if events:
            out[city] = events
    return out


def _venue(event: dict) -> str:
    """Resolve the address block, tolerating Partiful's looser location shapes."""
    info = event.get("locationInfo") or {}
    maps = info.get("mapsInfo") or {}
    name = (maps.get("name") or "").strip()
    lines = [str(x).strip() for x in (maps.get("addressLines") or []) if x]
    if not name and not lines:
        # Unstructured locations carry a free-text string instead.
        return str(info.get("address") or info.get("name") or "").strip()
    parts = [name, *lines]
    seen: list[str] = []
    for p in parts:
        if p and p.lower() not in {s.lower() for s in seen}:
            seen.append(p)
    return ", ".join(seen)


def _message(event: dict, name: str, city: str) -> dict | None:
    title = (event.get("title") or "").strip()
    start = event.get("startDate") or ""
    if not title or not start:
        return None
    if event.get("isPublic") is False:
        return None

    eid = event.get("id") or ""
    link = f"https://partiful.com/e/{eid}" if eid else ""
    venue = _venue(event)
    date_str, time_str = _local_start(start, city)

    parts = [title, f"Date: {date_str}", f"Time: {time_str}"]
    if venue:
        parts.append(f"Venue: {venue}")
    if link:
        parts.append(f"URL: {link}")
    desc = " ".join((event.get("description") or "").split())
    if desc:
        parts.append(desc[:DESC_LIMIT])

    return {
        "platform": "web",
        "id": f"partiful:{eid or title}"[:200],
        "timestamp": start,
        "author": "partiful.com",
        "channel": f"{name}-{city.lower()}",
        "content": "\n".join(parts),
    }


def _do_partiful_inner(uc, src: dict, now: datetime | None = None) -> list[dict]:
    name = src.get("name", "partiful")
    city = str(src.get("city", DEFAULT_CITY))
    timeout = int(src.get("timeout_s", 20))
    forward = int(src.get("forward_days", DEFAULT_FORWARD_DAYS))
    now = now or datetime.now(timezone.utc)

    try:
        r = requests.get(src["url"], timeout=timeout, headers={"User-Agent": UA})
        r.raise_for_status()
    except Exception as e:
        logger.error("%s: fetch failed for %s: %s", name, src["url"], e)
        return []

    sections = city_sections(r.text)
    if not sections:
        logger.error("%s: no trendingSections in the page props", name)
        return []
    if city not in sections:
        # Loud, because silently falling back would harvest another city --
        # exactly the failure this mode's docstring warns about.
        logger.error(
            "%s: no %r section; available: %s", name, city, sorted(sections),
        )
        return []

    horizon = now + timedelta(days=forward)
    messages: list[dict] = []
    out_of_window = 0
    for event in sections[city]:
        msg = _message(event, name, city)
        if msg is None:
            continue
        try:
            start = datetime.fromisoformat(msg["timestamp"].replace("Z", "+00:00"))
        except ValueError:
            start = None
        if start is not None and start > horizon:
            out_of_window += 1
            continue
        messages.append(msg)

    logger.info(
        "%s: partiful %s section had %d event(s) -> %d in a %dd window (%d beyond)",
        name, city, len(sections[city]), len(messages), forward, out_of_window,
    )
    return messages


def _do_partiful(uc, src: dict, tracer=None, capture=None, **_) -> list[dict]:
    with get_tracer(_TRACER).start_as_current_span("web.partiful") as span:
        span.set_attribute("source.name", src.get("name", "partiful"))
        span.set_attribute("source.city", str(src.get("city", DEFAULT_CITY)))
        msgs = _do_partiful_inner(uc, src)
        span.set_attribute("message.count", len(msgs))
        return msgs
