"""Calendar mode: load page, extract date↔link pairs, fetch detail pages.

Handles two flavors:
- Calcium calendars (Perl CGI; static <noscript> links — e.g. erobay)
- Modern JS calendars (DOM-graph date↔link association — e.g. luma)

Owns the Calcium parser since no other mode uses it.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone

from event_harvester.observability.tracing import get_tracer
from event_harvester.sources.web_utils import (
    DEFAULT_SCROLL_SECONDS,
    _extract_event_links,
    _extract_text,
    _fetch_event_details_parallel,
    _filter_known_links,
    _save_link_fingerprint,
    _scroll_for_seconds,
    _wait_cloudflare,
)

logger = logging.getLogger("event_harvester.web_fetch")


# ── Calcium (Perl CGI calendar) parser ───────────────────────────────

_CALCIUM_VENUE_RE = re.compile(r'<table[^>]*class="c_([^"]*)"')
_CALCIUM_TIME_RE = re.compile(r'class="TimeLabel">([^<]+)<')
_CALCIUM_NOSCRIPT_RE = re.compile(r'<noscript><a href="([^"]+)">([^<]+)</a>')
# Events that link straight to an external ticket site (e.g. buytickets.at)
# have no <noscript> fallback -- only the visible anchor. Matches that anchor.
_CALCIUM_EXTLINK_RE = re.compile(r'<a href="(https?://[^"]+)"[^>]*>([^<]+)</a>')
_CALCIUM_DATE_RE = re.compile(r'Date=(\d{4})%2F(\d{1,2})%2F(\d{1,2})')


def _hint_header(lnk: dict) -> str:
    """The date/time/venue the *listing* knew, as lines to prepend to a
    fetched detail page. Empty string when the link carried none.

    **The date is the load-bearing one, and it used to be dropped here.** A
    listing row gives an exact date — Calcium puts it in the URL as
    `Date=2026%2F9%2F5`, and `_parse_calcium_links` skips any block without
    one, so every link has it. The detail page it points at is prose written
    for a human who already knows what day they clicked, so it frequently
    never restates the date. `rerank_messages` then drops the message as
    "no date/link" before extraction ever sees it.

    Measured 2026-08-29 on erobay: **124 links fetched every run, 24 events on
    the page**, and the funnel line `Reranker filter: 2882 -> 731 (dropped 0
    past, 2151 no date/link)` is where the rest went. Nothing logged the loss
    per source, because the drop happens after every source has merged into
    one list.

    Kept in the same `date:` / `time:` / `location:` shape the inline branch
    below already emits, so the extraction prompt sees one format regardless
    of which branch built the message.
    """
    parts = []
    if lnk.get("date_hint"):
        parts.append(f"date: {lnk['date_hint']}")
    if lnk.get("time"):
        parts.append(f"time: {lnk['time']}")
    if lnk.get("venue"):
        parts.append(f"location: {lnk['venue']}")
    return "\n".join(parts) + "\n" if parts else ""


def _parse_calcium_links(html: str) -> list[dict]:
    """Parse a Calcium (Perl CGI) calendar into event link dicts.

    Returns the same format as _extract_event_links():
        [{url, text, date_hint, time, venue}]

    Extracts from <noscript> fallback links (server-generated URLs).
    Pulls time from .TimeLabel and venue from the parent table class.
    """
    from html import unescape

    seen_ids: set[str] = set()
    links: list[dict] = []

    # Split HTML on c_ table boundaries — each block is one event
    blocks = re.split(r'(?=<table[^>]*class="c_)', html)

    # Events are laid out in document order, day cell by day cell. An internal
    # event encodes its day in the link (`Date=YYYY%2FM%2FD`), which doubles as
    # the day marker; carry it forward so an external-ticket event in the same
    # cell (whose own URL has no Date=) inherits the right day.
    current_date: str | None = None

    for block in blocks:
        venue_m = _CALCIUM_VENUE_RE.search(block)
        if not venue_m:
            continue
        venue_raw = venue_m.group(1)

        # Venue: "WickedGrounds" → "Wicked Grounds"
        venue = re.sub(r"([A-Z])", r" \1", venue_raw).strip()

        # Time
        time_m = _CALCIUM_TIME_RE.search(block)
        time_str = time_m.group(1).strip() if time_m else ""

        # Date: from any `Date=` in this block (an internal event's URL, or the
        # day-cell header). Update the carry-forward whenever the block has one;
        # external-ticket events without one inherit the current day.
        date_m = _CALCIUM_DATE_RE.search(block)
        if date_m:
            current_date = (f"{date_m.group(1)}-{int(date_m.group(2)):02d}"
                            f"-{int(date_m.group(3)):02d}")

        # Link + title. Internal events expose a <noscript> fallback (server-
        # generated Calcium URL); external-ticket events expose only the visible
        # anchor. Prefer noscript; fall back to the external link.
        ns_m = _CALCIUM_NOSCRIPT_RE.search(block)
        if ns_m:
            event_url = unescape(ns_m.group(1))
            title = unescape(ns_m.group(2)).strip()
        else:
            ext_m = _CALCIUM_EXTLINK_RE.search(block)
            if not ext_m:
                continue
            event_url = unescape(ext_m.group(1))
            title = unescape(ext_m.group(2)).strip()

        date_str = current_date
        if not date_str:
            continue

        # Extract event ID for dedup (appears twice per event in HTML). External
        # links have no ID= — key them by URL so distinct tickets don't collide.
        id_m = re.search(r"ID=(\d+)", event_url)
        event_id = id_m.group(1) if id_m else event_url
        dedup_key = f"{event_id}:{date_str}"
        if dedup_key in seen_ids:
            continue
        seen_ids.add(dedup_key)

        links.append({
            "url": event_url,
            "text": title,
            "date_hint": date_str,
            "time": time_str,
            "venue": venue,
        })

    logger.info("Calcium parser: %d event links.", len(links))
    return links


def _next_month_url(url: str, today: date | None = None) -> str:
    """The same Calcium grid, one month on.

    Calcium takes the month from `Date=YYYY/M/D`, the same param its own event
    links carry. Absent it, the CGI serves **the current month**, and a month
    grid is mostly the past: probed 2026-08-29, the default grid held 124 links
    spanning `2026-07-26 -> 2026-09-05` of which **8 days were still ahead**.
    The same URL with `Date=2026/9/1` returned `2026-08-30 -> 2026-10-03` — 35
    forward days. So the default view collapses as a month ends, and on the
    28th of any month a week of coverage is all that is left.
    """
    today = today or date.today()
    y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}Date={y}%2F{m}%2F1"


def _calcium_next_month(uc, src: dict, url: str, name: str, tracer, capture) -> list[dict]:
    """Links from next month's grid. Never raises — a second page is a bonus."""
    try:
        page = uc.open(_next_month_url(url), source_name=name,
                       tracer=tracer, capture=capture)
        html = _wait_cloudflare(page, headless=False)
        page.close()
        if "just a moment" in html.lower():
            logger.warning("%s: next-month grid blocked, using this month only", name)
            return []
        links = _parse_calcium_links(html)
        logger.info("%s: +%d link(s) from next month's grid.", name, len(links))
        return links
    except Exception as e:  # noqa: BLE001 -- the current month still stands
        logger.warning("%s: next-month grid failed (%s)", name, e)
        return []


