"""Fetch event pages using Playwright with saved session state.

First run: use --web-login to open a browser window, log into your
event sites, then close. The session is saved to data/.playwright_state.json.
Subsequent runs reuse the saved session automatically.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("event_harvester.web_fetch")

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
    {"url": "https://lu.ma/discover", "name": "luma", "mode": "calendar"},
    {"url": "https://business.sfchamber.com/events/calendar", "name": "sfchamber", "mode": "calendar"},
    {"url": "https://www.erobay.com/", "name": "erobay", "mode": "calendar", "native_chrome": True, "cooldown_hours": 4},
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


# ── Pre-fetch fingerprint filter + save ───────────────────────────────

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

    from event_harvester.event_match import save_fingerprint

    save_fingerprint({
        "title": title,
        "date": lnk.get("date_hint"),  # save_fingerprint normalizes free-text dates
        "link": lnk.get("url"),
    })


# ── Pre-fetch fingerprint filter ──────────────────────────────────────

def _filter_known_links(event_links: list[dict]) -> list[dict]:
    """Filter out event links that match existing fingerprints.

    Checks each link's (text, date_hint, url) against the fingerprint store
    before expensive detail page fetches. Returns only new/unknown links.
    """
    from event_harvester.event_match import _normalize_title, _titles_overlap, load_fingerprints

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


# ── Auto-mode detection ───────────────────────────────────────────────

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


# ── Mode handlers ─────────────────────────────────────────────────────

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


# ── Calcium calendar parser (erobay etc.) ────────────────────────────

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


def _do_calendar(uc, src: dict, timeout_ms: int, max_events: int, page=None) -> list[dict]:
    """Calendar mode: load page, extract date↔link pairs, fetch detail pages."""
    from urllib.parse import urlparse

    url = src["url"]
    domain = urlparse(url).netloc
    logger.info("Calendar: %s", src.get("name", url))

    if page is None:
        page = uc.open(url)
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
    scroll_secs = src.get("scroll_seconds", 3)
    for _ in range(scroll_secs):
        page.evaluate("window.scrollBy(0, window.innerHeight)")
        page.wait_for_timeout(800)

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


def _do_feed(uc, src: dict, max_events: int = 30, page=None) -> list[dict]:
    """Feed mode: scroll page while intercepting API responses.

    Note: page param is accepted for interface consistency but feed mode
    always opens its own page (needs response interception from first load).
    If a pre-opened page is passed, it's closed and re-opened with interception.
    """
    if page is not None:
        page.close()  # need to re-open with interception
    import json as json_mod
    import time as _time
    from urllib.parse import urlparse

    url = src["url"]
    api_pattern = src.get("api_pattern", "graphql|api")
    scroll_secs = src.get("scroll_seconds", 30)
    name = src.get("name", "web")

    logger.info("Feed: scrolling %s for %ds...", name, scroll_secs)

    page, captured = uc.open_with_intercept(url, api_pattern)
    page.wait_for_timeout(3000)

    # UC obstacle clearing + feed detection
    uc.detect_all(page)
    uc.dismiss_cookies(page)
    uc.close_modal(page)

    # Use UC-detected feed container for targeted scrolling
    feed_selector = None
    patterns = uc.get_patterns(page)
    if patterns.get("feed"):
        feed_selector = patterns["feed"][0].get("selector")
        logger.info("UC: scrolling detected feed container: %s", feed_selector)

    # Scroll (targeted if feed detected, full-page otherwise)
    end_time = _time.time() + scroll_secs
    scroll_count = 0
    while _time.time() < end_time:
        if feed_selector:
            page.evaluate(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (el) el.scrollTop += el.clientHeight * 2;
                    else window.scrollBy(0, window.innerHeight * 2);
                }""",
                feed_selector,
            )
        else:
            page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
        page.wait_for_timeout(800)
        scroll_count += 1

    # Get page text — prefer feed container
    if feed_selector:
        page_text = page.evaluate(
            "(sel) => { const el = document.querySelector(sel); return el ? el.innerText : document.body.innerText; }",
            feed_selector,
        )
    else:
        page_text = page.evaluate("document.body.innerText") or ""
    logger.info("%s: scrolled %d times, captured %d API responses, %d chars page text.",
                name, scroll_count, len(captured), len(page_text))

    messages = []
    domain = urlparse(url).netloc

    # Try event link extraction from scrolled page (maximizes events)
    # Skip for api_only sources (e.g. Instagram where links aren't event pages)
    event_links = []
    if not src.get("api_only"):
        event_links = _extract_event_links(page)
        event_links = _filter_known_links(event_links)
        if event_links:
            fetch_links = [l for l in event_links[:max_events] if not l.get("inline")]
            logger.info("%s: %d new event links, fetching in parallel...", name, len(fetch_links))
            results = _fetch_event_details_parallel(
                uc._context, uc._stealth_plugin, fetch_links, uc.timeout_ms,
            )
            for lnk, text in results:
                if text:
                    _save_link_fingerprint(lnk)
                    messages.append({
                        "platform": "web",
                        "id": f"web:{hash(lnk['url'])}",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "author": urlparse(lnk["url"]).netloc,
                        "channel": lnk["url"][:120],
                        "content": text[:5000],
                    })

    # Page text as a message (if no event links found)
    if not event_links and page_text and len(page_text) > 200:
        messages.append({
            "platform": name,
            "id": f"feed:page:{hash(url)}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "author": name,
            "channel": url[:120],
            "content": page_text[:5000],
        })

    page.close()

    # Parse captured API responses
    for resp_url, resp_body in captured:
        try:
            data = json_mod.loads(resp_body)
            posts = _extract_posts_from_api(data, resp_url, name)
            messages.extend(posts)
        except json_mod.JSONDecodeError:
            pass

    return messages


