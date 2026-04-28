"""Browser session management — cookies, login, Playwright state.

Handles Chrome cookie extraction (including UAC elevation on Windows),
cookie format conversion, UC extension loading, and interactive login
for saving session state.
"""

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("event_harvester.web_session")

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DATA_DIR = _REPO_ROOT / "data"
_EXT_DIR = _REPO_ROOT / "ext" / "universal_controller" / "extension"
STATE_FILE = _DATA_DIR / ".playwright_state.json"


# ── Cookie extraction ────────────────────────────────────────────────


def get_chrome_cookies(domains: list[str] | None = None) -> list[dict]:
    """Extract cookies from Chrome using rookiepy.

    Chrome v130+ uses app-bound encryption requiring admin on Windows.
    Falls back to spawning an elevated subprocess if needed.
    """
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

    logger.info("Chrome requires admin for cookie decryption. Requesting elevation...")
    return _get_cookies_elevated(domains)


def _get_cookies_elevated(domains: list[str] | None = None) -> list[dict]:
    """Spawn an elevated Python subprocess to extract Chrome cookies.

    Shows a UAC prompt. The elevated process writes cookies to a temp file.
    """
    import subprocess
    import sys
    import tempfile

    cookie_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="chrome_cookies_", delete=False,
    )
    cookie_file.close()

    domain_arg = json.dumps(domains) if domains else "null"
    script = f'''
import json, sys, os
with open(r"{cookie_file.name}", "w") as f:
    json.dump({{"status": "running"}}, f)
try:
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
            params = (
                f'/c title Event Harvester - Cookie Access '
                f'&& "{python_exe}" "{script_file.name}"'
            )
            print("  [UAC] Requesting admin access for Chrome cookie decryption...")
            ret = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", "cmd.exe", params, None, 1,
            )
            if ret <= 32:
                logger.warning("UAC elevation was denied or failed (code %d).", ret)
                return []

            for i in range(30):
                time.sleep(1)
                try:
                    data = json.loads(Path(cookie_file.name).read_text())
                    if data.get("status") == "done":
                        cookies = data.get("cookies", [])
                        logger.info("Elevated extraction: %d cookies.", len(cookies))
                        return cookies
                    elif data.get("status") == "error":
                        logger.error(
                            "Elevated extraction error: %s", data.get("error"),
                        )
                        return []
                except (json.JSONDecodeError, FileNotFoundError):
                    continue
            logger.warning("Elevated cookie extraction timed out.")
            return []
        else:
            subprocess.run(
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


def cookies_to_playwright(cookies: list[dict]) -> list[dict]:
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
        cookie["sameSite"] = "Lax"
        pw_cookies.append(cookie)
    return pw_cookies


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