def _calcium_messages(uc, src: dict, event_links: list[dict], domain: str,
                      timeout_ms: int) -> list[dict]:
    """Turn Calcium rows into messages. **The row is the event.**

    A listing row carries a title, an exact date, a time, a venue and a URL.
    That is an event record, and until 2026-08-29 we threw it at an LLM to ask
    two questions the calendar had already answered — *is this an event* and
    *is it in the past* — after spending a browser on 124 detail pages to
    re-learn what the row said. Every failure measured that day lived in that
    round trip:

    * **Dates were invented.** With the row's date dropped, the model read
      "every Wednesday" off a *past* instance's page and published a plausible
      future date. Of 24 events on the site, 11 were dated inside a week whose
      real listing did not contain them.
    * **44 rows produced nothing** (`llm_no_events`) and 77 were binned by
      model judgement as `llm_past`, un-appealable and logged without a date.

    So the row is emitted directly and the past filter is arithmetic on
    `date_hint`. The **URL is the event's page** — a detail fetch adds a
    description, not the event's existence, and is opt-in per source
    (`fetch_details: true`); without it the link stands in for the detail,
    which is what a calendar entry is for.
    """
    from urllib.parse import unquote

    name = src.get("name", "web")
    if not event_links:
        return []

    # Dedupe across the two grids: they overlap by design (a month view spills
    # into its neighbours), so the same event arrives twice.
    seen: set[str] = set()
    merged = []
    for lnk in event_links:
        key = lnk.get("url", "")
        if key and key not in seen:
            seen.add(key)
            merged.append(lnk)

    today = date.today().isoformat()
    upcoming = [x for x in merged if (x.get("date_hint") or "") >= today]
    logger.info("%s: %d link(s), %d upcoming (%d already past).",
                name, len(merged), len(upcoming), len(merged) - len(upcoming))

    upcoming = _filter_known_links(upcoming)
    if not upcoming:
        return []

    details: dict[str, str] = {}
    if src.get("fetch_details"):
        logger.info("%s: fetching %d detail page(s).", name, len(upcoming))
        for lnk, text in _fetch_event_details_parallel(
            uc._context, uc._stealth_plugin, upcoming, timeout_ms,
        ):
            if text:
                details[lnk["url"]] = text

    messages = []
    for lnk in upcoming:
        _save_link_fingerprint(lnk)
        body = details.get(lnk["url"], "")
        # **Title first**, then the labelled fields — the order the inline
        # branch has always used. Emitting `location: Janus` immediately above
        # a bare title line made the extractor read the two as one string and
        # publish "Janus SOJ East Bay Munch", "Munch The Leather Munch",
        # "Transform1060 The Backrooms". Every field below the first line is
        # labelled, so the unlabelled line is unambiguously the title.
        head = (lnk.get("text") or "").strip()
        content = (head + "\n" if head else "") + _hint_header(lnk) + body
        messages.append({
            "platform": "web",
            "id": f"web:{hash(lnk['url'])}",
            # The event's own date, not the scrape's clock. Relative phrases
            # downstream ("tonight") then resolve against the day the event is
            # on, and the message sorts where it belongs.
            "timestamp": f"{lnk['date_hint']}T00:00:00+00:00",
            "author": domain,
            "channel": unquote(lnk["url"])[:120],
            "content": content[:5000],
        })
    logger.info("%s: %d messages.", name, len(messages))
    return messages


