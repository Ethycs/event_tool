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
from html import unescape

from event_harvester.observability.tracing import get_tracer
from event_harvester.sources import web_checkpoint
from event_harvester.sources.web_utils import (
    DEFAULT_SCROLL_SECONDS,
    _extract_event_links,
    _fetch_event_details_parallel,
    _filter_known_links,
    _save_link_fingerprint,
    _scroll_for_seconds,
    _wait_cloudflare,
    is_bot_check,
    page_deviation,
)

logger = logging.getLogger("event_harvester.web_fetch")


# ── Calcium (Perl CGI calendar) parser ───────────────────────────────

_CALCIUM_VENUE_RE = re.compile(r'<table[^>]*class="c_([^"]*)"')
_CALCIUM_TIME_RE = re.compile(r'class="TimeLabel">([^<]+)<')
# An anchor's title may carry inline markup -- `Tangle! <br>Mission Control
# Weekday<br>Peer Rope Jam`, `<strong>AURA</strong><br/>...` -- so it is any run
# of text and tags short of a nested anchor. As `[^<]+` those rows had no title
# and were dropped: 4 of erobay's upcoming rows on 2026-11-02.
_CALCIUM_TITLE = r'((?:[^<]|<(?!/?a\b)[^>]*>)+)'
_CALCIUM_NOSCRIPT_RE = re.compile(r'<noscript><a href="([^"]+)">' + _CALCIUM_TITLE + '</a>')
# Events that link straight to an external ticket site (e.g. buytickets.at)
# have no <noscript> fallback -- only the visible anchor. Matches that anchor.
_CALCIUM_EXTLINK_RE = re.compile(r'<a href="(https?://[^"]+)"[^>]*>' + _CALCIUM_TITLE + '</a>')
_CALCIUM_DATE_RE = re.compile(r'Date=(\d{4})%2F(\d{1,2})%2F(\d{1,2})')


def _anchor_text(raw: str) -> str:
    """An anchor's visible text: tags become spaces (a `<br>` separates words),
    entities are decoded, whitespace collapses."""
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", raw)).split())


def _first_link(regex: re.Pattern, block: str) -> tuple[str, str] | None:
    """``(url, title)`` of the first anchor `regex` finds whose title has text."""
    for m in regex.finditer(block):
        title = _anchor_text(m.group(2))
        if title:
            return unescape(m.group(1)), title
    return None


def _calcium_row_key(url: str, day: str) -> str:
    """One occurrence: its Calcium event ID on one day, or for an external
    ticket link (which has no ID) the URL on one day.

    Neither half is enough alone. The same ID and day arrive from both month
    grids under different URLs, because the query carries the grid it came
    from (`DoneURL=`). And one ticket URL can serve every date of a series —
    Vox Body sells each monthly rope jam from one buytickets.at page — so a
    URL-only key kept the first date and dropped the rest: 8 of erobay's
    upcoming rows on 2026-11-02.
    """
    id_m = re.search(r"ID=(\d+)", url)
    return f"{id_m.group(1) if id_m else url}:{day}"


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
        found = (_first_link(_CALCIUM_NOSCRIPT_RE, block)
                 or _first_link(_CALCIUM_EXTLINK_RE, block))
        if not found:
            continue
        event_url, title = found

        date_str = current_date
        if not date_str:
            continue

        # The same occurrence can appear twice in one grid.
        dedup_key = _calcium_row_key(event_url, date_str)
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


#: How many months of a Calcium grid to walk, counting the current one. The
#: default view is one month and mostly the past; one next-month grid (the old
#: behavior) still dropped everything from the third month on. Measured on
#: erobay 2026-11-05: current + next = 198 upcoming, but 314+ unique events span
#: the next six months (and more beyond) — over a third missed, worse later in
#: the month as the current grid collapses. The forward months share the first
#: month's Cloudflare clearance, so walking several is cheap. Per-source
#: override: ``calendar_months``.
DEFAULT_CALENDAR_MONTHS = 6


