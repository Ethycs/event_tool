"""Luma mode: ask Luma's own discovery API, one message per event.

Read over plain HTTP, like `ics.py` and `rss.py`: no browser is involved, and
`web_fetch.HTTP_MODES` routes it outside every browser group.

**Why this exists.** Luma was already configured, as `mode: "calendar"` against
`lu.ma/discover`, and measured 2026-08-10 it returned **1 message and 1 event**
— from a page whose own directory reads "San Francisco 80 Events". That is the
`raw`-on-a-dense-listing failure the `rss` mode was written for, repeating on
the largest event platform in the city: one blob in, one event out.

Three things were found by probing rather than reasoning, and each changed the
design:

1. **`lu.ma` 301s to `luma.com`.** The configured URL still worked, at the cost
   of a redirect on every fetch.
2. **`/discover` server-renders no events at all** — its `__NEXT_DATA__` has an
   empty `data`. The city pages (`/sf`) render 20. The URL you are given is not
   the page with the events, again.
3. **There is a public JSON API** behind the city page —
   `api.lu.ma/discover/get-paginated-events` — needing no key and returning
   `entries` / `has_more` / `next_cursor`. It answered with **45 SF events** to
   a single unauthenticated GET.

So this mode uses the API and paginates it, rather than parsing the 400 KB
page. The page is still fetched once when `place_api_id` is not configured,
because a hardcoded id is a thing that silently rots.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

from event_harvester.observability.tracing import get_tracer
from event_harvester.sources.web_utils import parse_next_data

logger = logging.getLogger("event_harvester.web_fetch")
_TRACER = "event_harvester.sources.web_modes.luma"

API = "https://api.lu.ma/discover/get-paginated-events"
UA = "event-harvester/1.0 (+luma)"
# Luma's API returns `start_at` in UTC and the event's own `timezone` separately;
# an 8pm-Pacific event is stored 03:00Z the *next* day. Falls back to Pacific
# when an event omits its zone, matching the SF/Bay-Area focus of this tool.
DEFAULT_TZ = "America/Los_Angeles"

DEFAULT_FORWARD_DAYS = 21
DEFAULT_MAX_EVENTS = 60
# Luma serves ~45 per call regardless of a higher ask; more pages, not bigger.
PAGE_SIZE = 50
MAX_PAGES = 6


def resolve_place_id(city_url: str, timeout: int = 20) -> str | None:
    """Read the discovery place id off a Luma city page.

    Configuring the id directly is supported and skips this, but the id is an
    opaque token (`discplace-BDj7GNbGlsF7Cka`) that no operator can verify by
    eye, so deriving it from a human-readable URL is the default.
    """
    try:
        r = requests.get(city_url, timeout=timeout, headers={"User-Agent": UA})
        r.raise_for_status()
    except Exception as e:
        logger.error("luma: could not fetch %s: %s", city_url, e)
        return None

    data = parse_next_data(r.text)
    if not data:
        logger.error("luma: no __NEXT_DATA__ at %s", city_url)
        return None
    try:
        place = data["props"]["pageProps"]["initialData"]["data"]["place"]
    except (KeyError, TypeError):
        logger.error("luma: %s has no place block — is it a city page?", city_url)
        return None
    return place.get("api_id")


def fetch_entries(
    place_api_id: str, max_events: int, timeout: int = 20,
    category_api_id: str | None = None,
) -> list[dict]:
    """Page through the discovery API until satisfied or out of pages.

    ``category_api_id`` narrows to one of Luma's own categories. That is the
    strongest keyword support Luma offers publicly — its free-text search
    endpoint answers *"You are not signed in"* — and it was confirmed real
    rather than assumed: an unknown parameter leaves the result set identical
    (28/28 overlap), while `discover_category_api_id=cat-ai` changes it to
    10/28 and returns visibly AI-themed events.
    """
    out: list[dict] = []
    cursor: str | None = None

    for _ in range(MAX_PAGES):
        params: dict[str, Any] = {
            "place_api_id": place_api_id,
            "pagination_limit": PAGE_SIZE,
        }
        if category_api_id:
            params["discover_category_api_id"] = category_api_id
        if cursor:
            params["pagination_cursor"] = cursor
        try:
            r = requests.get(
                API, params=params, timeout=timeout,
                headers={"User-Agent": UA, "Accept": "application/json"},
            )
            r.raise_for_status()
            payload = r.json()
        except Exception as e:
            logger.error("luma: discovery API failed: %s", e)
            break

        entries = payload.get("entries") or []
        out.extend(entries)
        cursor = payload.get("next_cursor")
        if not entries or not payload.get("has_more") or not cursor:
            break
        if len(out) >= max_events:
            break

    return out[:max_events]


def _local_start(start_raw: str, tz_name: str) -> tuple[str, str]:
    """``(date, time)`` of a Luma UTC ``start_at``, in the event's local zone.

    ``start_at`` is UTC (``…Z``); the event carries its own ``timezone``. Taking
    the raw string's date/time slice put an 8pm-Pacific event at 03:00 on the
    *next* day — the "shows at 3am, a day early" bug. Convert first.
    """
    try:
        dt = datetime.fromisoformat((start_raw or "").replace("Z", "+00:00"))
    except ValueError:
        return start_raw[:10], start_raw[11:16]     # unparseable: last-resort slice
    try:
        local = dt.astimezone(ZoneInfo(tz_name or DEFAULT_TZ))
    except Exception:
        local = dt.astimezone(ZoneInfo(DEFAULT_TZ))
    return local.strftime("%Y-%m-%d"), local.strftime("%H:%M")


def _venue(event: dict) -> str:
    """Best available place string, without inventing precision.

    `geo_address_info` is `mode: "obfuscated"` for events that hide their
    address until you RSVP; those carry a city but no street, and saying so is
    better than implying a venue we do not have.
    """
    geo = event.get("geo_address_info") or {}
    if event.get("location_type") == "virtual" or (
        event.get("virtual_info") and not geo
    ):
        return "Online"
    parts = [geo.get("address"), geo.get("city_state") or geo.get("city")]
    seen: list[str] = []
    for p in parts:
        p = (p or "").strip()
        if p and p.lower() not in {s.lower() for s in seen}:
            seen.append(p)
    return ", ".join(seen)


def _message(entry: dict, name: str) -> dict | None:
    event = entry.get("event") or {}
    title = (event.get("name") or "").strip()
    start = event.get("start_at") or ""
    if not title or not start:
        return None

    slug = event.get("url") or ""
    link = f"https://luma.com/{slug}" if slug else ""
    venue = _venue(event)
    date_str, time_str = _local_start(start, event.get("timezone") or DEFAULT_TZ)

    # Facts first: `analysis.build_prompt` keeps 400 chars per message, and
    # these are exact where any prose would be inferred. Date/Time are the
    # event's local wall-clock, converted from the UTC `start_at`.
    parts = [title, f"Date: {date_str}", f"Time: {time_str}"]
    if venue:
        parts.append(f"Venue: {venue}")
    if link:
        parts.append(f"URL: {link}")

    return {
        "platform": "web",
        "id": f"luma:{event.get('api_id') or slug or title}"[:200],
        "timestamp": start,
        "author": "luma.com",
        "channel": name,
        "content": "\n".join(parts),
    }


def _do_luma_inner(src: dict, now: datetime | None = None) -> list[dict]:
    name = src.get("name", "luma")
    timeout = int(src.get("timeout_s", 20))
    forward = int(src.get("forward_days", DEFAULT_FORWARD_DAYS))
    max_events = int(src.get("max_events", DEFAULT_MAX_EVENTS))
    now = now or datetime.now(timezone.utc)

    place_id = src.get("place_api_id") or resolve_place_id(src["url"], timeout)
    if not place_id:
        logger.error("%s: no place id; skipping", name)
        return []

    # Keywords that name a Luma category narrow the fetch itself; the rest can
    # only be matched after the fact, which `fetch_web_sources` does for every
    # mode. One category per request, so several keywords mean several passes.
    categories: list[str] = []
    if src.get("keywords"):
        from event_harvester.keywords import LUMA_CATEGORIES, terms_for

        for t in terms_for(src["keywords"]):
            cat = LUMA_CATEGORIES.get(t.strip().lower())
            if cat and cat not in categories:
                categories.append(cat)

    if categories:
        entries, seen_ids = [], set()
        per_cat = max(1, max_events // len(categories))
        for cat in categories:
            for entry in fetch_entries(place_id, per_cat, timeout, category_api_id=cat):
                eid = (entry.get("event") or {}).get("api_id")
                if eid and eid in seen_ids:
                    continue
                if eid:
                    seen_ids.add(eid)
                entries.append(entry)
        logger.info(
            "%s: %d categor(ies) %s -> %d unique entr(ies)",
            name, len(categories), categories, len(entries),
        )
    else:
        entries = fetch_entries(place_id, max_events, timeout)

    horizon = now + timedelta(days=forward)

    messages: list[dict] = []
    out_of_window = 0
    for entry in entries:
        msg = _message(entry, name)
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
        "%s: luma api returned %d entr(ies) -> %d in a %dd window (%d beyond)",
        name, len(entries), len(messages), forward, out_of_window,
    )
    return messages


def _do_luma(src: dict, tracer=None, capture=None) -> list[dict]:
    with get_tracer(_TRACER).start_as_current_span("web.luma") as span:
        span.set_attribute("source.name", src.get("name", "luma"))
        msgs = _do_luma_inner(src)
        span.set_attribute("message.count", len(msgs))
        return msgs