def _do_search(uc, src: dict, timeout_ms: int, max_events: int, page=None) -> list[dict]:
    """Search mode: detect search bar, query, scroll, extract event links."""
    from urllib.parse import urlparse

    url = src["url"]
    query = src.get("query")
    scroll_secs = src.get("scroll_seconds", 10)
    name = src.get("name", "web")

    logger.info("Search: %s", name)

    if page is None:
        page = uc.open(url)
    uc.detect_all(page)
    uc.dismiss_cookies(page)
    uc.close_modal(page)

    if uc.has_login_wall(page):
        logger.warning("%s: login wall — skipping", name)
        page.close()
        return []

    # Search if query provided
    if query:
        uc.first_scan(page)
        searched = uc.search(page, query)

        # Fallback: generic input discovery if static detect missed the search bar
        if not searched:
            # Try UC generic discovery first, then inline JS fallback
            inputs = uc.find_inputs(page)
            if not inputs:
                inputs = page.evaluate("""() => {
                    const els = document.querySelectorAll(
                        'input[type="search"], input[type="text"], input:not([type]), textarea'
                    );
                    for (const el of els) {
                        const rect = el.getBoundingClientRect();
                        if (rect.width === 0 || rect.height === 0) continue;
                        const style = getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden') continue;
                        let sel = '';
                        if (el.id) sel = '#' + el.id;
                        else if (el.getAttribute('placeholder'))
                            sel = el.tagName.toLowerCase()
                                + '[placeholder="' + el.getAttribute('placeholder').replace(/"/g, '\\\\"') + '"]';
                        else sel = el.tagName.toLowerCase() + '[type="' + (el.type || 'text') + '"]';
                        return [{selector: sel, score: 1}];
                    }
                    return [];
                }""") or []

            if inputs:
                best = inputs[0]
                logger.info("%s: using generic input: %s (score=%.1f)",
                            name, best.get("selector"), best.get("score", 0))
                try:
                    page.fill(best["selector"], query)
                    page.press(best["selector"], "Enter")
                    searched = True
                except Exception as e:
                    logger.debug("Generic search fill failed: %s", e)

        if searched:
            page.wait_for_timeout(3000)
            diff = uc.next_scan(page)
            detected = uc.auto_detect(page)
            if diff:
                logger.info("%s: search → %d changed, %d added",
                            name, diff.get("changed", 0), diff.get("added", 0))

    # Scroll
    for _ in range(scroll_secs):
        page.evaluate("window.scrollBy(0, window.innerHeight)")
        page.wait_for_timeout(1000)

    # Extract event links, filter already-known ones
    event_links = _filter_known_links(_extract_event_links(page))[:max_events]
    messages = []
    domain = urlparse(url).netloc

    if event_links:
        logger.info("%s: %d event links, fetching in parallel...", name, len(event_links))
        results = _fetch_event_details_parallel(
            uc._context, uc._stealth_plugin, event_links, timeout_ms,
        )
        for lnk, text in results:
            if text:
                _save_link_fingerprint(lnk)
                messages.append({
                    "platform": "web",
                    "id": f"web:{hash(lnk['url'])}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "author": urlparse(lnk["url"]).netloc,
                    "channel": lnk["url"][:120],
                    "content": text[:5000],
                })
    else:
        # Fallback: feed items
        items = uc.get_feed_items(page)
        for i, item_text in enumerate(items):
            if len(item_text) > 30:
                messages.append({
                    "platform": "web",
                    "id": f"web:{hash(item_text[:100])}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "author": domain,
                    "channel": f"{name}-feed",
                    "content": item_text[:5000],
                })

    page.close()
    return messages


