"""Shared helpers for the web fetch pipeline.

Everything in here is mode-agnostic — used by 2+ of calendar / feed /
search / raw / chat, or by the orchestrator in ``web_fetch.py``. Mode
handlers live in ``web_modes/``.

Layout intent (after the Phase 3 split of the old monolithic web_fetch):

  web_utils.py     ← this file: cooldowns, fingerprint filter, link/text
                     extraction, structured data, dead-page check,
                     Cloudflare wait, scrolling, parallel detail fetch
  web_modes/       ← one mode handler per file (calendar/feed/search/raw/chat)
  web_fetch.py     ← orchestrator: fetch_web_sources + dispatch
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("event_harvester.web_fetch")

# ── Cooldowns ────────────────────────────────────────────────────────

_DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
_COOLDOWN_FILE = _DATA_DIR / ".web_cooldowns.json"
_DEFAULT_COOLDOWN = timedelta(hours=1)


def _load_cooldowns() -> dict[str, str]:
    """Load per-URL last-fetch timestamps."""
    if not _COOLDOWN_FILE.exists():
        return {}
    try:
        return json.loads(_COOLDOWN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cooldown(url: str) -> None:
    """Record that a URL was just fetched."""
    cooldowns = _load_cooldowns()
    cooldowns[url] = datetime.now(timezone.utc).isoformat()
    _COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _COOLDOWN_FILE.write_text(
        json.dumps(cooldowns, indent=2), encoding="utf-8",
    )


def _is_on_cooldown(url: str, cooldown: timedelta = _DEFAULT_COOLDOWN) -> bool:
    """Check if a URL was fetched recently."""
    cooldowns = _load_cooldowns()
    last = cooldowns.get(url)
    if not last:
        return False
    try:
        last_dt = datetime.fromisoformat(last)
        return datetime.now(timezone.utc) - last_dt < cooldown
    except Exception:
        return False


# ── Web source config ────────────────────────────────────────────────

_WEB_SOURCES_FILE = _DATA_DIR / "web_sources.json"

# Fallback defaults (used only if data/web_sources.json is missing).
_DEFAULT_SOURCES = [
    {"url": "https://lu.ma/discover", "name": "luma", "mode": "calendar", "scroll_seconds": 5},
    {"url": "https://business.sfchamber.com/events/calendar", "name": "sfchamber", "mode": "calendar", "scroll_seconds": 5},
    {"url": "https://www.erobay.com/", "name": "erobay", "mode": "calendar", "native_chrome": True, "cooldown_hours": 4, "scroll_seconds": 5},
    {"url": "https://www.instagram.com/", "name": "instagram", "mode": "feed", "api_pattern": r"graphql", "scroll_seconds": 25, "api_only": True},
    {"url": "https://www.eventbrite.com/d/ca--san-francisco/events/", "name": "eventbrite", "mode": "feed", "api_pattern": r"api|search|events", "scroll_seconds": 15},
    {"url": "https://www.meetup.com/find/?source=EVENTS&eventType=inPerson&sortField=RELEVANCE", "name": "meetup", "mode": "search", "query": "AI meetup", "scroll_seconds": 10},
    # Chat — query AI chatbots for event discovery (requires --web-login first)
    {"url": "https://chatgpt.com", "name": "chatgpt", "mode": "chat", "query": "What AI and tech events are happening in San Francisco this week? List each with date, time, location, and link.", "timeout_s": 45},
]


def _load_web_sources() -> list[dict]:
    """Load web sources from data/web_sources.json, falling back to defaults."""
    from event_harvester.utils import load_json

    sources = load_json(_WEB_SOURCES_FILE, default=None)
    return sources if sources is not None else list(_DEFAULT_SOURCES)


# ── Junk-title + fingerprint filter ──────────────────────────────────

_JUNK_LINK_TITLES = frozenset({
    "view all", "view more", "see all", "see more", "show all", "show more",
    "next", "previous", "prev", "more", "load more", "back", "home",
    "relative", "navigation", "menu", "filter", "sort", "search",
})


def _is_junk_link_title(title: str) -> bool:
    """Reject navigation/UI links that aren't actual events."""
    if not title:
        return True
    t = title.strip().lower()
    if not t:
        return True
    if t in _JUNK_LINK_TITLES:
        return True
    # Reject very short titles (likely nav widgets)
    if len(t) < 5:
        return True
    return False


