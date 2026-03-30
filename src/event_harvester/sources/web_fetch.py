"""Fetch event pages using Playwright with saved session state.

First run: use --web-login to open a browser window, log into your
event sites, then close. The session is saved to data/.playwright_state.json.
Subsequent runs reuse the saved session automatically.
"""

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("event_harvester.web_fetch")

# Default event pages to fetch — override with WEB_EVENT_URLS env var
DEFAULT_EVENT_URLS = [
    "https://lu.ma/discover",
    "https://www.meetup.com/find/?source=EVENTS&eventType=inPerson&sortField=RELEVANCE",
]


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


def web_login(urls: list[str] | None = None) -> None:
    """Open a visible browser for manual login. Saves session state.

    Opens each URL in a browser window. Log into your accounts,
    then close the browser. The session cookies/storage are saved
    to data/.playwright_state.json for future headless runs.
    """
    if urls is None:
        urls = DEFAULT_EVENT_URLS

    import concurrent.futures

    def _do_login():
        from playwright.sync_api import sync_playwright

        print("Opening browser for login. Log into your accounts, then close the browser.")
        print("Session will be saved for future runs.\n")

        # Use persistent context with a dedicated user data dir.
        # This avoids automation detection flags that trigger
        # "This browser or app may not be secure" on Google/etc.
        user_data_dir = str(Path("data/.chrome_profile").resolve())

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir,
                headless=False,
                channel="chrome",
                args=[
                    "--disable-blink-features=AutomationControlled",
                ],
            )

            for url in urls:
                page = context.new_page()
                page.goto(url)
                print(f"  Opened: {url}")

            print("\n  Log in to your accounts, then close the browser window.")
            try:
                context.pages[0].wait_for_event("close", timeout=300000)
            except Exception:
                pass

            _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(_STATE_FILE))
            print(f"\n  Session saved -> {_STATE_FILE}")

            context.close()

    # Run in a thread to avoid conflict with asyncio event loop
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(_do_login).result()


def _extract_text(html: str) -> str:
    """Strip HTML tags and return readable text."""
    # Remove script and style blocks
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html).strip()


def fetch_event_pages(
    urls: list[str] | None = None,
    headless: bool = True,
    timeout_ms: int = 15000,
) -> list[dict]:
    """Fetch event pages using saved Playwright session state.

    Args:
        urls: list of event page URLs to fetch. Defaults to DEFAULT_EVENT_URLS.
        headless: run browser without visible window
        timeout_ms: page load timeout in milliseconds

    Returns:
        List of message dicts in standard format, one per page.
    """
    if urls is None:
        env_urls = os.getenv("WEB_EVENT_URLS", "")
        if env_urls:
            urls = [u.strip() for u in env_urls.split(",") if u.strip()]
        else:
            urls = DEFAULT_EVENT_URLS

    if not urls:
        return []

    import concurrent.futures

    def _do_fetch():
        from urllib.parse import urlparse
        from playwright.sync_api import sync_playwright

        messages = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless, channel="chrome")

            # Use saved state if available, otherwise plain context
            if _STATE_FILE.exists():
                context = browser.new_context(storage_state=str(_STATE_FILE))
                logger.info("Using saved session state for web fetch.")
            else:
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                )
                logger.info("No saved session. Run --web-login first for authenticated access.")

            for url in urls:
                logger.info("Fetching: %s", url)
                try:
                    page = context.new_page()
                    page.goto(url, timeout=timeout_ms, wait_until="networkidle")
                    page.wait_for_timeout(2000)

                    html = page.content()
                    text = _extract_text(html)

                    if len(text) < 50:
                        page.close()
                        continue

                    domain = urlparse(url).netloc
                    messages.append({
                        "platform": "web",
                        "id": f"web:{hash(url)}",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "author": domain,
                        "channel": url[:120],
                        "content": text[:5000],
                    })
                    page.close()

                except Exception as e:
                    logger.error("Failed to fetch %s: %s", url, e)

            browser.close()

        return messages

    # Run in thread to avoid asyncio conflict
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        messages = pool.submit(_do_fetch).result()

    logger.info("Web fetch: %d page(s) fetched.", len(messages))
    return messages