# ── Mode handler ─────────────────────────────────────────────────────

def _do_calendar(uc, src: dict, timeout_ms: int, max_events: int, page=None, tracer=None, capture=None) -> list[dict]:
    """Calendar mode (OTel-wrapped). Body lives in ``_do_calendar_inner``."""
    name = src.get("name", "web")
    mode = "calendar"
    otel_tracer = get_tracer("event_harvester.web_fetch")
    with otel_tracer.start_as_current_span(f"web.{name}.{mode}") as span:
        span.set_attribute("source.name", name)
        span.set_attribute("source.mode", mode)
        try:
            messages = _do_calendar_inner(
                uc, src, timeout_ms, max_events,
                page=page, tracer=tracer, capture=capture,
            )
            span.set_attribute("message.count", len(messages))
            return messages
        except Exception as e:
            span.set_attribute("error.message", str(e))
            span.record_exception(e)
            raise


def _do_calendar_inner(uc, src: dict, timeout_ms: int, max_events: int, page=None, tracer=None, capture=None) -> list[dict]:
    """Calendar mode: load page, extract date↔link pairs, fetch detail pages."""
    from urllib.parse import urlparse

    url = src["url"]
    name = src.get("name", "web")
    domain = urlparse(url).netloc
    logger.info("Calendar: %s", src.get("name", url))

    if page is None:
        page = uc.open(url, source_name=name, tracer=tracer, capture=capture)
        html = _wait_cloudflare(page, headless=False)
        if "just a moment" in html.lower():
            page.close()
            return []
    else:
        try:
            page.wait_for_load_state("load", timeout=10000)
        except Exception:
            pass
        html = page.content()

    # Calcium calendar (e.g. erobay). **A row here is already an event** —
    # title, exact date, time and venue, plus a URL to its own page. See
    # `_calcium_messages` for why that ends the pipeline's job rather than
    # starting it.
    if "function PopupWindow" in html:
        event_links = _parse_calcium_links(html)
        page.close()
        event_links += _calcium_next_month(uc, src, url, name, tracer, capture)
        return _calcium_messages(uc, src, event_links, domain, timeout_ms)

    # UC obstacle clearing
    uc.detect_all(page)
    uc.dismiss_cookies(page)
    uc.close_modal(page)

    # Scroll to load lazy content before extracting links
    _scroll_for_seconds(page, src.get("scroll_seconds", DEFAULT_SCROLL_SECONDS))

    # Extract event links via DOM-graph date↔link association
    event_links = _extract_event_links(page)

    # Multi-page: follow pagination to get more events
    max_pages = src.get("max_pages", 0)
    for _ in range(max_pages):
        next_sel = page.evaluate("""() => {
            const candidates = [...document.querySelectorAll('a, button')].filter(el => {
                const text = ((el.textContent || '') + ' '
                    + (el.getAttribute('aria-label') || '') + ' '
                    + (el.getAttribute('title') || '')).toLowerCase();
                return /next|forward|›|»|load more|show more|more events/.test(text)
                    && el.offsetWidth > 0 && el.offsetHeight > 0;
            });
            if (candidates.length === 0) return null;
            const el = candidates[0];
            if (el.id) return '#' + el.id;
            if (el.getAttribute('aria-label'))
                return el.tagName.toLowerCase() + '[aria-label="' + el.getAttribute('aria-label').replace(/"/g, '\\\\"') + '"]';
            return null;
        }""")
        if not next_sel:
            break
        logger.info("%s: following pagination (%s)...", src.get("name"), next_sel[:40])
        try:
            page.click(next_sel)
            page.wait_for_timeout(2000)
            more_links = _extract_event_links(page)
            # Deduplicate by URL
            seen = {lnk["url"] for lnk in event_links}
            for lnk in more_links:
                if lnk["url"] not in seen:
                    event_links.append(lnk)
                    seen.add(lnk["url"])
        except Exception:
            break

    if len(event_links) >= 3:
        # Filter out already-known events before expensive detail fetches
        event_links = _filter_known_links(event_links)
        logger.info("%s: %d event links to process.", src.get("name"), len(event_links))
        messages = []
        # Split inline vs fetchable
        inline_links = [l for l in event_links if l.get("inline")]
        fetch_links = [l for l in event_links if not l.get("inline")]

        # Inline events (already have all data)
        for lnk in inline_links:
            date_hint = lnk.get("date_hint", "")
            time_hint = lnk.get("time", "")
            venue = lnk.get("venue", "")
            parts = [lnk["text"]]
            if date_hint:
                parts.append(f"date: {date_hint}")
            if time_hint:
                parts.append(f"time: {time_hint}")
            if venue:
                parts.append(f"location: {venue}")
            messages.append({
                "platform": "web",
                "id": f"web:{hash(lnk.get('url', '') + date_hint)}",
                "timestamp": f"{date_hint}T00:00:00+00:00" if date_hint else datetime.now(timezone.utc).isoformat(),
                "author": domain,
                "channel": url[:120],
                "content": "\n".join(parts),
            })

        # Fetch detail pages in parallel (5 tabs at a time)
        if fetch_links:
            results = _fetch_event_details_parallel(
                uc._context, uc._stealth_plugin, fetch_links, timeout_ms,
            )
            for lnk, text in results:
                if text:
                    _save_link_fingerprint(lnk)
                    # Same reason as the Calcium branch: `_extract_event_links`
                    # associates a date with each link in the DOM, and dropping
                    # it here costs the message its only date signal whenever
                    # the detail page does not restate one.
                    messages.append({
                        "platform": "web",
                        "id": f"web:{hash(lnk['url'])}",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "author": domain,
                        "channel": lnk["url"][:120],
                        "content": (_hint_header(lnk) + text)[:5000],
                    })
        page.close()
        return messages

    # Fallback: raw text
    text = _extract_text(html)
    page.close()
    if len(text) < 50:
        return []
    return [{
        "platform": "web",
        "id": f"web:{hash(url)}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "author": domain,
        "channel": url[:120],
        "content": text[:8000],
    }]