def _save_link_fingerprint(lnk: dict) -> None:
    """No-op kept for backwards compatibility.

    Used to eagerly fingerprint every successfully-fetched detail page so
    we'd skip it on the next run. That was wrong by design: dedup should
    only fire AFTER the user decides on an event (approve/decline), not
    every time a detail page is fetched. Auto entries leaked into the
    filter via _filter_known_links and silently hid 90%+ of the calendar.
    Fingerprinting now happens only in approve_event / decline_event /
    ticktick push paths. This function stays as a no-op so existing
    call sites compile without removing imports.
    """
    return


def _filter_known_links(event_links: list[dict]) -> list[dict]:
    """Filter out event links the user has already decided on.

    Pre-fetch optimization: skip detail-page fetches for events whose
    fingerprint is marked ``approved`` or ``declined``. Auto-status
    entries (legacy or otherwise) are ignored — a prior sighting alone
    does NOT count as "known" since the user hasn't acted on it.
    """
    from event_harvester.pipeline.event_match import (
        _normalize_title,
        _titles_overlap,
        load_fingerprints,
    )

    fps = [fp for fp in load_fingerprints() if fp.get("status") in ("approved", "declined")]
    if not fps:
        return event_links

    # Build fingerprint lookup: link index + title/date sets
    fp_links = set()
    fp_sigs = []
    for fp in fps:
        link = (fp.get("link") or "").strip().lower()
        if link:
            fp_links.add(link)
        fp_sigs.append({
            "title_words": _normalize_title(fp.get("title", "")),
            "date": (fp.get("date") or "")[:10],
        })

    new_links = []
    skipped = 0
    for lnk in event_links:
        # Fast path: exact URL match
        link_url = lnk.get("url", "").strip().lower()
        if link_url and link_url in fp_links:
            skipped += 1
            continue

        # Fuzzy path: title + date match
        lnk_title = _normalize_title(lnk.get("text", ""))
        lnk_date = (lnk.get("date_hint") or "")[:10]
        matched = False
        for fp_sig in fp_sigs:
            score = 0
            if _titles_overlap(lnk_title, fp_sig["title_words"]):
                score += 1
            if lnk_date and fp_sig["date"] and lnk_date == fp_sig["date"]:
                score += 1
            if score >= 2:
                matched = True
                break
        if matched:
            skipped += 1
            continue

        new_links.append(lnk)

    if skipped:
        logger.info("Fingerprint filter: %d known, %d new.", skipped, len(new_links))
    return new_links


# ── Auto-mode detection (used by orchestrator dispatch) ──────────────

def _detect_mode(uc, page, src: dict) -> str:
    """Auto-detect the best mode for a page using the same signals as --test-url."""
    # webcal://* or *.ics URLs are iCalendar feeds -- shortcut before doing
    # any browser-based signal detection.  Handled by web_modes.ics.
    raw_url = src.get("url", "")
    if raw_url.startswith(("webcal://", "webcals://")) or raw_url.rstrip("/").endswith(".ics"):
        return "ics"

    event_links = _extract_event_links(page)
    if len(event_links) >= 3:
        return "calendar"

    search_hits = uc.detect(page, "search")
    if search_hits and src.get("query"):
        return "search"

    feed_items = uc.get_feed_items(page)
    feed_items = [t for t in feed_items if len(t) > 30]
    if len(feed_items) >= 3:
        return "feed"

    return "raw"


# ── Deviation: the page is not the page a source was written for ─────

