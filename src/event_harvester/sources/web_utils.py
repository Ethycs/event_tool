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
    """Save a lightweight fingerprint for a processed event link.

    Called after successfully fetching a detail page, so the same link
    is skipped on the next run. Skips navigation/UI links to avoid
    polluting the fingerprint store.
    """
    title = lnk.get("text", "") or ""
    if _is_junk_link_title(title):
        return  # Don't fingerprint navigation widgets

    from event_harvester.pipeline.event_match import save_fingerprint

    save_fingerprint({
        "title": title,
        "date": lnk.get("date_hint"),  # save_fingerprint normalizes free-text dates
        "link": lnk.get("url"),
    })


def _filter_known_links(event_links: list[dict]) -> list[dict]:
    """Filter out event links that match existing fingerprints.

    Checks each link's (text, date_hint, url) against the fingerprint store
    before expensive detail page fetches. Returns only new/unknown links.
    """
    from event_harvester.pipeline.event_match import _normalize_title, _titles_overlap, load_fingerprints

    fps = load_fingerprints()
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


# ── Cloudflare wait + scrolling ──────────────────────────────────────

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
    if "just a moment" not in html.lower() and "security verification" not in html.lower():
        return html
    if headless:
        logger.warning("Cloudflare challenge (headless can't solve). Skipping.")
        return html
    print("  Cloudflare captcha — click it in the browser window (15s timeout)...")
    for _ in range(15):
        page.wait_for_timeout(1000)
        try:
            html = page.content()
        except Exception:
            continue
        if "just a moment" not in html.lower():
            page.wait_for_timeout(3000)
            return page.content()
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


def _extract_event_links(page) -> list[dict]:
    """Extract event links by associating dates with links via DOM locality.

    The JavaScript logic lives in ext/helpers/extract_event_links.js for proper
    syntax highlighting and independent testability.

    Returns list of {url, text, date_hint} — links co-located with dates.
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


def _decimate_text(text: str, max_chars: int = 2000) -> str:
    """Extract the event-relevant region from page text.

    Adaptive: if the text is already small, return it whole.
    Otherwise, find date/time anchors and extract surrounding context.

    This reduces LLM input from ~5000 chars of nav junk to ~1000 chars
    of pure event content.
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
    context_before = 300
    region_start = max(0, best_start - context_before)
    region_end = min(len(text), best_start + max_chars)

    # Try to start at a word boundary
    while region_start > 0 and text[region_start] != " ":
        region_start -= 1

    return text[region_start:region_end].strip()


# ── Schema.org JSON-LD extraction + dead-page detection ──────────────

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
                    loc_str = ", ".join(p for p in [loc_name] + loc_parts if p)
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


# ── Detail page fetch (one + parallel) ───────────────────────────────

def _fetch_event_detail(context, stealth, url: str, timeout_ms: int) -> str | None:
    """Fetch a single event detail page.

    Tries schema.org JSON-LD first (structured, no LLM needed).
    Falls back to text extraction.
    """
    try:
        page = context.new_page()
        if stealth:
            stealth.apply_stealth_sync(page)
        page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)

        # Wait for Cloudflare challenge if present
        html = page.content()
        if "just a moment" in html.lower():
            for _ in range(15):
                page.wait_for_timeout(1000)
                try:
                    html = page.content()
                except Exception:
                    continue
                if "just a moment" not in html.lower():
                    page.wait_for_timeout(1000)
                    html = page.content()
                    break

        page.close()

        # Detect dead/deleted/cancelled pages
        if _is_dead_page(html):
            logger.info("Dead page (deleted/cancelled/not found): %s", url[:80])
            return None

        # Try structured data first
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

        # Fallback to text extraction
        text = _extract_text(html)
        return text if len(text) > 50 else None
    except Exception:
        return None


def _fetch_event_details_parallel(
    context, stealth, links: list[dict], timeout_ms: int, max_workers: int = 5,
) -> list[tuple[dict, str | None]]:
    """Fetch multiple event detail pages in parallel.

    Opens up to max_workers tabs simultaneously. Returns list of
    (link_dict, text_or_none) tuples.
    """
    results: list[tuple[dict, str | None]] = []

    # Process in batches to avoid too many open tabs
    for batch_start in range(0, len(links), max_workers):
        batch = links[batch_start:batch_start + max_workers]
        pages = []

        # Open all tabs in this batch
        for lnk in batch:
            try:
                page = context.new_page()
                if stealth:
                    stealth.apply_stealth_sync(page)
                page.goto(lnk["url"], timeout=timeout_ms, wait_until="domcontentloaded")
                pages.append((lnk, page))
            except Exception:
                pages.append((lnk, None))

        # Wait for all to settle, then collect
        for lnk, page in pages:
            if page is None:
                results.append((lnk, None))
                continue
            try:
                page.wait_for_timeout(1500)

                html = page.content()
                # Cloudflare wait
                if "just a moment" in html.lower():
                    for _ in range(10):
                        page.wait_for_timeout(1000)
                        try:
                            html = page.content()
                        except Exception:
                            continue
                        if "just a moment" not in html.lower():
                            page.wait_for_timeout(1000)
                            html = page.content()
                            break

                page.close()

                # Try structured data first
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
                    results.append((lnk, "\n".join(parts)))
                else:
                    text = _extract_text(html)
                    results.append((lnk, text if len(text) > 50 else None))
            except Exception:
                try:
                    page.close()
                except Exception:
                    pass
                results.append((lnk, None))

    return results