def _month_url(url: str, offset: int, today: date | None = None) -> str:
    """The same Calcium grid, ``offset`` months on (0 = the given month's URL).

    Calcium takes the month from `Date=YYYY/M/D`, the same param its own event
    links carry. Absent it, the CGI serves **the current month**, and a month
    grid is mostly the past: probed 2026-08-29, the default grid held 124 links
    spanning `2026-07-26 -> 2026-09-05` of which **8 days were still ahead**.
    The same URL with `Date=2026/9/1` returned `2026-08-30 -> 2026-10-03` — 35
    forward days. So the default view collapses as a month ends, and on the
    28th of any month a week of coverage is all that is left — which is why the
    fetch walks several months forward rather than trusting one grid.
    """
    today = today or date.today()
    base = today.month - 1 + offset
    y, m = today.year + base // 12, base % 12 + 1
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}Date={y}%2F{m}%2F1"


def _next_month_url(url: str, today: date | None = None) -> str:
    """The grid one month on. Thin wrapper over :func:`_month_url`."""
    return _month_url(url, 1, today)


def _calcium_forward_months(uc, src: dict, url: str, name: str, tracer, capture) -> list[dict]:
    """Links from the next several months' grids. Never raises — each extra
    month is a bonus, and the current month already stands on its own.

    Walks ``calendar_months - 1`` months forward (default
    :data:`DEFAULT_CALENDAR_MONTHS`). Each grid is checkpointed under its own
    month URL. A Cloudflare block stops the walk (the session is challenged, so
    later months would block too); any other per-month error is skipped.
    """
    months = max(1, int(src.get("calendar_months", DEFAULT_CALENDAR_MONTHS)))
    links: list[dict] = []
    for offset in range(1, months):
        try:
            m_url = _month_url(url, offset)
            # Replay from an MHTML checkpoint when one exists (EH_WEB_CHECKPOINT),
            # else fetch live and snapshot it. Keyed per month URL.
            replayed = web_checkpoint.replay(uc, m_url, source_name=name,
                                             tracer=tracer, capture=capture)
            if replayed is not None:
                page, html = replayed
            else:
                page = uc.open(m_url, source_name=name, tracer=tracer, capture=capture)
                html = _wait_cloudflare(page, headless=False)
                web_checkpoint.record(page, m_url)
            page.close()
            if is_bot_check(html):
                logger.warning("%s: month +%d grid blocked, stopping the walk", name, offset)
                break
            m_links = _parse_calcium_links(html)
            logger.info("%s: +%d link(s) from month +%d.", name, len(m_links), offset)
            links += m_links
        except Exception as e:  # noqa: BLE001 -- the months already in hand still stand
            logger.warning("%s: month +%d grid failed (%s)", name, offset, e)
            continue
    return links


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
    # into its neighbours), so the same occurrence arrives twice. Keyed by
    # occurrence, not URL -- see `_calcium_row_key`.
    seen: set[str] = set()
    merged = []
    for lnk in event_links:
        key = _calcium_row_key(lnk.get("url", ""), lnk.get("date_hint") or "")
        if lnk.get("url") and key not in seen:
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
        for lnk, text in _fetch_event_details_parallel(uc, upcoming, timeout_ms):
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
            # Per occurrence: a series URL is shared by several rows.
            "id": f"web:{hash((lnk['url'], lnk['date_hint']))}",
            # The event's own date, not the scrape's clock. Relative phrases
            # downstream ("tonight") then resolve against the day the event is
            # on, and the message sorts where it belongs.
            "timestamp": f"{lnk['date_hint']}T00:00:00+00:00",
            "author": domain,
            "channel": unquote(lnk["url"])[:120],
            # The full, untruncated event URL — the row's own page. `channel`
            # is capped at 120 for display/grouping and cuts the URL mid-query,
            # so it isn't clickable; this preserves the real link for the event
            # to carry as its "open" link (see analysis._carry_calendar_dates).
            "url": lnk["url"],
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
        if is_bot_check(html):
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
        event_links += _calcium_forward_months(uc, src, url, name, tracer, capture)
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
            results = _fetch_event_details_parallel(uc, fetch_links, timeout_ms)
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

    # Not a listing. This handler is a program for a page of dated event
    # links, and a page without them is not that page. The raw-text fallback
    # that stood here emitted whatever was on it as one message. On every
    # night measured (2026-11-17..23) that was a Cloudflare challenge
    # (erobay), a no-JavaScript shell (midway) or navigation text (makerfaire).
    deviation = page_deviation(
        page, f"a calendar listing with at least 3 dated links (found {len(event_links)})",
    )
    page.close()
    raise deviation