class SourceDeviated(Exception):
    """A source's page is not what its handler was written for.

    Every handler here is a program for a known page: a Calcium grid, a
    listing of dated links, a profile that issues GraphQL, a recorded route.
    When the page stops being that, the handler is the one placed to notice,
    at the point where its own expectation fails. It raises this and emits
    nothing; it does not fall back to scraping whatever is there.

    ``kind`` and ``evidence`` say what the page looks like instead. They come
    from the rule layer of ``observability.hints`` (one ``page.evaluate``, no
    vision call), which already names login walls, bot checks, error pages
    and empty results.
    """

    def __init__(self, expected: str, kind: str = "unknown", evidence: str = ""):
        self.expected = expected
        self.kind = kind
        self.evidence = evidence
        looks = f"{kind} ({evidence})" if evidence else kind
        super().__init__(f"expected {expected}; the page looks like: {looks}")


class SourceBlocked(SourceDeviated):
    """The deviation is a bot check: it needs a person at the browser."""


def make_deviation(expected: str, kind: str = "unknown", evidence: str = "") -> SourceDeviated:
    """A :class:`SourceDeviated`, or :class:`SourceBlocked` for a bot check."""
    cls = SourceBlocked if kind == "cloudflare_challenge" else SourceDeviated
    return cls(expected, kind, evidence)


def page_deviation(page, expected: str) -> SourceDeviated:
    """The deviation to raise for ``page``. Call it while the page is open."""
    from event_harvester.observability.hints import diagnose

    kind, evidence = "unknown", ""
    try:
        top = diagnose(page, use_vlm=False).top
        if top is not None:
            kind, evidence = top.kind, top.evidence
    except Exception as e:  # noqa: BLE001 — a page that cannot be read still deviated
        logger.debug("deviation diagnosis failed: %s", e)
    return make_deviation(expected, kind, evidence)


# ── Bot-check pages + scrolling ──────────────────────────────────────

#: Text that marks a bot-check interstitial (Cloudflare's, in practice) served
#: in place of the page that was asked for.
_BOT_CHECK_MARKERS = ("just a moment", "security verification")


def is_bot_check(html: str) -> bool:
    """True when ``html`` is a bot-check interstitial, not the requested page."""
    lowered = html.lower()
    return any(marker in lowered for marker in _BOT_CHECK_MARKERS)


def _poll_bot_check(page, html: str, *, seconds: int, settle_ms: int) -> str:
    """Re-read ``page`` once a second until its bot check is gone.

    Returns the settled HTML, or the last HTML read if the check never clears;
    the caller tells the two apart with :func:`is_bot_check`.
    """
    for _ in range(seconds):
        page.wait_for_timeout(1000)
        try:
            html = page.content()
        except Exception:
            continue
        if not is_bot_check(html):
            page.wait_for_timeout(settle_ms)
            return page.content()
    return html


def _wait_cloudflare(page, headless: bool = True) -> str:
    """Wait for Cloudflare challenge to resolve. Returns page HTML."""
    try:
        page.wait_for_load_state("load", timeout=15000)
    except Exception:
        pass
    # Retry page.content() — native Chrome CDP may still be navigating
    html = None
    for _ in range(5):
        try:
            html = page.content()
            break
        except Exception:
            page.wait_for_timeout(1000)
    if html is None:
        html = page.content()  # final attempt, let it raise
    if not is_bot_check(html):
        return html
    if headless:
        logger.warning("Cloudflare challenge (headless can't solve). Skipping.")
        return html
    print("  Cloudflare captcha — click it in the browser window (15s timeout)...")
    html = _poll_bot_check(page, html, seconds=15, settle_ms=3000)
    if is_bot_check(html):
        logger.warning("Cloudflare did not resolve in 15s. Skipping.")
    return html


DEFAULT_SCROLL_SECONDS = 5


def _scroll_for_seconds(page, seconds: int, step_ms: int = 800) -> None:
    """Scroll one viewport every ``step_ms`` for ``seconds`` total seconds.

    Time-bounded (wall clock), not iteration-bounded. Every ``_do_*`` mode
    that exposes a per-source ``scroll_seconds`` knob routes through here so
    "scroll time" means the same thing across calendar / search / raw modes.
    Feed mode keeps its own dwell loop because it interleaves API capture
    and GIF-frame snapshots with each scroll.

    Tolerates ``page.evaluate`` failures (page may close mid-scroll on
    Cloudflare redirects, etc.) by returning early.
    """
    if seconds <= 0:
        return
    import time as _time
    end = _time.time() + seconds
    while _time.time() < end:
        try:
            page.evaluate("window.scrollBy(0, window.innerHeight)")
        except Exception:
            return
        page.wait_for_timeout(step_ms)


