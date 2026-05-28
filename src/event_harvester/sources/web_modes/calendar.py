"""Calendar mode: load page, extract date↔link pairs, fetch detail pages.

Handles two flavors:
- Calcium calendars (Perl CGI; static <noscript> links — e.g. erobay)
- Modern JS calendars (DOM-graph date↔link association — e.g. luma)

Owns the Calcium parser since no other mode uses it.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

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
from event_harvester.observability.tracing import get_tracer

logger = logging.getLogger("event_harvester.web_fetch")


# ── Calcium (Perl CGI calendar) parser ───────────────────────────────

_CALCIUM_VENUE_RE = re.compile(r'<table[^>]*class="c_([^"]*)"')
_CALCIUM_TIME_RE = re.compile(r'class="TimeLabel">([^<]+)<')
_CALCIUM_NOSCRIPT_RE = re.compile(r'<noscript><a href="([^"]+)">([^<]+)</a>')
_CALCIUM_DATE_RE = re.compile(r'Date=(\d{4})%2F(\d{1,2})%2F(\d{1,2})')


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

    for block in blocks:
        venue_m = _CALCIUM_VENUE_RE.match(block)
        if not venue_m:
            continue
        venue_raw = venue_m.group(1)

        # Venue: "WickedGrounds" → "Wicked Grounds"
        venue = re.sub(r"([A-Z])", r" \1", venue_raw).strip()

        # Time
        time_m = _CALCIUM_TIME_RE.search(block)
        time_str = time_m.group(1).strip() if time_m else ""

        # Noscript link (server-generated URL + title)
        ns_m = _CALCIUM_NOSCRIPT_RE.search(block)
        if not ns_m:
            continue
        event_url = unescape(ns_m.group(1))
        title = unescape(ns_m.group(2)).strip()

        # Extract date from URL params
        date_m = _CALCIUM_DATE_RE.search(event_url)
        if not date_m:
            continue
        date_str = f"{date_m.group(1)}-{int(date_m.group(2)):02d}-{int(date_m.group(3)):02d}"

        # Extract event ID for dedup (appears twice per event in HTML)
        id_m = re.search(r"ID=(\d+)", event_url)
        event_id = id_m.group(1) if id_m else title
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

    # Calcium calendar (e.g. erobay) — parse links from static HTML,
    # then fetch detail pages through the normal pipeline below.
    if "function PopupWindow" in html:
        event_links = _parse_calcium_links(html)
        page.close()
        if not event_links:
            return []
        # Filter already-known events, then fetch detail pages
        event_links = _filter_known_links(event_links)
        logger.info("%s: %d calcium links to fetch.", src.get("name"), len(event_links))
        if not event_links:
            return []
        results = _fetch_event_details_parallel(
            uc._context, uc._stealth_plugin, event_links, timeout_ms,
        )
        from urllib.parse import unquote

        messages = []
        for lnk, text in results:
            if text:
                _save_link_fingerprint(lnk)
                # Prepend inline metadata the detail page may not have
                header_parts = []
                if lnk.get("time"):
                    header_parts.append(f"time: {lnk['time']}")
                if lnk.get("venue"):
                    header_parts.append(f"location: {lnk['venue']}")
                if header_parts:
                    text = "\n".join(header_parts) + "\n" + text
                messages.append({
                    "platform": "web",
                    "id": f"web:{hash(lnk['url'])}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "author": domain,
                    "channel": unquote(lnk["url"])[:120],
                    "content": text[:5000],
                })
        return messages

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
                    messages.append({
                        "platform": "web",
                        "id": f"web:{hash(lnk['url'])}",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "author": domain,
                        "channel": lnk["url"][:120],
                        "content": text[:5000],
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