def _do_raw(uc, src: dict, page=None) -> list[dict]:
    """Raw mode: just load page and grab text."""
    from urllib.parse import urlparse

    url = src["url"]
    if page is None:
        page = uc.open(url)
        html = _wait_cloudflare(page, headless=False)
    else:
        html = page.content()
    uc.dismiss_cookies(page)
    text = _extract_text(html)
    page.close()

    if len(text) < 50:
        return []

    domain = urlparse(url).netloc
    return [{
        "platform": "web",
        "id": f"web:{hash(url)}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "author": domain,
        "channel": url[:120],
        "content": text[:8000],
    }]


def _do_chat(uc, src: dict) -> list[dict]:
    """Chat mode: query an AI chatbot and extract events from its response."""
    from urllib.parse import urlparse

    url = src["url"]
    query = src.get("query", "What events are happening this week? List each with date, time, location, and link.")
    timeout_s = src.get("timeout_s", 45)
    name = src.get("name", "chat")

    logger.info("Chat: %s (query: %s...)", name, query[:60])

    page = uc.open(url, wait_ms=5000)
    uc.dismiss_cookies(page)
    uc.close_modal(page)

    if uc.has_login_wall(page):
        logger.warning("%s: login wall — run --web-login first", name)
        page.close()
        return []

    response = uc.chat(page, query, timeout_s=timeout_s)
    page.close()

    if not response or len(response) < 50:
        logger.warning("%s: no usable response (%d chars)", name, len(response or ""))
        return []

    logger.info("%s: got %d chars response", name, len(response))
    domain = urlparse(url).netloc

    return [{
        "platform": "web",
        "id": f"chat:{name}:{hash(query)}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "author": f"chat:{name}",
        "channel": f"chat-{name}",
        "content": response[:8000],
    }]


# ── Unified entry point ──────────────────────────────────────────────

def fetch_web_sources(
    sources: list[dict] | None = None,
    max_events: int = 30,
    timeout_ms: int = 30000,
    no_cooldown: bool = False,
) -> list[dict]:
    """Fetch events from all configured web sources.

    Groups sources by headless/visible, launches one UCBrowser per group,
    dispatches each source to its mode handler.
    """
    if sources is None:
        sources = _load_web_sources()
    if not sources:
        return []

    # Filter cooldowns
    active = []
    for src in sources:
        cd_hours = src.get("cooldown_hours", -1)
        if not no_cooldown and cd_hours >= 0 and _is_on_cooldown(src["url"], timedelta(hours=cd_hours)):
            logger.info("Skipping %s (on cooldown).", src.get("name", src["url"]))
        else:
            active.append(src)
    if not active:
        return []

    # Group: standard sources (Chromium + UC extension) vs native Chrome (Cloudflare)
    standard_srcs = [s for s in active if not s.get("native_chrome")]
    native_srcs = [s for s in active if s.get("native_chrome")]

    import concurrent.futures

    def _process_group(group: list[dict], **uc_kwargs) -> list[dict]:
        from event_harvester.sources.uc_browser import UCBrowser

        messages = []
        cf_retries = []
        with UCBrowser(**uc_kwargs) as uc:
            for src in group:
                mode = src.get("mode")
                name = src.get("name", src["url"])
                try:
                    # Chat mode doesn't need page pre-open
                    if mode == "chat":
                        msgs = _do_chat(uc, src)
                        messages.extend(msgs)
                        if msgs:
                            _save_cooldown(src["url"])
                        logger.info("%s: %d messages.", name, len(msgs))
                        continue

                    # All other modes: open page, detect mode if needed
                    page = uc.open(src["url"])
                    html = _wait_cloudflare(page, headless=False)

                    # Cloudflare blocked — queue for native Chrome retry
                    if "just a moment" in html.lower():
                        if not uc_kwargs.get("native_chrome"):
                            logger.info("%s: Cloudflare — queuing for native Chrome retry.", name)
                            cf_retries.append(src)
                            page.close()
                            continue

                    # Auto-mode: check saved signatures, then detect
                    if not mode or mode == "auto":
                        uc.detect_all(page)
                        uc.dismiss_cookies(page)
                        uc.close_modal(page)

                        # Check saved signature first
                        try:
                            sigs = uc.load_signatures(page)
                            mode_sig = next(
                                (s for s in sigs if s.get("pattern", "").startswith("mode:")),
                                None,
                            )
                            if mode_sig:
                                mode = mode_sig["pattern"].split(":")[1]
                                logger.info("%s: saved signature → mode=%s", name, mode)
                        except Exception:
                            pass

                        if not mode or mode == "auto":
                            mode = _detect_mode(uc, page, src)
                            logger.info("%s: auto-detected mode=%s", name, mode)

                    # Dispatch with pre-opened page
                    if mode == "calendar":
                        msgs = _do_calendar(uc, src, timeout_ms, max_events, page=page)
                    elif mode == "feed":
                        msgs = _do_feed(uc, src, max_events, page=page)
                    elif mode == "search":
                        msgs = _do_search(uc, src, timeout_ms, max_events, page=page)
                    else:
                        msgs = _do_raw(uc, src, page=page)

                    # Save mode as signature on success
                    if msgs:
                        try:
                            uc.save_signature(page, f"mode:{mode}")
                        except Exception:
                            pass
                    messages.extend(msgs)
                    if msgs:
                        _save_cooldown(src["url"])
                    logger.info("%s: %d messages.", name, len(msgs))
                except Exception as e:
                    logger.error("Failed %s (%s): %s", name, mode, e)
        return messages, cf_retries

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = []
        if standard_srcs:
            futures.append(pool.submit(_process_group, standard_srcs))
        if native_srcs:
            futures.append(pool.submit(_process_group, native_srcs, native_chrome=True))

        all_messages = []
        all_cf_retries = []
        for f in concurrent.futures.as_completed(futures):
            msgs, retries = f.result()
            all_messages.extend(msgs)
            all_cf_retries.extend(retries)

    # Phase 4: Cloudflare auto-retry with native Chrome
    if all_cf_retries:
        logger.info("Retrying %d source(s) with native Chrome (Cloudflare)...", len(all_cf_retries))
        retry_msgs, _ = _process_group(all_cf_retries, native_chrome=True)
        all_messages.extend(retry_msgs)

    logger.info("Web sources: %d messages from %d sources.", len(all_messages), len(active))
    return all_messages





