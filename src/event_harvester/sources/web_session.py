"""Browser session management — UC extension loading, login, Playwright state.

Cookie extraction was moved to ``uc_browser.chrome_cookies`` (the new
standalone package). Anything that previously imported
``get_chrome_cookies`` or ``cookies_to_playwright`` from here should
switch to::

    from uc_browser.chrome_cookies import get_chrome_cookies, cookies_to_playwright
"""

import logging
from pathlib import Path
from typing import Optional

# Re-export the cookie helpers from their new home so legacy imports keep
# working without a hard break. Prefer importing directly from
# uc_browser.chrome_cookies in new code.
from uc_browser.chrome_cookies import cookies_to_playwright, get_chrome_cookies

logger = logging.getLogger("event_harvester.web_session")

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DATA_DIR = _REPO_ROOT / "data"
_EXT_DIR = _REPO_ROOT / "ext" / "universal_controller" / "extension"
STATE_FILE = _DATA_DIR / ".playwright_state.json"


# ── UC Extension ─────────────────────────────────────────────────────


def extension_args() -> list[str]:
    """Return Chromium launch args to load the UC extension, or [] if not available."""
    if not _EXT_DIR.is_dir() or not (_EXT_DIR / "manifest.json").exists():
        return []
    ext_path = str(_EXT_DIR.resolve())
    return [
        f"--load-extension={ext_path}",
        f"--disable-extensions-except={ext_path}",
    ]


# ── UC Detection Helpers ─────────────────────────────────────────────


def wait_for_uc(page, timeout_ms: int = 5000) -> Optional[dict]:
    """Wait for UC extension, run detection, return window.__UC or None."""
    try:
        page.wait_for_function(
            "window.__UC && window.__UC.ready === true",
            timeout=timeout_ms,
        )
        page.evaluate("window.__UC_detectAll()")
        return page.evaluate("window.__UC")
    except Exception:
        return None


def apply_uc_patterns(page, uc: dict) -> None:
    """Use detected patterns to dismiss cookie banners, etc."""
    if not uc or not uc.get("patterns"):
        return
    cookies = uc["patterns"].get("cookie_consent", [])
    if cookies:
        try:
            dismissed = page.evaluate("window.__UC_dismiss()")
            if dismissed:
                logger.info("UC: dismissed cookie consent banner.")
                page.wait_for_timeout(500)
        except Exception:
            pass
    for ptype, hits in uc["patterns"].items():
        if hits:
            logger.info(
                "UC detected %s: %d candidate(s), best=%.2f",
                ptype, len(hits), hits[0].get("confidence", 0),
            )


# ── Interactive Login ────────────────────────────────────────────────


def web_login(urls: list[str] | None = None) -> None:
    """Open real Chrome for manual login with full password manager access.

    Launches the user's actual Chrome binary via subprocess (not Playwright-
    controlled) so Google trusts it for password sync and autofill. Connects
    via CDP to open tabs. Sessions persist in the real Chrome profile.
    """
    if urls is None:
        from event_harvester.sources.web_fetch import _load_web_sources

        urls = [s["url"] for s in _load_web_sources()]

    import concurrent.futures

    def _do_login():
        from playwright.sync_api import sync_playwright

        print("Opening browser. Log into your event sites, then close the browser.")
        print("Sessions persist across runs in data/.chrome_profile/\n")
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
                    *extension_args(),
                ],
            )

            for i, url in enumerate(urls):
                if i == 0 and context.pages:
                    page = context.pages[0]
                else:
                    page = context.new_page()
                try:
                    page.goto(url, timeout=30000, wait_until="domcontentloaded")
                except Exception:
                    pass

            print("  Browser opened. Navigate to sites, log in, then close the window.")
            try:
                context.pages[0].wait_for_event("close", timeout=600000)
            except Exception:
                pass

            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(STATE_FILE))
            print(f"\n  Session saved -> {STATE_FILE}")

            context.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(_do_login).result()
