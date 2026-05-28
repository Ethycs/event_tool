"""Grok (grok.com) automation client.

Builds on UCBrowser primitives to expose a full-lifecycle API for a Grok
account: send, read, list, delete, rename, archive, regenerate, stop,
get_models, switch_model, new_chat.

Designed to be held as a long-lived singleton inside a process (e.g. the
MCP server) so the browser stays open across calls.

Auth:
    Relies on the persistent profile at ``data/.uc_chromium_profile/`` and
    saved cookies in ``data/.playwright_state.json``. If Grok shows a
    login wall on first call, raise ``GrokAuthRequired`` with a hint.

Resilience:
    Recipes use anchor-based selectors (role, aria-label, href patterns)
    rather than fragile class names. The ``_SELECTORS`` dict at the top
    of the module is the single point of tuning if Grok's DOM drifts.
"""

from __future__ import annotations

import atexit
import logging
import re
import threading
import time
from typing import Optional
from urllib.parse import urlparse

from uc_browser import BrowserMode, UCBrowser

logger = logging.getLogger("event_harvester.sites.grok")

_RECIPE_VERSION = 2  # bump when DOM recipes change

# Anchor selectors verified against the live DOM. The chat-input/message
# anchors are stable test-ids; the menu/picker selectors are fuzzy and
# may need tuning when Grok's DOM drifts.
_SELECTORS: dict[str, str] = {
    # ── Core chat surface (DOM-anchored, verified) ───────────────────
    "chat_input": "div.ProseMirror",
    "user_message": 'div[data-testid="user-message"]',
    "assistant_message": 'div[data-testid="assistant-message"]',
    # ── Sidebar ──────────────────────────────────────────────────────
    "conversation_link": 'a[href*="/c/"]',
    # 3-dot / "more options" button on a conversation row in the sidebar
    "row_more_button": (
        'button[aria-label*="more" i], '
        'button[aria-label*="options" i], '
        'button[aria-haspopup="menu"], '
        'button[aria-haspopup="true"]'
    ),
    # Menu items inside an opened popover
    "menu_item": '[role="menuitem"], button',
    # Stop-generation button (shown while assistant is streaming)
    "stop_button": (
        '[data-testid*="stop" i], '
        'button[aria-label*="stop" i]'
    ),
    # Regenerate button at the bottom of an assistant message
    "regenerate_button": (
        'button[aria-label*="regenerate" i], '
        'button[aria-label*="try again" i]'
    ),
    # Model picker dropdown trigger (top header)
    "model_picker": (
        'button[aria-label*="model" i], '
        '[data-testid*="model" i] button, '
        'button[aria-haspopup="listbox"]'
    ),
    # New-chat button in the sidebar
    "new_chat_button": (
        'a[href="/"], '
        '[data-testid*="new-chat" i], '
        'button[aria-label*="new chat" i]'
    ),
    # Rename input field inside the rename modal/inline editor
    "rename_input": 'input[type="text"], textarea',
}


# ── Exceptions ───────────────────────────────────────────────────────


class GrokError(RuntimeError):
    """Base class for Grok-specific errors."""


class GrokAuthRequired(GrokError):
    """Login wall detected — user needs to run web-login first."""


# ── Helpers ──────────────────────────────────────────────────────────


_UUID_PATTERN = (
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)
_PATH_UUID_RE = re.compile(rf"/c/{_UUID_PATTERN}")
_QUERY_UUID_RE = re.compile(rf"[?&]chat={_UUID_PATTERN}")
# Strip Grok's "Thought for Ns" reasoning header from streamed responses.
_THOUGHT_PREFIX_RE = re.compile(r"^Thought for [0-9]+\s*s\s*\n+", flags=re.IGNORECASE)


def _conv_id_from_url(url: str) -> Optional[str]:
    """Extract the conversation UUID from any of Grok's URL shapes.

    Recognised forms:
        ``/c/<uuid>``                      — classic chat URL
        ``/project/<uuid>?chat=<uuid>``    — chat lives inside a "project" workspace
        ``...?chat=<uuid>``                — short link form
    """
    if not url:
        return None
    match = _PATH_UUID_RE.search(url) or _QUERY_UUID_RE.search(url)
    return match.group(1) if match else None