# ── Event link extraction (DOM via JS) ───────────────────────────────

_JS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "ext" / "helpers"
_EVENT_LINKS_JS = (_JS_DIR / "extract_event_links.js").read_text(encoding="utf-8")


#: The five extraction strategies, ordered most to least trustworthy. The order
#: is the point: `js-embedded` reads the date and id out of the href itself,
#: while `near-date-fallback` may accept a link whose only qualification is that
#: a date appeared within five ancestors. Merging their output into one flat
#: list without saying which fired is what made a promo carousel
#: indistinguishable from a search result downstream.
LINK_STRATEGIES: tuple[str, ...] = (
    "js-embedded",
    "dated-card",
    "dated-card-image",
    "repeated-child",
    "near-date-fallback",
)


def region_key(link: dict) -> str:
    """Canonical key for the page region a link came from. ``""`` if unknown.

    Canonical *at mint time*, deliberately — the alternative is clustering
    regions after the fact, which inherits the order-dependence already recorded
    against ``events_match`` (roadmap 2.6). For events that is a nuisance; for
    regions it would merge a carousel with a results list and silently poison
    everything learned from either.

    Heading first, because class names on Meetup and Eventbrite are build-hashed
    and change on every deploy — a key built from them rots silently and looks
    exactly like a region that stopped producing.
    """
    region = link.get("region") or {}
    heading = (region.get("heading") or "").strip().lower()
    if heading:
        return f"h:{heading[:60]}"
    landmark = (region.get("landmark") or "").strip().lower()
    path = (region.get("path") or "").strip().lower()
    if landmark and path:
        return f"l:{landmark}|{path[:80]}"
    return f"p:{path[:80]}" if path else ""


def _extract_event_links(page) -> list[dict]:
    """Extract event links by associating dates with links via DOM locality.

    The JavaScript logic lives in ext/helpers/extract_event_links.js for proper
    syntax highlighting and independent testability.

    Returns list of ``{url, text, date_hint, strategy, region}`` — links
    co-located with dates. ``strategy`` names which of :data:`LINK_STRATEGIES`
    produced the link; ``region`` locates it on the page (``heading``,
    ``landmark``, ``depth``, ``path``). Both are provenance added 2026-07-30,
    and both may be absent on links produced by older code paths — treat them as
    optional and use :func:`region_key` rather than reading ``region`` directly.
    """
    raw = page.evaluate(_EVENT_LINKS_JS)
    return raw or []


# ── HTML text extraction + decimation ────────────────────────────────

def _extract_text(html: str) -> str:
    """Strip HTML tags and return readable text."""
    # Remove script and style blocks
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html).strip()


# Date/time patterns for content region detection (reuse from weights.py)
_DECIMATE_DATE_RE = re.compile(
    r"(?i)(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}"
    r"|\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
    r"|\d{4}-\d{2}-\d{2}"
    r"|(?:mon|tue|wed|thu|fri|sat|sun)(?:day)?(?:\s*,)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
)
_DECIMATE_TIME_RE = re.compile(
    r"(?i)\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b"
    r"|\b(?:[01]\d|2[0-3]):[0-5]\d\b"
)


