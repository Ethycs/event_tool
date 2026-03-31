"""Fetch event pages using Playwright with saved session state.

First run: use --web-login to open a browser window, log into your
event sites, then close. The session is saved to data/.playwright_state.json.
Subsequent runs reuse the saved session automatically.
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("event_harvester.web_fetch")

_DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
_COOLDOWN_FILE = _DATA_DIR / ".web_cooldowns.json"
_DEFAULT_COOLDOWN = timedelta(hours=1)
_EXT_DIR = Path(__file__).resolve().parent.parent.parent.parent / "ext" / "uc_extension"

# Default event pages to fetch — override with WEB_EVENT_URLS env var
DEFAULT_EVENT_URLS = [
    {"url": "https://lu.ma/discover", "headless": True},
    {"url": "https://www.instagram.com/", "headless": True},
    {"url": "https://www.erobay.com/", "headless": False, "cooldown_hours": 4},
    {"url": "https://business.sfchamber.com/events/calendar", "headless": True},
]


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


def _get_chrome_cookies(domains: list[str] | None = None) -> list[dict]:
    """Extract cookies from Chrome using rookiepy.

    Chrome v130+ uses app-bound encryption requiring admin on Windows.
    Falls back to spawning an elevated subprocess if needed.
    """
    # Try direct extraction first
    try:
        import rookiepy
        if domains:
            cookies = rookiepy.chrome(domains)
        else:
            cookies = rookiepy.chrome()
        logger.info("Extracted %d cookies from Chrome.", len(cookies))
        return cookies
    except Exception as e:
        if "admin" not in str(e).lower() and "appbound" not in str(e).lower():
            logger.error("Failed to extract Chrome cookies: %s", e)
            return []

    # Chrome v130+ app-bound encryption — need elevated subprocess
    logger.info("Chrome requires admin for cookie decryption. Requesting elevation...")
    return _get_cookies_elevated(domains)


def _get_cookies_elevated(domains: list[str] | None = None) -> list[dict]:
    """Spawn an elevated Python subprocess to extract Chrome cookies.

    Shows a UAC prompt. The elevated process writes cookies to a temp file.
    """
    import json
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    # Write a small script that extracts cookies and dumps to JSON
    cookie_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="chrome_cookies_", delete=False,
    )
    cookie_file.close()

    domain_arg = json.dumps(domains) if domains else "null"
    script = f'''
import json, sys, os
# Signal that we started
with open(r"{cookie_file.name}", "w") as f:
    json.dump({{"status": "running"}}, f)
try:
    # Add pixi env to path so rookiepy is importable
    sys.path.insert(0, os.path.dirname(r"{sys.executable}") + r"\\..\\Lib\\site-packages")
    import rookiepy
    domains = {domain_arg}
    cookies = rookiepy.chrome(domains) if domains else rookiepy.chrome()
    with open(r"{cookie_file.name}", "w") as f:
        json.dump({{"status": "done", "cookies": cookies}}, f)
except Exception as e:
    with open(r"{cookie_file.name}", "w") as f:
        json.dump({{"status": "error", "error": str(e)}}, f)
'''

    script_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", prefix="extract_cookies_", delete=False,
    )
    script_file.write(script)
    script_file.close()

    try:
        if sys.platform == "win32":
            import ctypes
            import time

            python_exe = sys.executable
            # Use cmd /c with title so UAC shows "Event Harvester - Cookie Access"
            params = f'/c title Event Harvester - Cookie Access && "{python_exe}" "{script_file.name}"'
            print("  [UAC] Requesting admin access for Chrome cookie decryption...")
            ret = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", "cmd.exe", params, None, 1  # 1 = SW_SHOWNORMAL
            )
            if ret <= 32:
                logger.warning("UAC elevation was denied or failed (code %d).", ret)
                return []

            # Wait for the elevated process to write results
            for i in range(30):
                time.sleep(1)
                try:
                    data = json.loads(Path(cookie_file.name).read_text())
                    if data.get("status") == "done":
                        cookies = data.get("cookies", [])
                        logger.info("Elevated extraction: %d cookies.", len(cookies))
                        return cookies
                    elif data.get("status") == "error":
                        logger.error("Elevated extraction error: %s", data.get("error"))
                        return []
                except (json.JSONDecodeError, FileNotFoundError):
                    continue
            logger.warning("Elevated cookie extraction timed out.")
            return []
        else:
            # Non-Windows: try sudo
            result = subprocess.run(
                [sys.executable, script_file.name],
                capture_output=True, text=True, timeout=30,
            )
            cookies = json.loads(Path(cookie_file.name).read_text())
            logger.info("Extracted %d cookies.", len(cookies))
            return cookies

    except Exception as e:
        logger.error("Elevated cookie extraction failed: %s", e)
        return []
    finally:
        Path(script_file.name).unlink(missing_ok=True)
        Path(cookie_file.name).unlink(missing_ok=True)


def _cookies_to_playwright(cookies: list[dict]) -> list[dict]:
    """Convert rookiepy cookies to Playwright format."""
    pw_cookies = []
    for c in cookies:
        cookie = {
            "name": c.get("name", ""),
            "value": c.get("value", ""),
            "domain": c.get("domain", ""),
            "path": c.get("path", "/"),
        }
        if c.get("expires"):
            cookie["expires"] = c["expires"]
        if c.get("secure"):
            cookie["secure"] = True
        if c.get("httponly"):
            cookie["httpOnly"] = True
        # Playwright requires sameSite
        cookie["sameSite"] = "Lax"
        pw_cookies.append(cookie)
    return pw_cookies


_STATE_FILE = Path("data/.playwright_state.json")


def _extension_args() -> list[str]:
    """Return Chromium launch args to load the UC extension, or [] if not available."""
    if not _EXT_DIR.is_dir() or not (_EXT_DIR / "manifest.json").exists():
        return []
    ext_path = str(_EXT_DIR.resolve())
    return [
        f"--load-extension={ext_path}",
        f"--disable-extensions-except={ext_path}",
    ]


def _wait_for_uc(page, timeout_ms: int = 5000) -> Optional[dict]:
    """Wait for UC extension, run detection, return window.__UC or None.

    UC doesn't auto-detect — it needs an explicit detectAll() call.
    """
    try:
        page.wait_for_function(
            "window.__UC && window.__UC.ready === true",
            timeout=timeout_ms,
        )
        # Trigger static three-signal detection for all pattern types
        page.evaluate("window.__UC_detectAll()")
        return page.evaluate("window.__UC")
    except Exception:
        return None


def _apply_uc_patterns(page, uc: dict) -> None:
    """Use detected patterns to dismiss cookie banners, etc."""
    if not uc or not uc.get("patterns"):
        return
    # Auto-dismiss cookie consent
    cookies = uc["patterns"].get("cookie_consent", [])
    if cookies:
        try:
            dismissed = page.evaluate("window.__UC_dismiss()")
            if dismissed:
                logger.info("UC: dismissed cookie consent banner.")
                page.wait_for_timeout(500)
        except Exception:
            pass
    # Log what was detected
    for ptype, hits in uc["patterns"].items():
        if hits:
            logger.info("UC detected %s: %d candidate(s), best=%.2f",
                        ptype, len(hits), hits[0].get("confidence", 0))


def web_login(urls: list[str] | None = None) -> None:
    """Open a visible browser for manual login. Saves session state.

    Opens each URL in a browser window. Log into your accounts,
    then close the browser. The session cookies/storage are saved
    to data/.playwright_state.json for future headless runs.
    """
    if urls is None:
        urls = [u["url"] if isinstance(u, dict) else u for u in DEFAULT_EVENT_URLS]

    import concurrent.futures

    def _do_login():
        from playwright.sync_api import sync_playwright

        print("Opening browser. Log into your event sites, then close the browser.")
        print("Session will be saved for future runs.\n")
        print("  Suggested sites to log into:")
        for url in urls:
            print(f"    - {url}")
        print()

        user_data_dir = str(Path("data/.chrome_profile").resolve())

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir,
                headless=False,
                channel="chrome",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    *_extension_args(),
                ],
            )

            # Open each URL in its own tab
            for i, url in enumerate(urls):
                if i == 0 and context.pages:
                    page = context.pages[0]
                else:
                    page = context.new_page()
                try:
                    page.goto(url, timeout=30000, wait_until="domcontentloaded")
                except Exception:
                    pass  # timeout is fine, user can interact

            print("  Browser opened. Navigate to sites, log in, then close the window.")
            try:
                # Wait for any page to close (user closes browser)
                context.pages[0].wait_for_event("close", timeout=600000)  # 10 min
            except Exception:
                pass

            _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(_STATE_FILE))
            print(f"\n  Session saved -> {_STATE_FILE}")

            context.close()

    # Run in a thread to avoid conflict with asyncio event loop
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(_do_login).result()


# ── UC-driven perceptual harvest ──────────────────────────────────────

DEFAULT_UC_SOURCES = [
    {
        "url": "https://www.meetup.com/find/?source=EVENTS&eventType=inPerson&sortField=RELEVANCE",
        "query": "AI meetup",
        "scroll_seconds": 10,
        "name": "meetup",
    },
]


def uc_harvest(
    sources: list[dict] | None = None,
    max_events: int = 30,
) -> list[dict]:
    """Perceptually discover and harvest events from any website.

    Uses UC extension to detect page components (search bars, feeds,
    cookie banners, modals, login walls), interact with them, and
    extract event data — turning any event site into an API.

    Flow per source:
      1. Open → detectAll → clear obstacles (cookies, modals, login walls)
      2. If query: detect search → fill + submit → scan-diff the results
      3. Scroll to load more content
      4. Extract event links from page → fetch each detail page
      5. Fallback: use detected feed items as messages

    Source config keys:
        url:             page to open
        query:           search text (optional — skip search if absent)
        scroll_seconds:  how long to scroll (default 10)
        name:            source label for logging
    """
    if sources is None:
        sources = DEFAULT_UC_SOURCES
    if not sources:
        return []

    import concurrent.futures

    def _do_harvest():
        from event_harvester.sources.uc_browser import UCBrowser
        from urllib.parse import urlparse

        all_messages = []

        with UCBrowser(headless=False) as uc:
            for cfg in sources:
                url = cfg["url"]
                query = cfg.get("query")
                scroll_secs = cfg.get("scroll_seconds", 10)
                name = cfg.get("name", urlparse(url).netloc)
                messages = []

                logger.info("UC harvest: %s", name)

                # ── 1. Open + detect + clear obstacles ───────────
                page = uc.open(url)
                patterns = uc.detect_all(page)
                uc.dismiss_cookies(page)
                uc.close_modal(page)

                if uc.has_login_wall(page):
                    logger.warning("%s: login wall — skipping", name)
                    page.close()
                    continue

                # ── 2. Search if query provided ──────────────────
                if query:
                    uc.first_scan(page)
                    searched = uc.search(page, query)
                    if searched:
                        page.wait_for_timeout(3000)
                        diff = uc.next_scan(page)
                        detected = uc.auto_detect(page)
                        if diff:
                            logger.info(
                                "%s: search → %d changed, %d added, %d patterns",
                                name, diff.get("changed", 0),
                                diff.get("added", 0), len(detected),
                            )
                    else:
                        logger.info("%s: no search bar — using page as-is", name)

                # ── 3. Scroll to load more ───────────────────────
                for _ in range(scroll_secs):
                    page.evaluate("window.scrollBy(0, window.innerHeight)")
                    page.wait_for_timeout(1000)

                # ── 4. Score and extract event links ─────────────
                event_links = _extract_event_links(page)

                event_links = event_links[:max_events]

                if event_links:
                    logger.info("%s: %d event links, fetching details...", name, len(event_links))
                    for lnk in event_links:
                        text = _fetch_event_detail(
                            uc._context, uc._stealth_plugin,
                            lnk["url"], uc.timeout_ms,
                        )
                        if text:
                            domain = urlparse(lnk["url"]).netloc
                            messages.append({
                                "platform": "web",
                                "id": f"web:{hash(lnk['url'])}",
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "author": domain,
                                "channel": lnk["url"][:120],
                                "content": text[:5000],
                            })
                else:
                    # ── 5. Fallback: feed items as messages ──────
                    items = uc.get_feed_items(page)
                    domain = urlparse(url).netloc
                    for i, item_text in enumerate(items):
                        if len(item_text) > 30:
                            messages.append({
                                "platform": "web",
                                "id": f"web:{hash(url + str(i))}",
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "author": domain,
                                "channel": f"{name}-feed",
                                "content": item_text[:5000],
                            })

                logger.info("%s: %d messages harvested.", name, len(messages))
                all_messages.extend(messages)
                page.close()

        return all_messages

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        messages = pool.submit(_do_harvest).result()

    logger.info("UC harvest: %d message(s) from %d source(s).", len(messages), len(sources))
    return messages


# ── Feed scrolling with API interception ───────────────────────────────

# Feed configs: URL to visit, API pattern to intercept, scroll duration
DEFAULT_FEEDS = [
    {
        "url": "https://www.instagram.com/",
        "api_pattern": r"graphql",
        "scroll_seconds": 25,
        "name": "instagram",
    },
    {
        "url": "https://www.eventbrite.com/d/ca--san-francisco/events/",
        "api_pattern": r"api|search|events",
        "scroll_seconds": 15,
        "name": "eventbrite",
    },
]


def fetch_feeds(
    feeds: list[dict] | None = None,
    headless: bool = True,
) -> list[dict]:
    """Scroll through social feeds and intercept API responses.

    Opens each feed URL, scrolls for a set duration, and captures
    API responses containing post/event data.

    Args:
        feeds: list of feed configs (url, api_pattern, scroll_seconds, name)
        headless: run browser without visible window

    Returns:
        List of message dicts from intercepted API responses.
    """
    if feeds is None:
        env_feeds = os.getenv("WEB_FEED_URLS", "")
        if env_feeds:
            feeds = [{"url": u.strip(), "api_pattern": "graphql|api", "scroll_seconds": 30, "name": "web"}
                     for u in env_feeds.split(",") if u.strip()]
        else:
            feeds = DEFAULT_FEEDS

    if not feeds:
        return []

    import concurrent.futures

    def _do_scroll():
        import json as json_mod
        import time
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth

        stealth = Stealth()
        messages = []
        user_data_dir = str(Path("data/.chrome_profile").resolve())

        with sync_playwright() as p:
            # Use storage_state for headless, persistent profile for visible
            if headless:
                browser = p.chromium.launch(headless=True, channel="chrome")
                if _STATE_FILE.exists():
                    context = browser.new_context(storage_state=str(_STATE_FILE))
                else:
                    context = browser.new_context()
            else:
                context = p.chromium.launch_persistent_context(
                    user_data_dir,
                    headless=False,
                    channel="chrome",
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        *_extension_args(),
                    ],
                )
                browser = None

            for feed in feeds:
                url = feed["url"]
                api_pattern = re.compile(feed.get("api_pattern", "graphql|api"))
                scroll_secs = feed.get("scroll_seconds", 30)
                name = feed.get("name", "web")
                feed_responses: list[str] = []

                logger.info("Scrolling %s for %ds...", url, scroll_secs)

                page = context.new_page()
                stealth.apply_stealth_sync(page)

                # Intercept API responses as they load — capture full body
                def _on_response(response):
                    try:
                        if not api_pattern.search(response.url):
                            return
                        content_type = response.headers.get("content-type") or ""
                        if "json" not in content_type:
                            return
                        body = response.text()
                        if len(body) > 100:
                            feed_responses.append((response.url, body))
                    except Exception:
                        pass

                page.on("response", _on_response)

                try:
                    page.goto(url, timeout=20000, wait_until="domcontentloaded")
                    page.wait_for_timeout(3000)

                    # Check UC extension for detected patterns
                    uc = _wait_for_uc(page, timeout_ms=3000)
                    _apply_uc_patterns(page, uc)

                    # If UC found a feed container, scroll it specifically
                    feed_selector = None
                    if uc and uc.get("patterns", {}).get("feed"):
                        best_feed = uc["patterns"]["feed"][0]
                        feed_selector = best_feed.get("selector")
                        logger.info("UC: scrolling detected feed container: %s", feed_selector)

                    # Scroll rapidly
                    end_time = time.time() + scroll_secs
                    scroll_count = 0
                    while time.time() < end_time:
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
                        page.wait_for_timeout(800)  # fast scroll
                        scroll_count += 1

                    # Grab text — prefer UC feed text extraction
                    if feed_selector:
                        page_text = page.evaluate(
                            "(sel) => { const el = document.querySelector(sel); return el ? el.innerText : document.body.innerText; }",
                            feed_selector,
                        )
                    else:
                        page_text = page.evaluate("document.body.innerText")

                    logger.info(
                        "%s: scrolled %d times, captured %d API responses, page text %d chars.",
                        name, scroll_count, len(feed_responses), len(page_text or ""),
                    )

                    # Add page text as a message if substantial
                    if page_text and len(page_text) > 200:
                        messages.append({
                            "platform": name,
                            "id": f"feed:page:{hash(url)}",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "author": name,
                            "channel": url[:120],
                            "content": page_text[:5000],
                        })

                except Exception as e:
                    logger.error("Feed scroll failed for %s: %s", url, e)

                page.close()

                # Parse captured API responses — extract posts/events
                for resp_url, resp_body in feed_responses:
                    try:
                        data = json_mod.loads(resp_body)
                        # Extract individual posts from known feed structures
                        posts = _extract_posts_from_api(data, resp_url, name)
                        messages.extend(posts)
                    except json_mod.JSONDecodeError:
                        pass

            context.close()
            if browser:
                browser.close()

        return messages

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        messages = pool.submit(_do_scroll).result()

    logger.info("Feed fetch: %d response(s) captured.", len(messages))
    return messages


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


def _extract_event_links(page) -> list[dict]:
    """Extract event links by associating dates with links via DOM locality.

    Treats the DOM as a graph. For each date/time text node on the page,
    walks up to find the containing "card" (nearest ancestor with siblings),
    then finds the primary link in that same card. The date and link are
    associated because they share a common ancestor subtree.

    Returns list of {url, text, date_hint} — links co-located with dates.
    Falls back to primary links from repeated-child containers if no
    date associations are found.
    """
    raw = page.evaluate("""() => {
        const DATE_RE = /(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\\.?\\s+\\d{1,2}(?:st|nd|rd|th)?|\\d{1,2}(?:st|nd|rd|th)?\\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*|\\d{4}-\\d{2}-\\d{2}|(?:mon|tue|wed|thu|fri|sat|sun)(?:day)?(?:\\s*,)?\\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\\.?\\s+\\d{1,2}/i;
        const TIME_RE = /\\b\\d{1,2}(?::\\d{2})?\\s*(?:am|pm)\\b|\\b(?:[01]\\d|2[0-3]):[0-5]\\d\\b/i;
        const domain = location.hostname;

        // ── Step 1: Walk the DOM tree to find all text nodes with dates ──
        const dateNodes = [];
        const walker = document.createTreeWalker(
            document.body, NodeFilter.SHOW_TEXT, null
        );
        while (walker.nextNode()) {
            const text = walker.currentNode.textContent.trim();
            if (text.length > 3 && text.length < 200 && DATE_RE.test(text)) {
                dateNodes.push({
                    node: walker.currentNode,
                    text: text,
                    dateMatch: text.match(DATE_RE)?.[0] || '',
                    hasTime: TIME_RE.test(text),
                });
            }
        }

        // ── Step 2: For each date node, walk up to find its "card" ──────
        // A card is the nearest ancestor that has siblings of the same type
        // (indicating it's an item in a repeated list/grid)
        function findCard(node) {
            let cur = node.parentElement;
            let bestCard = null;
            while (cur && cur !== document.body) {
                const parent = cur.parentElement;
                if (parent) {
                    const cn = typeof cur.className === 'string' ? cur.className : '';
                    const tag = cur.tagName + '.' + cn.split(' ')[0];
                    let sibCount = 0;
                    for (const sib of parent.children) {
                        const scn = typeof sib.className === 'string' ? sib.className : '';
                        if (sib.tagName + '.' + scn.split(' ')[0] === tag) sibCount++;
                    }
                    if (sibCount >= 2) {
                        bestCard = cur;
                        // Keep walking if this card has no links (too narrow)
                        if (cur.querySelectorAll('a[href]').length > 0) return cur;
                    }
                }
                cur = cur.parentElement;
            }
            return bestCard;
        }

        // ── Step 3: From each card, find the primary link ───────────────
        const results = [];
        const seenUrls = new Set();
        const seenCards = new Set();

        for (const dn of dateNodes) {
            const card = findCard(dn.node);
            if (!card || seenCards.has(card)) continue;
            seenCards.add(card);

            // Find the best link in this card (longest text = likely title)
            const links = card.querySelectorAll('a[href]');
            let bestLink = null;
            let bestLen = 0;
            for (const a of links) {
                const href = a.href;
                if (!href || seenUrls.has(href)) continue;
                try { if (new URL(href).hostname !== domain) continue; } catch { continue; }
                const linkText = a.innerText.trim();
                if (linkText.length > bestLen) {
                    bestLink = a;
                    bestLen = linkText.length;
                }
            }

            // Accept link with text, or image-wrapped link (empty text but long slug)
            if (bestLink && bestLen >= 5) {
                seenUrls.add(bestLink.href);
                results.push({
                    url: bestLink.href,
                    text: bestLink.innerText.trim().substring(0, 200) || card.innerText.trim().substring(0, 200),
                    date_hint: dn.dateMatch,
                });
            } else if (bestLink && bestLen < 5) {
                // Image-wrapped link — use card text as the title
                const cardTitle = card.innerText.trim().split('\\n').filter(l => l.length > 5)[0] || '';
                if (cardTitle.length >= 5) {
                    seenUrls.add(bestLink.href);
                    results.push({
                        url: bestLink.href,
                        text: cardTitle.substring(0, 200),
                        date_hint: dn.dateMatch,
                    });
                }
            }
        }

        // ── Step 4: Fallback — if few date associations, try repeated ───
        // containers and extract primary links from each child item
        if (results.length < 3) {
            const containers = [];
            document.querySelectorAll('main, [role="main"], section, div, ul, ol, table, tbody').forEach(el => {
                if (el.children.length < 3) return;
                const tags = {};
                for (const c of el.children) {
                    const cn = typeof c.className === 'string' ? c.className : '';
                    tags[c.tagName + '.' + cn.split(' ')[0]] = (tags[c.tagName + '.' + cn.split(' ')[0]] || 0) + 1;
                }
                const max = Math.max(...Object.values(tags));
                if (max >= 3) containers.push({ el, count: max });
            });
            containers.sort((a, b) => b.count - a.count);

            // Use top container, skip if it's a parent of an already-used one
            for (const { el: cont } of containers.slice(0, 2)) {
                for (const item of cont.children) {
                    const links = item.querySelectorAll('a[href]');
                    let best = null, bestLen = 0;
                    for (const a of links) {
                        if (!a.href || seenUrls.has(a.href)) continue;
                        try { if (new URL(a.href).hostname !== domain) continue; } catch { continue; }
                        const t = a.innerText.trim();
                        if (t.length > bestLen) { best = a; bestLen = t.length; }
                    }
                    if (best && bestLen >= 5) {
                        seenUrls.add(best.href);
                        // Check if this item has a date too
                        const itemText = item.innerText || '';
                        const dateMatch = itemText.match(DATE_RE);
                        results.push({
                            url: best.href,
                            text: best.innerText.trim().substring(0, 200),
                            date_hint: dateMatch ? dateMatch[0] : '',
                        });
                    }
                }
            }
        }

        // ── Step 5: Last resort — links near dates (walk up 5 ancestors) ──
        if (results.length < 3) {
            const allLinks = document.querySelectorAll('a[href]');
            for (const a of allLinks) {
                if (seenUrls.has(a.href)) continue;
                const text = a.innerText.trim();
                if (text.length < 5) continue;
                try { if (new URL(a.href).hostname !== domain) continue; } catch { continue; }
                if (/^(home|about|contact|login|sign|help|privacy|terms|\\d{1,2})$/i.test(text)) continue;

                // Walk up 5 ancestors looking for date context
                let dateMatch = text.match(DATE_RE)?.[0] || '';
                if (!dateMatch) {
                    let cur = a.parentElement;
                    for (let depth = 0; cur && depth < 5; depth++, cur = cur.parentElement) {
                        const ct = (cur.childNodes.length <= 5)
                            ? (cur.innerText || '').substring(0, 300)
                            : '';
                        const m = ct.match(DATE_RE);
                        if (m) { dateMatch = m[0]; break; }
                    }
                }

                // Accept if date found nearby, or if the URL pattern suggests event detail
                const urlPath = new URL(a.href).pathname;
                const looksLikeDetail = /\\/\\w{5,}.*\\d/.test(urlPath) && urlPath.split('/').length >= 3;
                if (!dateMatch && !looksLikeDetail) continue;

                seenUrls.add(a.href);
                results.push({
                    url: a.href,
                    text: text.substring(0, 200),
                    date_hint: dateMatch,
                });
            }
        }

        return results;
    }""")

    return raw or []


def _fetch_event_detail(context, stealth, url: str, timeout_ms: int) -> str | None:
    """Fetch a single event detail page and return its text."""
    try:
        page = context.new_page()
        stealth.apply_stealth_sync(page)
        page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        html = page.content()
        page.close()
        text = _extract_text(html)
        return text if len(text) > 50 else None
    except Exception:
        return None


def _try_calendar_extraction(
    page, context, stealth, url: str, domain: str, timeout_ms: int,
) -> list[dict] | None:
    """Detect calendar pages and extract events via their detail links.

    Returns list of message dicts (one per event), or None if not a calendar.
    """
    event_links = _extract_event_links(page)

    if len(event_links) < 3:
        return None

    logger.info(
        "Calendar detected on %s: %d event links found. Fetching details...",
        domain, len(event_links),
    )

    messages = []
    for lnk in event_links:
        text = _fetch_event_detail(context, stealth, lnk["url"], timeout_ms)
        if not text:
            continue
        messages.append({
            "platform": "web",
            "id": f"web:{hash(lnk['url'])}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "author": domain,
            "channel": lnk["url"][:120],
            "content": text[:5000],
        })

    logger.info("Calendar: fetched %d/%d event detail pages.", len(messages), len(event_links))
    return messages if messages else None


def fetch_event_pages(
    urls: list[dict | str] | None = None,
    timeout_ms: int = 30000,
) -> list[dict]:
    """Fetch event pages using Playwright.

    URLs can be dicts ``{"url": ..., "headless": bool, "cooldown_hours": N}``
    or plain strings (default headless=True, cooldown=1h).
    Headless and non-headless URLs run in parallel browser instances.
    URLs on cooldown are skipped.
    """
    if urls is None:
        env_urls = os.getenv("WEB_EVENT_URLS", "")
        if env_urls:
            urls = [{"url": u.strip(), "headless": True}
                    for u in env_urls.split(",") if u.strip()]
        else:
            urls = list(DEFAULT_EVENT_URLS)

    if not urls:
        return []

    # Normalise plain strings to dicts
    normalised = []
    for u in urls:
        if isinstance(u, str):
            normalised.append({"url": u, "headless": True})
        else:
            normalised.append(u)

    # Filter out URLs on cooldown (cooldown_hours=-1 means no cooldown)
    active = []
    for u in normalised:
        cd_hours = u.get("cooldown_hours", -1)
        if cd_hours >= 0 and _is_on_cooldown(u["url"], timedelta(hours=cd_hours)):
            logger.info("Skipping %s (on cooldown).", u["url"])
        else:
            active.append(u)

    if not active:
        return []

    # Split into headless and non-headless groups
    headless_urls = [u["url"] for u in active if u.get("headless", True)]
    visible_urls = [u["url"] for u in active if not u.get("headless", True)]

    import concurrent.futures

    def _fetch_group(group_urls: list[str], headless: bool) -> list[dict]:
        from urllib.parse import urlparse
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth

        stealth = Stealth()
        messages = []
        user_data_dir = str(Path("data/.chrome_profile").resolve())

        with sync_playwright() as p:
            if headless:
                browser = p.chromium.launch(headless=True, channel="chrome")
                if _STATE_FILE.exists():
                    context = browser.new_context(storage_state=str(_STATE_FILE))
                else:
                    context = browser.new_context()
            else:
                context = p.chromium.launch_persistent_context(
                    user_data_dir,
                    headless=False,
                    channel="chrome",
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        *_extension_args(),
                    ],
                )
                browser = None

            for url in group_urls:
                logger.info("Fetching: %s", url)
                try:
                    page = context.new_page()
                    stealth.apply_stealth_sync(page)
                    page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                    page.wait_for_timeout(2000)

                    # Wait for Cloudflare challenge to resolve
                    html = page.content()
                    if "just a moment" in html.lower() or "security verification" in html.lower():
                        if headless:
                            logger.warning("Cloudflare on %s (headless). Skipping.", url)
                            page.close()
                            continue
                        print(f"  Cloudflare captcha on {url} — click it in the browser...")
                        for _ in range(90):
                            page.wait_for_timeout(1000)
                            try:
                                html = page.content()
                            except Exception:
                                continue
                            if "just a moment" not in html.lower():
                                page.wait_for_timeout(3000)
                                html = page.content()
                                break

                    domain = urlparse(url).netloc

                    # Check UC extension for detected patterns
                    uc = _wait_for_uc(page, timeout_ms=3000)
                    _apply_uc_patterns(page, uc)

                    # Try calendar link extraction first
                    cal_msgs = _try_calendar_extraction(
                        page, context, stealth, url, domain, timeout_ms,
                    )
                    if cal_msgs:
                        messages.extend(cal_msgs)
                        page.close()
                        _save_cooldown(url)
                        continue

                    # Prefer UC feed extraction if available
                    if uc and uc.get("patterns", {}).get("feed"):
                        try:
                            text = page.evaluate("window.__UC_getVisibleText()")
                        except Exception:
                            text = _extract_text(html)
                    else:
                        text = _extract_text(html)

                    if len(text) < 50:
                        page.close()
                        continue

                    messages.append({
                        "platform": "web",
                        "id": f"web:{hash(url)}",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "author": domain,
                        "channel": url[:120],
                        "content": text[:8000],
                    })
                    _save_cooldown(url)
                    page.close()

                except Exception as e:
                    logger.error("Failed to fetch %s: %s", url, e)

            context.close()
            if browser:
                browser.close()

        return messages

    # Run headless and visible groups in parallel threads
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = []
        if headless_urls:
            futures.append(pool.submit(_fetch_group, headless_urls, True))
        if visible_urls:
            futures.append(pool.submit(_fetch_group, visible_urls, False))

        messages = []
        for f in concurrent.futures.as_completed(futures):
            messages.extend(f.result())

    logger.info("Web fetch: %d page(s) fetched.", len(messages))
    return messages
