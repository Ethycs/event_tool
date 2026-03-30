"""Programmatic browser API powered by Universal Controller + Playwright.

Provides a high-level Python interface for interacting with any website
using auto-detected UI patterns. The UC Chrome extension detects search bars,
feeds, forms, modals, cookie banners, and login walls — this module wraps
those detections into Playwright actions.

Usage::

    from event_harvester.sources.uc_browser import UCBrowser

    with UCBrowser() as browser:
        page = browser.open("https://lu.ma/discover")
        browser.search(page, "AI meetup San Francisco")
        results = browser.get_feed_text(page)
        print(results)
"""

import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("event_harvester.uc_browser")

_EXT_DIR = Path(__file__).resolve().parent.parent.parent.parent / "ext" / "uc_extension"
_CHROME_PROFILE = Path("data/.chrome_profile").resolve()
_STATE_FILE = Path("data/.playwright_state.json")


class UCBrowser:
    """High-level browser automation using UC pattern detection + Playwright.

    Manages browser lifecycle and provides methods to interact with
    auto-detected UI patterns on any website.
    """

    def __init__(
        self,
        headless: bool = False,
        channel: str = "chrome",
        stealth: bool = True,
        timeout_ms: int = 30000,
    ):
        self.headless = headless
        self.channel = channel
        self.use_stealth = stealth
        self.timeout_ms = timeout_ms
        self._pw = None
        self._context = None
        self._browser = None
        self._stealth_plugin = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    def start(self) -> None:
        """Launch the browser with the UC extension loaded."""
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()

        ext_args = []
        if not self.headless and _EXT_DIR.is_dir() and (_EXT_DIR / "manifest.json").exists():
            ext_path = str(_EXT_DIR.resolve())
            ext_args = [
                f"--load-extension={ext_path}",
                f"--disable-extensions-except={ext_path}",
            ]
            logger.info("Loading UC extension from %s", ext_path)
        elif not self.headless:
            logger.warning("UC extension not found at %s — running without it", _EXT_DIR)

        if self.headless:
            # Extensions don't work in headless — use plain browser
            self._browser = self._pw.chromium.launch(
                headless=True, channel=self.channel,
            )
            if _STATE_FILE.exists():
                self._context = self._browser.new_context(
                    storage_state=str(_STATE_FILE),
                )
            else:
                self._context = self._browser.new_context()
        else:
            _CHROME_PROFILE.parent.mkdir(parents=True, exist_ok=True)
            self._context = self._pw.chromium.launch_persistent_context(
                str(_CHROME_PROFILE),
                headless=False,
                channel=self.channel,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    *ext_args,
                ],
            )

        if self.use_stealth:
            try:
                from playwright_stealth import Stealth
                self._stealth_plugin = Stealth()
            except ImportError:
                logger.warning("playwright-stealth not installed, skipping stealth")

    def close(self) -> None:
        """Shut down browser and Playwright."""
        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._context = self._browser = self._pw = None

    # ── Page management ─────────────────────────────────────────────────

    def open(self, url: str, wait_ms: int = 2000) -> "Page":
        """Open a URL in a new tab, wait for load, return the Playwright Page."""
        page = self._context.new_page()
        if self._stealth_plugin:
            self._stealth_plugin.apply_stealth_sync(page)
        page.goto(url, timeout=self.timeout_ms, wait_until="domcontentloaded")
        page.wait_for_timeout(wait_ms)
        return page

    def detect(self, page, timeout_ms: int = 5000) -> Optional[dict]:
        """Wait for UC detection and return the patterns dict, or None."""
        try:
            page.wait_for_function(
                "window.__UC && window.__UC.ready === true",
                timeout=timeout_ms,
            )
            uc = page.evaluate("window.__UC")
            # Log detections
            if uc and uc.get("patterns"):
                for ptype, hits in uc["patterns"].items():
                    if hits:
                        logger.info(
                            "UC detected %s: %d hit(s), best=%.2f",
                            ptype, len(hits), hits[0].get("confidence", 0),
                        )
            return uc
        except Exception:
            logger.debug("UC detection timed out on %s", page.url)
            return None

    def rescan(self, page) -> Optional[dict]:
        """Re-run UC detection (useful after SPA navigation or DOM changes)."""
        try:
            return page.evaluate("window.__UC_rescan()")
        except Exception:
            return None

    # ── Pattern-driven actions ──────────────────────────────────────────

    def dismiss_cookies(self, page) -> bool:
        """Click the detected cookie consent accept button."""
        try:
            return page.evaluate("window.__UC_dismiss()") is True
        except Exception:
            return False

    def search(self, page, query: str, submit: bool = True) -> bool:
        """Type a query into the detected search bar.

        Args:
            page: Playwright page.
            query: The search text.
            submit: If True, press Enter after filling.

        Returns:
            True if a search bar was found and filled.
        """
        uc = self.detect(page)
        if not uc or not uc.get("patterns", {}).get("search"):
            logger.warning("No search bar detected on %s", page.url)
            return False

        best = uc["patterns"]["search"][0]
        selector = best["selector"]
        logger.info("Filling search bar: %s (confidence=%.2f)", selector, best["confidence"])

        try:
            # Use Playwright's fill for reliable input (handles React, etc.)
            page.fill(selector, query)
            if submit:
                page.press(selector, "Enter")
                page.wait_for_timeout(2000)
            return True
        except Exception as e:
            # Fallback to the JS-based approach
            logger.debug("Playwright fill failed (%s), trying JS approach", e)
            try:
                filled = page.evaluate(
                    "(q) => window.__UC_fillSearch(q)", query,
                )
                if filled and submit:
                    page.press(selector, "Enter")
                    page.wait_for_timeout(2000)
                return filled
            except Exception:
                return False

    def get_feed_text(self, page) -> str:
        """Extract text from the detected feed container, or full page text."""
        try:
            return page.evaluate("window.__UC_getVisibleText()") or ""
        except Exception:
            return page.evaluate("document.body.innerText") or ""

    def get_feed_items(self, page) -> list[str]:
        """Extract individual feed item texts using the detected item selector."""
        uc = self.detect(page)
        if not uc or not uc.get("patterns", {}).get("feed"):
            return [page.evaluate("document.body.innerText") or ""]

        best = uc["patterns"]["feed"][0]
        item_sel = best.get("item_selector")
        if not item_sel:
            return [self.get_feed_text(page)]

        try:
            return page.evaluate(
                """(sel) => {
                    const items = document.querySelectorAll(sel);
                    return Array.from(items).map(el => el.innerText.trim()).filter(t => t.length > 10);
                }""",
                item_sel,
            )
        except Exception:
            return [self.get_feed_text(page)]

    def scroll_feed(
        self, page, seconds: int = 15, on_item: callable = None,
    ) -> list[str]:
        """Scroll the detected feed container, collecting item texts.

        Args:
            page: Playwright page.
            seconds: How long to scroll.
            on_item: Optional callback called with each new item text.

        Returns:
            List of item texts collected during scrolling.
        """
        uc = self.detect(page)
        feed_selector = None
        if uc and uc.get("patterns", {}).get("feed"):
            feed_selector = uc["patterns"]["feed"][0].get("selector")

        seen_texts = set()
        all_items = []
        end_time = time.time() + seconds

        while time.time() < end_time:
            # Scroll
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

            # Collect new items
            current = self.get_feed_items(page)
            for text in current:
                if text not in seen_texts:
                    seen_texts.add(text)
                    all_items.append(text)
                    if on_item:
                        on_item(text)

        logger.info("Scroll complete: %d items collected in %ds", len(all_items), seconds)
        return all_items

    def fill_form(self, page, fields: dict[str, str]) -> bool:
        """Fill a detected form with field values.

        Args:
            page: Playwright page.
            fields: Mapping of field name/label → value.
                    Keys are matched against input name, placeholder,
                    aria-label, or type (best effort).

        Returns:
            True if at least one field was filled.
        """
        uc = self.detect(page)
        if not uc or not uc.get("patterns", {}).get("form"):
            logger.warning("No form detected on %s", page.url)
            return False

        best = uc["patterns"]["form"][0]
        form_fields = best.get("fields", [])
        filled_any = False

        for key, value in fields.items():
            key_lower = key.lower()
            # Find matching field
            matched = None
            for f in form_fields:
                if (
                    key_lower in (f.get("name") or "").lower()
                    or key_lower in (f.get("type") or "").lower()
                    or key_lower == f.get("name", "").lower()
                ):
                    matched = f
                    break

            if matched:
                try:
                    page.fill(matched["selector"], value)
                    filled_any = True
                except Exception as e:
                    logger.debug("Failed to fill field %s: %s", key, e)

        return filled_any

    def submit_form(self, page) -> bool:
        """Click the submit button on the detected form."""
        uc = self.detect(page)
        if not uc or not uc.get("patterns", {}).get("form"):
            return False

        best = uc["patterns"]["form"][0]
        try:
            # Try to find a submit button within the form
            form_sel = best["selector"]
            btn = page.query_selector(f"{form_sel} button[type='submit'], {form_sel} button, {form_sel} [type='submit']")
            if btn:
                btn.click()
                page.wait_for_timeout(2000)
                return True
        except Exception:
            pass
        return False

    def close_modal(self, page) -> bool:
        """Dismiss the detected modal/dialog."""
        uc = self.detect(page)
        if not uc or not uc.get("patterns", {}).get("modal"):
            return False

        best = uc["patterns"]["modal"][0]
        dismiss_sel = best.get("dismiss_selector")
        if dismiss_sel:
            try:
                page.click(dismiss_sel)
                page.wait_for_timeout(500)
                return True
            except Exception:
                pass
        return False

    def has_login_wall(self, page) -> bool:
        """Check if the page has a blocking login wall."""
        uc = self.detect(page)
        if not uc or not uc.get("patterns", {}).get("login_wall"):
            return False
        return any(lw.get("blocking") for lw in uc["patterns"]["login_wall"])

    def get_patterns(self, page) -> dict:
        """Return all detected patterns as a dict."""
        uc = self.detect(page)
        if not uc:
            return {}
        return uc.get("patterns", {})

    # ── Convenience: open + detect + act ────────────────────────────────

    def navigate_and_search(self, url: str, query: str) -> tuple:
        """Open a URL, handle obstacles, search, and return (page, results_text).

        This is the high-level "do everything" method:
        1. Opens the URL
        2. Detects patterns
        3. Dismisses cookie banners
        4. Closes blocking modals
        5. Fills the search bar and submits
        6. Returns the page and visible results text
        """
        page = self.open(url)
        uc = self.detect(page)

        if uc:
            self.dismiss_cookies(page)
            self.close_modal(page)

            if self.has_login_wall(page):
                logger.warning("Login wall detected on %s", url)

        self.search(page, query)
        # Rescan after search results load
        self.rescan(page)
        text = self.get_feed_text(page)
        return page, text

    def navigate_and_scrape(self, url: str, scroll_seconds: int = 15) -> tuple:
        """Open a URL, handle obstacles, scroll the feed, return (page, items).

        1. Opens the URL
        2. Detects patterns
        3. Dismisses cookie banners / modals
        4. Scrolls the feed for the given duration
        5. Returns the page and list of item texts
        """
        page = self.open(url)
        uc = self.detect(page)

        if uc:
            self.dismiss_cookies(page)
            self.close_modal(page)

        items = self.scroll_feed(page, seconds=scroll_seconds)
        return page, items