def _decimate_text(
    text: str, max_chars: int = 2000, context_before: int = 300,
) -> str:
    """Extract the event-relevant region from page text.

    Adaptive: if the text is already small, return it whole.
    Otherwise, find date/time anchors and extract surrounding context.

    This reduces LLM input from ~5000 chars of nav junk to ~1000 chars
    of pure event content.

    ``context_before`` is how far to reach back from the densest anchor to
    catch the title that usually precedes a date. The Gmail path passes a
    much smaller value: `analysis.build_prompt` truncates each message to 400
    characters *and* derives its date hints from that same slice, so 300
    characters of preamble would push the date out of both.
    """
    if len(text) <= max_chars:
        return text

    # Find all date/time positions
    anchors = []
    for m in _DECIMATE_DATE_RE.finditer(text):
        anchors.append(m.start())
    for m in _DECIMATE_TIME_RE.finditer(text):
        anchors.append(m.start())

    if not anchors:
        # No dates found — take the middle of the text (skip header/footer)
        start = len(text) // 5
        return text[start:start + max_chars]

    # Find the region that contains the most date/time anchors
    # Use a window of max_chars centered on the anchor cluster
    anchors.sort()
    # Cluster: find the densest group within max_chars window
    best_start = 0
    best_count = 0
    for i, pos in enumerate(anchors):
        window_end = pos + max_chars
        count = sum(1 for a in anchors if pos <= a < window_end)
        if count > best_count:
            best_count = count
            best_start = pos

    # Expand backwards to capture title (usually before the first date)
    region_start = max(0, best_start - context_before)
    region_end = min(len(text), best_start + max_chars)

    # Try to start at a word boundary
    while region_start > 0 and text[region_start] != " ":
        region_start -= 1

    return text[region_start:region_end].strip()


def parse_next_data(html: str) -> dict | None:
    """Return the `__NEXT_DATA__` payload a Next.js page server-renders.

    Next.js inlines the props it rendered the page with. On an event platform
    that is the event list itself — already structured, already typed, and
    already there without running a line of JavaScript. Measured 2026-08-10:
    `luma.com/sf` carries 20 whole events this way and `partiful.com/explore`
    carries 5 per city, against the **1** event the browser-driven `raw`/
    `calendar` scrape of Luma's own discover page produced.

    Returns None when the tag is absent or unparseable — the caller decides
    whether that is fatal.
    """
    if not html:
        return None
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL,
    )
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


# ── Schema.org JSON-LD extraction + dead-page detection ──────────────

def _first_image(value) -> str:
    """schema.org `image`: a URL, a list of them, or an ImageObject."""
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, dict):
        value = value.get("url") or value.get("contentUrl") or ""
    return str(value or "").strip()