def _extract_posts_from_api(data: dict, url: str, platform: str) -> list[dict]:
    """Extract individual posts from a feed API response.

    Handles Instagram GraphQL timeline responses and other common
    feed structures. Returns one message dict per post.
    """
    posts = []
    data_inner = data.get("data", data)

    # Walk through all values looking for edges/items with media+caption
    def _walk(obj, depth=0):
        if depth > 5 or not isinstance(obj, dict):
            return

        # Instagram timeline: edges → node → media → caption
        edges = obj.get("edges", [])
        if isinstance(edges, list):
            for edge in edges:
                node = edge.get("node", {}) if isinstance(edge, dict) else {}
                media = node.get("media") or {}
                if not isinstance(media, dict):
                    # Try explore_story.media
                    es = node.get("explore_story", {})
                    media = es.get("media", {}) if isinstance(es, dict) else {}

                if not isinstance(media, dict) or not media:
                    continue

                user = media.get("user", {})
                username = user.get("username", "?") if isinstance(user, dict) else "?"

                caption = media.get("caption")
                text = ""
                if isinstance(caption, dict):
                    text = caption.get("text", "")
                elif isinstance(caption, str):
                    text = caption

                taken_at = media.get("taken_at", "")
                media_id = str(media.get("id", media.get("pk", "")))

                if text and len(text) > 10:
                    ts = ""
                    if taken_at:
                        try:
                            ts = datetime.fromtimestamp(int(taken_at), tz=timezone.utc).isoformat()
                        except (ValueError, OSError):
                            pass

                    posts.append({
                        "platform": platform,
                        "id": f"ig:{media_id}" if media_id else f"ig:{hash(text[:50])}",
                        "timestamp": ts or datetime.now(timezone.utc).isoformat(),
                        "author": f"@{username}",
                        "channel": "instagram-feed",
                        "content": text[:2000],
                    })

        # Also check items arrays (some APIs use items instead of edges)
        items = obj.get("items", [])
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                caption = item.get("caption")
                text = ""
                if isinstance(caption, dict):
                    text = caption.get("text", "")
                user = item.get("user", {})
                username = user.get("username", "?") if isinstance(user, dict) else "?"
                media_id = str(item.get("id", item.get("pk", "")))

                if text and len(text) > 10:
                    posts.append({
                        "platform": platform,
                        "id": f"ig:{media_id}" if media_id else f"ig:{hash(text[:50])}",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "author": f"@{username}",
                        "channel": "instagram-feed",
                        "content": text[:2000],
                    })

        # Recurse into dict values
        for v in obj.values():
            if isinstance(v, dict):
                _walk(v, depth + 1)

    _walk(data_inner)
    return posts


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
    import concurrent.futures

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