def _strip_thought_prefix(text: str) -> str:
    """Drop Grok's ``Thought for Ns`` reasoning prefix if present."""
    return _THOUGHT_PREFIX_RE.sub("", text or "", count=1)


def _normalize_url(url_or_id: str) -> str:
    """Accept either a full URL or a bare conversation id; return a URL."""
    if not url_or_id:
        return "https://grok.com/"
    if url_or_id.startswith("http"):
        return url_or_id
    return f"https://grok.com/c/{url_or_id}"


# ── GrokClient ───────────────────────────────────────────────────────


class GrokClient:
    """Stateful Grok automation client.

    Holds a single UCBrowser instance for the lifetime of the client.
    Construct once (e.g. lazily inside the MCP server), reuse across calls.
    """

    GROK_HOME = "https://grok.com/"

    def __init__(
        self,
        *,
        mode: BrowserMode = BrowserMode.CHROMIUM_EXT,
        timeout_ms: int = 30000,
    ) -> None:
        self.mode = mode
        self.timeout_ms = timeout_ms
        self._uc: UCBrowser | None = None

    # ── Lifecycle ────────────────────────────────────────────────────

    def __enter__(self) -> "GrokClient":
        self._ensure_uc()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _ensure_uc(self) -> UCBrowser:
        if self._uc is None:
            logger.info("Starting UCBrowser for Grok (mode=%s).", self.mode)
            self._uc = UCBrowser(mode=self.mode, timeout_ms=self.timeout_ms)
            self._uc.start()
        return self._uc

    def close(self) -> None:
        if self._uc is not None:
            try:
                self._uc.close()
            except Exception as e:  # pragma: no cover
                logger.debug("UCBrowser close failed: %s", e)
            self._uc = None

    # ── Internal: prepare a page ─────────────────────────────────────

    def _open(self, url: str, *, wait_ms: int = 4000):
        """Open URL, dismiss obstacles, raise if login wall detected."""
        uc = self._ensure_uc()
        page = uc.open(url, wait_ms=wait_ms)
        # Best-effort obstacle clear; quiet on failure.
        uc.dismiss_cookies(page)
        uc.close_modal(page)
        if uc.has_login_wall(page):
            current = page.url
            page.close()
            raise GrokAuthRequired(
                f"Grok shows a login wall at {current}. "
                "Run `pixi run event-harvester web-login --urls https://grok.com` "
                "to log in, then retry."
            )
        return page

    # ── Core ─────────────────────────────────────────────────────────

    def send(
        self,
        message: str,
        *,
        conversation_url: Optional[str] = None,
        timeout_s: int = 60,
    ) -> dict:
        """Send a message; return ``{response, url, conversation_id}``.

        Grok-specific recipe (bypasses the generic UCBrowser.chat heuristic
        which mis-scores grok's input wrapper). Types into the ProseMirror
        editor and submits via Enter, then waits for a new assistant block
        ``div[data-testid="assistant-message"]`` to appear and stabilise.

        If ``conversation_url`` is None, posts to the Grok root (which
        creates a new conversation and redirects to ``/c/<uuid>``).
        """
        url = _normalize_url(conversation_url) if conversation_url else self.GROK_HOME
        page = self._open(url, wait_ms=4000)
        try:
            input_sel = _SELECTORS["chat_input"]
            assistant_sel = _SELECTORS["assistant_message"]
            page.wait_for_selector(input_sel, timeout=15000)

            # Snapshot the count of assistant blocks so we can detect a new one.
            prev_count = page.evaluate(
                "(sel) => document.querySelectorAll(sel).length",
                assistant_sel,
            )

            # Type via the extension's framework-aware setText (ProseMirror).
            typed = page.evaluate(
                """(args) => {
                    if (!window.__UC_setText) return {success: false, error: 'no __UC_setText'};
                    return window.__UC_setText(args[0], args[1]);
                }""",
                [input_sel, message],
            ) or {}
            if not typed.get("success"):
                logger.warning("Grok: __UC_setText failed (%s); using keyboard.type fallback", typed.get("error"))
                page.focus(input_sel)
                page.keyboard.type(message, delay=10)

            # Submit on Enter — Grok has no aria-labelled send button.
            page.focus(input_sel)
            page.keyboard.press("Enter")

            # Poll for a new assistant block to appear and its text to settle.
            deadline = time.monotonic() + timeout_s
            last_text = ""
            stable = 0
            while time.monotonic() < deadline:
                page.wait_for_timeout(700)
                state = page.evaluate(
                    """(args) => {
                        const sel = args[0];
                        const prev = args[1];
                        const els = document.querySelectorAll(sel);
                        if (els.length <= prev) return {ready: false, text: ''};
                        const el = els[els.length - 1];
                        return {ready: true, text: (el.innerText || '').trim()};
                    }""",
                    [assistant_sel, prev_count],
                ) or {}
                if not state.get("ready"):
                    continue
                cur = state.get("text") or ""
                if cur and cur == last_text:
                    stable += 1
                    if stable >= 3:  # ~2.1s of unchanged text → settled
                        break
                else:
                    stable = 0
                    last_text = cur

            result_url = page.url
            return {
                "response": _strip_thought_prefix(last_text),
                "url": result_url,
                "conversation_id": _conv_id_from_url(result_url),
            }
        finally:
            try:
                page.close()
            except Exception:
                pass

    def read(self, conversation_url: str) -> dict:
        """Return the full transcript: ``{url, conversation_id, messages}``.

        Walks ``div[data-testid="user-message"]`` and
        ``div[data-testid="assistant-message"]`` blocks in document order.
        Assistant messages have any ``Thought for Ns`` reasoning prefix
        stripped.
        """
        url = _normalize_url(conversation_url)
        page = self._open(url, wait_ms=3500)
        try:
            messages = page.evaluate(
                """(args) => {
                    const userSel = args[0];
                    const asstSel = args[1];
                    const all = document.querySelectorAll(
                        `${userSel}, ${asstSel}`
                    );
                    const out = [];
                    for (const el of all) {
                        const tid = el.getAttribute('data-testid');
                        out.push({
                            role: tid === 'user-message' ? 'user' : 'assistant',
                            text: (el.innerText || '').trim(),
                        });
                    }
                    return out;
                }""",
                [_SELECTORS["user_message"], _SELECTORS["assistant_message"]],
            ) or []
            # Strip Grok's reasoning prefix from assistant blocks.
            cleaned = [
                {**m, "text": _strip_thought_prefix(m["text"])} if m.get("role") == "assistant"
                else m
                for m in messages
            ]
            return {
                "url": page.url,
                "conversation_id": _conv_id_from_url(page.url),
                "messages": cleaned,
            }
        finally:
            try:
                page.close()
            except Exception:
                pass

    def list_conversations(self) -> list[dict]:
        """Scrape the sidebar for ``[{id, title, url}, ...]``."""
        page = self._open(self.GROK_HOME, wait_ms=4500)
        try:
            items = page.evaluate(
                """(sel) => {
                    const links = document.querySelectorAll(sel);
                    const seen = new Set();
                    const out = [];
                    for (const a of links) {
                        const href = a.href;
                        if (!href || seen.has(href)) continue;
                        seen.add(href);
                        // Try to grab a title from the link's text or its closest row
                        let title = (a.innerText || '').trim();
                        if (!title) {
                            const row = a.closest('li, [role="listitem"], div');
                            title = (row?.innerText || '').trim();
                        }
                        let m = null;
                        try { m = new URL(href).pathname.match(/\\/c\\/([^/?#]+)/); }
                        catch (e) {}
                        out.push({
                            id: m ? m[1] : null,
                            title: title.slice(0, 200) || null,
                            url: href,
                        });
                    }
                    return out;
                }""",
                _SELECTORS["conversation_link"],
            )
            return items or []
        finally:
            try:
                page.close()
            except Exception:
                pass

    def delete(self, conversation_url: str) -> bool:
        """Delete a conversation. Returns True if the row disappears."""
        conv_id = _conv_id_from_url(conversation_url) or conversation_url
        page = self._open(self.GROK_HOME, wait_ms=4000)
        try:
            # Locate sidebar row, hover, click 3-dot button
            opened = page.evaluate(
                """(args) => {
                    const id = args[0];
                    const moreSel = args[1];
                    const row = document.querySelector(`a[href*="/c/${id}"]`);
                    if (!row) return {ok: false, why: 'row-not-found'};
                    row.scrollIntoView({block: 'center'});
                    const container = row.closest('li, [role="listitem"], div') || row;
                    container.dispatchEvent(new MouseEvent('mouseenter', {bubbles: true}));
                    container.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
                    const btn = container.querySelector(moreSel)
                                || row.querySelector(moreSel);
                    if (!btn) return {ok: false, why: 'more-not-found'};
                    btn.click();
                    return {ok: true};
                }""",
                [conv_id, _SELECTORS["row_more_button"]],
            )
            if not opened or not opened.get("ok"):
                logger.warning("Grok delete: %s", opened)
                return False
            page.wait_for_timeout(400)
            # Click "Delete" menu item
            clicked = page.evaluate(
                """(sel) => {
                    for (const el of document.querySelectorAll(sel)) {
                        const t = (el.innerText || '').trim().toLowerCase();
                        if (t === 'delete' || t.startsWith('delete')) {
                            el.click();
                            return true;
                        }
                    }
                    return false;
                }""",
                _SELECTORS["menu_item"],
            )
            if not clicked:
                logger.warning("Grok delete: 'Delete' menu item not found")
                return False
            page.wait_for_timeout(500)
            # Confirm modal — pick the destructive button by text
            confirmed = page.evaluate(
                """() => {
                    for (const b of document.querySelectorAll('button, [role="button"]')) {
                        const t = (b.innerText || '').trim().toLowerCase();
                        if (t === 'delete' || t === 'confirm' || t === 'yes' || t === 'remove') {
                            b.click();
                            return true;
                        }
                    }
                    return false;
                }""",
            )
            if not confirmed:
                logger.warning("Grok delete: confirm button not found")
                return False
            page.wait_for_timeout(1500)
            # Verify the row is gone from the sidebar
            gone = page.evaluate(
                "(id) => !document.querySelector(`a[href*=\"/c/${id}\"]`)",
                conv_id,
            )
            return bool(gone)
        finally:
            try:
                page.close()
            except Exception:
                pass

    # ── Lifecycle helpers ────────────────────────────────────────────

    def new_chat(self) -> dict:
        """Create a new chat; return ``{url, conversation_id}``.

        Grok's root URL is the new-chat surface, but we need it to commit
        (the URL doesn't change to /c/<uuid> until the first message).
        For predictability we send a no-op probe — actually no, we just
        return the home URL; callers should use ``send`` to materialize.
        """
        return {"url": self.GROK_HOME, "conversation_id": None}

    def rename(self, conversation_url: str, new_title: str) -> bool:
        """Rename a conversation via its sidebar row's menu."""
        conv_id = _conv_id_from_url(conversation_url) or conversation_url
        page = self._open(self.GROK_HOME, wait_ms=4000)
        try:
            opened = page.evaluate(
                """(args) => {
                    const id = args[0];
                    const moreSel = args[1];
                    const row = document.querySelector(`a[href*="/c/${id}"]`);
                    if (!row) return false;
                    row.scrollIntoView({block: 'center'});
                    const container = row.closest('li, [role="listitem"], div') || row;
                    container.dispatchEvent(new MouseEvent('mouseenter', {bubbles: true}));
                    const btn = container.querySelector(moreSel)
                                || row.querySelector(moreSel);
                    if (!btn) return false;
                    btn.click();
                    return true;
                }""",
                [conv_id, _SELECTORS["row_more_button"]],
            )
            if not opened:
                return False
            page.wait_for_timeout(400)
            clicked = page.evaluate(
                """(sel) => {
                    for (const el of document.querySelectorAll(sel)) {
                        const t = (el.innerText || '').trim().toLowerCase();
                        if (t === 'rename' || t.startsWith('rename')) {
                            el.click();
                            return true;
                        }
                    }
                    return false;
                }""",
                _SELECTORS["menu_item"],
            )
            if not clicked:
                return False
            page.wait_for_timeout(400)
            # Fill the rename input — Grok may use inline edit or a modal
            typed = page.evaluate(
                """(args) => {
                    const sel = args[0];
                    const title = args[1];
                    const input = document.querySelector(sel);
                    if (!input) return false;
                    input.focus();
                    if ('value' in input) input.value = title;
                    else input.textContent = title;
                    input.dispatchEvent(new Event('input', {bubbles: true}));
                    input.dispatchEvent(new Event('change', {bubbles: true}));
                    return true;
                }""",
                [_SELECTORS["rename_input"], new_title],
            )
            if not typed:
                return False
            # Submit via Enter or a Save button
            page.keyboard.press("Enter")
            page.wait_for_timeout(800)
            return True
        finally:
            try:
                page.close()
            except Exception:
                pass

    def archive(self, conversation_url: str) -> bool:
        """Archive a conversation via its sidebar row's menu (best-effort)."""
        conv_id = _conv_id_from_url(conversation_url) or conversation_url
        page = self._open(self.GROK_HOME, wait_ms=4000)
        try:
            opened = page.evaluate(
                """(args) => {
                    const id = args[0];
                    const moreSel = args[1];
                    const row = document.querySelector(`a[href*="/c/${id}"]`);
                    if (!row) return false;
                    const container = row.closest('li, [role="listitem"], div') || row;
                    container.dispatchEvent(new MouseEvent('mouseenter', {bubbles: true}));
                    const btn = container.querySelector(moreSel)
                                || row.querySelector(moreSel);
                    if (!btn) return false;
                    btn.click();
                    return true;
                }""",
                [conv_id, _SELECTORS["row_more_button"]],
            )
            if not opened:
                return False
            page.wait_for_timeout(400)
            clicked = page.evaluate(
                """(sel) => {
                    for (const el of document.querySelectorAll(sel)) {
                        const t = (el.innerText || '').trim().toLowerCase();
                        if (t === 'archive' || t.startsWith('archive')) {
                            el.click();
                            return true;
                        }
                    }
                    return false;
                }""",
                _SELECTORS["menu_item"],
            )
            page.wait_for_timeout(800)
            return bool(clicked)
        finally:
            try:
                page.close()
            except Exception:
                pass

    def regenerate(self, conversation_url: str, *, timeout_s: int = 60) -> dict:
        """Click "Regenerate" on the last assistant message and capture the new response."""
        url = _normalize_url(conversation_url)
        page = self._open(url, wait_ms=3500)
        uc = self._ensure_uc()
        try:
            # Snapshot the last assistant block's text as the "old"
            old = page.evaluate(
                """() => {
                    const els = document.querySelectorAll(
                        '[data-message-author-role="assistant"]'
                    );
                    if (!els.length) return '';
                    return (els[els.length - 1].innerText || '').trim();
                }""",
            )
            # Capture a trigram baseline so __UC_extractFromContainer can score
            uc._wait_ready(page)
            page.evaluate("window.__UC_captureBaseline && window.__UC_captureBaseline()")
            clicked = page.evaluate(
                """(sel) => {
                    const btns = document.querySelectorAll(sel);
                    if (!btns.length) return false;
                    btns[btns.length - 1].click();  // last regenerate = most recent reply
                    return true;
                }""",
                _SELECTORS["regenerate_button"],
            )
            if not clicked:
                return {"ok": False, "reason": "regenerate-not-found", "response": None}
            # Poll for the assistant block to differ from `old` and stabilise
            deadline = time.monotonic() + timeout_s
            last = ""
            stable = 0
            while time.monotonic() < deadline:
                page.wait_for_timeout(500)
                cur = page.evaluate(
                    """() => {
                        const els = document.querySelectorAll(
                            '[data-message-author-role="assistant"]'
                        );
                        if (!els.length) return '';
                        return (els[els.length - 1].innerText || '').trim();
                    }""",
                )
                if cur and cur != old:
                    if cur == last:
                        stable += 1
                        if stable >= 3:
                            return {"ok": True, "response": cur}
                    else:
                        stable = 0
                        last = cur
            return {"ok": bool(last), "response": last, "reason": "timeout"}
        finally:
            try:
                page.close()
            except Exception:
                pass

    def stop(self, conversation_url: str) -> bool:
        """Click the stop button if the assistant is currently generating."""
        url = _normalize_url(conversation_url)
        page = self._open(url, wait_ms=2000)
        try:
            clicked = page.evaluate(
                """(sel) => {
                    const btn = document.querySelector(sel);
                    if (!btn) return false;
                    btn.click();
                    return true;
                }""",
                _SELECTORS["stop_button"],
            )
            return bool(clicked)
        finally:
            try:
                page.close()
            except Exception:
                pass

    # ── Models ───────────────────────────────────────────────────────

    def get_models(self) -> list[str]:
        """Open the model picker and return the menu options as labels."""
        page = self._open(self.GROK_HOME, wait_ms=3500)
        try:
            names = page.evaluate(
                """(sel) => {
                    const trigger = document.querySelector(sel);
                    if (!trigger) return [];
                    trigger.click();
                    return new Promise((resolve) => {
                        setTimeout(() => {
                            const opts = document.querySelectorAll(
                                '[role="option"], [role="menuitem"]'
                            );
                            const out = [];
                            for (const o of opts) {
                                const t = (o.innerText || '').trim();
                                if (t) out.push(t.split('\\n')[0]);
                            }
                            // Close the picker so we leave the page clean
                            document.body.click();
                            resolve(out);
                        }, 400);
                    });
                }""",
                _SELECTORS["model_picker"],
            )
            return list(dict.fromkeys(names or []))  # de-dupe preserving order
        finally:
            try:
                page.close()
            except Exception:
                pass

    def switch_model(self, model_name: str) -> bool:
        """Open the picker and click the option matching ``model_name``."""
        page = self._open(self.GROK_HOME, wait_ms=3500)
        try:
            chose = page.evaluate(
                """(args) => {
                    const trigger = document.querySelector(args[0]);
                    if (!trigger) return false;
                    trigger.click();
                    return new Promise((resolve) => {
                        setTimeout(() => {
                            const target = args[1].toLowerCase();
                            const opts = document.querySelectorAll(
                                '[role="option"], [role="menuitem"]'
                            );
                            for (const o of opts) {
                                const t = (o.innerText || '').trim().toLowerCase();
                                if (t === target || t.startsWith(target)) {
                                    o.click();
                                    resolve(true);
                                    return;
                                }
                            }
                            document.body.click();
                            resolve(false);
                        }, 400);
                    });
                }""",
                [_SELECTORS["model_picker"], model_name],
            )
            return bool(chose)
        finally:
            try:
                page.close()
            except Exception:
                pass


# ── Process-level singleton ──────────────────────────────────────────
#
# Both the MCP server and the litellm CustomLLM provider want a single
# long-lived GrokClient (one persistent browser, shared sessions). Keep
# the singleton here so neither caller spins up its own competing one.

_singleton: GrokClient | None = None
_singleton_lock = threading.Lock()


def get_grok_client() -> GrokClient:
    """Return the process-wide GrokClient, constructing it on first call."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            logger.info("Creating GrokClient singleton.")
            _singleton = GrokClient()
            atexit.register(_close_singleton)
        return _singleton


def _close_singleton() -> None:
    global _singleton
    with _singleton_lock:
        client = _singleton
        _singleton = None
    if client is not None:
        try:
            client.close()
        except Exception:  # pragma: no cover - defensive
            logger.debug("GrokClient singleton close failed.", exc_info=True)


def reset_grok_singleton() -> None:
    """Drop the cached client (tests only). Closes the browser if open."""
    _close_singleton()