def _extract_structured_data(html: str) -> dict | None:
    """Extract schema.org Event JSON-LD from HTML.

    Many event sites embed structured data like:
      <script type="application/ld+json">{"@type": "Event", ...}</script>

    Returns a dict with title, date, time, location, link, details — or None.
    """
    import json as _json

    # Find all JSON-LD blocks
    for m in re.finditer(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        try:
            data = _json.loads(m.group(1))
        except Exception:
            continue

        # Handle @graph arrays
        items = [data] if isinstance(data, dict) else data if isinstance(data, list) else []
        if isinstance(data, dict) and "@graph" in data:
            items = data["@graph"]

        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type", "")
            if isinstance(item_type, list):
                item_type = " ".join(item_type)
            if "Event" not in item_type:
                continue

            # Extract fields
            title = item.get("name", "")
            start = item.get("startDate", "")
            end = item.get("endDate", "")
            desc = item.get("description", "")
            url = item.get("url", "")

            # Location
            loc = item.get("location", {})
            if isinstance(loc, dict):
                loc_name = loc.get("name", "")
                address = loc.get("address", {})
                if isinstance(address, dict):
                    loc_parts = [address.get("streetAddress", ""),
                                 address.get("addressLocality", "")]
                    # Venues routinely repeat themselves across `name` and
                    # `streetAddress` ("DG717, DG717, San Francisco"), so drop
                    # case-insensitive repeats while keeping document order.
                    seen_parts: list[str] = []
                    for p in [loc_name] + loc_parts:
                        p = (p or "").strip()
                        if p and p.lower() not in {s.lower() for s in seen_parts}:
                            seen_parts.append(p)
                    loc_str = ", ".join(seen_parts)
                elif isinstance(address, str):
                    loc_str = f"{loc_name}, {address}" if loc_name else address
                else:
                    loc_str = loc_name
            elif isinstance(loc, str):
                loc_str = loc
            else:
                loc_str = ""

            # Parse date/time
            date_str = start[:10] if start else ""
            time_str = ""
            if "T" in start:
                time_str = start.split("T")[1][:5]
                if end and "T" in end:
                    time_str += " - " + end.split("T")[1][:5]

            if title:
                return {
                    "title": title,
                    "date": date_str,
                    "time": time_str,
                    "location": loc_str,
                    "link": url,
                    "details": desc[:500] if desc else "",
                    # Added 2026-08-12 for report thumbnails. schema.org
                    # allows a bare URL, a list, or an ImageObject, so the
                    # shape is normalised here rather than at each caller.
                    "image": _first_image(item.get("image")),
                }

    return None


_DEAD_PAGE_RE = re.compile(
    r"event\s+(?:has\s+been\s+)?(?:deleted|removed|cancelled|canceled)"
    r"|no\s+longer\s+available"
    r"|this\s+event\s+(?:was|has\s+been)\s+(?:deleted|removed|cancelled|canceled)"
    r"|page\s+not\s+found"
    r"|404\s+not\s+found"
    r"|event\s+not\s+found",
    re.IGNORECASE,
)


def _is_dead_page(html: str) -> bool:
    """Detect pages for deleted, cancelled, or removed events."""
    # Check visible text only (strip tags for a lightweight scan)
    text = re.sub(r"<[^>]+>", " ", html)
    # Only check the first 3000 chars — dead-page notices are always near the top
    return bool(_DEAD_PAGE_RE.search(text[:3000]))


# ── Detail page fetch ────────────────────────────────────────────────

def _detail_text(html: str) -> str | None:
    """What a detail page says about its event.

    Tries schema.org JSON-LD first (structured, no LLM needed).
    Falls back to text extraction.
    """
    structured = _extract_structured_data(html)
    if structured:
        parts = [structured["title"]]
        if structured["date"]:
            parts.append(f"date: {structured['date']}")
        if structured["time"]:
            parts.append(f"time: {structured['time']}")
        if structured["location"]:
            parts.append(f"location: {structured['location']}")
        if structured["link"]:
            parts.append(f"link: {structured['link']}")
        if structured["details"]:
            parts.append(structured["details"])
        return "\n".join(parts)
    text = _extract_text(html)
    return text if len(text) > 50 else None


def _close_quietly(page) -> None:
    try:
        page.close()
    except Exception:
        pass


def _fetch_event_details_parallel(
    uc, links: list[dict], timeout_ms: int, max_workers: int = 5,
) -> list[tuple[dict, str | None]]:
    """Fetch multiple event detail pages in parallel.

    Opens up to max_workers tabs simultaneously through ``uc.new_page()``.
    Returns list of (link_dict, text_or_none) tuples. Every tab opened here is
    closed here, whether or not its page loaded. A page still showing a bot
    check after its wait yields ``None``: the challenge text is not the event.
    """
    blocked = 0
    results: list[tuple[dict, str | None]] = []

    # Process in batches to avoid too many open tabs
    for batch_start in range(0, len(links), max_workers):
        batch = links[batch_start:batch_start + max_workers]
        pages = []

        # Open all tabs in this batch
        for lnk in batch:
            page = None
            try:
                page = uc.new_page()
                page.goto(lnk["url"], timeout=timeout_ms, wait_until="domcontentloaded")
            except Exception:
                if page is not None:
                    _close_quietly(page)
                page = None
            pages.append((lnk, page))

        # Wait for all to settle, then collect
        for lnk, page in pages:
            if page is None:
                results.append((lnk, None))
                continue
            try:
                page.wait_for_timeout(1500)
                html = page.content()
                if is_bot_check(html):
                    html = _poll_bot_check(page, html, seconds=10, settle_ms=1000)
                if is_bot_check(html):
                    blocked += 1
                    results.append((lnk, None))
                else:
                    results.append((lnk, _detail_text(html)))
            except Exception:
                results.append((lnk, None))
            finally:
                _close_quietly(page)

    if blocked:
        logger.warning("%d of %d detail page(s) blocked by a bot check.", blocked, len(links))
    return results
