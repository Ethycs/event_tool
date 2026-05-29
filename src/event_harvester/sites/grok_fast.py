"""Grok fast path: React-onSubmit trigger + fetch-hook response capture.

This combines the two halves of Option 3:

1. **Trigger via React** — find the ``<form>`` ancestor of ``div.ProseMirror``
   in the React fiber tree and call its ``memoizedProps.onSubmit({preventDefault})``
   directly. The submit goes through Grok's apiClient wrapper, so the
   ``x-statsig-id`` anti-bot signing and cookie handling happen as if
   the user clicked. Skips the keyboard-Enter roundtrip and any
   ProseMirror keymap dispatch.

2. **Capture via fetch hook** — wrap ``window.fetch`` and intercept
   responses for the chat endpoints. Stream the body to Python via
   :func:`page.expose_function`. Skips the slow DOM-polling for the
   assistant message.

The browser is still the auth oracle (the page hosts all the JS that
signs the request). We just stop typing + stop watching the DOM and
instead pull the response from the network layer where it really
lives.

Expected warm overhead per send: < 0.5s + Grok's actual generation
time, vs ~2.4s overhead on the DOM-only path.

Falls back to ``GrokClient.send`` if anything goes sideways (response
parser fails, callback times out, etc.) — wired in :mod:`uc` provider.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Optional

from event_harvester.sites.grok import GrokClient, _conv_id_from_url
# The JSON-lines parsers live with the (currently-not-wired) shim-path
# module so we don't duplicate them.
from event_harvester.sites.grok_api import (
    _extract_assistant_text,
    _extract_conversation_id,
)

logger = logging.getLogger("event_harvester.sites.grok_fast")

# Match both ``/conversations/new`` (new chats) and
# ``/conversations/<uuid>/responses`` (continuing existing chats).
_CHAT_URL_RE = re.compile(
    r"/rest/app-chat/conversations(/new|/[0-9a-fA-F-]+/responses)\b"
)


# ── JS installed once per page ─────────────────────────────────────


_INSTALL_JS = r"""
(() => {
  if (window.__UC_GROK_FAST_INSTALLED) return;
  window.__UC_GROK_FAST_INSTALLED = true;

  // ── Response storage (polled by wait_for_function, NOT expose_function) ──
  // Sync Playwright can't deliver expose_function callbacks while Python
  // is blocked on the same thread, so we write to a window slot and let
  // Python poll via wait_for_function (which yields the greenlet).
  window.__GROK_LAST_RESPONSE = null;
  window.__GROK_RESPONSE_VERSION = 0;
  window.__UC_grokDeliver = function(payload) {
    try {
      window.__GROK_LAST_RESPONSE = payload;
      window.__GROK_RESPONSE_VERSION++;
    } catch (e) {}
  };

  // ── React onSubmit invoker ────────────────────────────────
  // Walks up from div.ProseMirror to the closest fiber, then up through
  // fibers looking for the form's onSubmit. Caches the last hit per
  // discovery; if it 404's (component remounted) we re-walk.
  function getFiber(el) {
    if (!el) return null;
    for (const k of Object.keys(el)) {
      if (k.startsWith('__reactFiber$') || k.startsWith('__reactInternalInstance$')) {
        return el[k];
      }
    }
    return null;
  }
  function nearestFiberUp(el, maxUp) {
    maxUp = maxUp || 25;
    let cur = el;
    for (let i = 0; i < maxUp && cur; i++) {
      const f = getFiber(cur);
      if (f) return f;
      cur = cur.parentElement;
    }
    return null;
  }
  window.__UC_grokFindOnSubmit = function() {
    const input = document.querySelector('div.ProseMirror');
    if (!input) return {ok: false, why: 'no-prosemirror'};
    let f = nearestFiberUp(input, 25);
    if (!f) return {ok: false, why: 'no-fiber-above-input'};
    let depth = 0;
    while (f && depth < 40) {
      try {
        const props = f.memoizedProps || f.pendingProps;
        if (props && typeof props.onSubmit === 'function') {
          // Stash on window so the trigger function can call it without
          // re-walking. Stale references are caught by callers — if it
          // throws, we re-walk.
          window.__UC_grokOnSubmit = props.onSubmit;
          return {
            ok: true,
            depth,
            type: (typeof f.type === 'function'
                    ? (f.type.displayName || f.type.name || '(anon-fn)')
                    : String(f.type)),
            fn_name: props.onSubmit.name || '(anon)',
          };
        }
      } catch (e) {}
      f = f.return;
      depth++;
    }
    return {ok: false, why: 'no-onSubmit-in-trail', walked: depth};
  };

  window.__UC_grokTriggerSubmit = async function() {
    // Always re-walk: navigating between /c/<uuid> URLs remounts the form,
    // so a cached handler from a previous send may submit on a detached
    // form (silent no-op) or with empty state.
    const find = window.__UC_grokFindOnSubmit();
    if (!find.ok) return find;
    try {
      await window.__UC_grokOnSubmit({preventDefault: () => {}, persist: () => {}});
      return {ok: true, picked: find};
    } catch (e) {
      return {ok: false, why: 'submit-failed', err: String(e), picked: find};
    }
  };
})();
"""


# ── Client ─────────────────────────────────────────────────────────


class GrokFastClient:
    """Fast-path Grok client. Reuses GrokClient as the auth oracle.

    The response-delivery mechanism is *polled*, not bridged: the install
    script's interceptor writes the response onto
    ``window.__GROK_LAST_RESPONSE`` and bumps
    ``window.__GROK_RESPONSE_VERSION``. The Python ``send()`` uses
    :func:`page.wait_for_function` to wait for the version counter to
    advance — that yields Playwright's greenlet between checks (unlike a
    Python-side ``queue.get`` which would block the whole thread and
    starve the bridge).
    """

    def __init__(self, grok_client: Optional[GrokClient] = None) -> None:
        from event_harvester.sites.grok import get_grok_client
        self._client = grok_client or get_grok_client()
        # session_key -> True once the install script + interceptor
        # registration is in place for that session's page.
        self._installed: dict[str, bool] = {}
        self._lock = threading.Lock()

    # ── Internal: install per session page ──────────────────

    def _ensure_installed(self, page, session_key: str) -> None:
        """Install the React onSubmit invoker on the page.

        Always run the JS — page.goto for a new URL resets window globals
        so a previously-installed walker disappears. The install script
        guards itself with ``__UC_GROK_FAST_INSTALLED``, so re-running on
        the same warm page is a cheap no-op.

        Response capture is done at the Playwright network layer via
        ``page.expect_response`` — that sees the call regardless of
        whether Grok used ``fetch`` or ``XMLHttpRequest``, and Sentry's
        ``window.fetch`` wrap can't bypass it.
        """
        page.evaluate(_INSTALL_JS)
        with self._lock:
            self._installed[session_key] = True

    # ── Public API ──────────────────────────────────────────

    def send(
        self,
        message: str,
        *,
        conversation_url: Optional[str] = None,
        session_key: Optional[str] = None,
        timeout_s: int = 120,
    ) -> dict:
        """Send a message via React-onSubmit + capture via fetch hook.

        Raises ``RuntimeError`` on any structural failure (hook didn't
        install, onSubmit not found, response timeout). Callers should
        catch and fall back to ``GrokClient.send``.
        """
        sk = session_key or self._client.DEFAULT_SESSION
        url_target = (
            conversation_url
            if conversation_url
            else self._client.GROK_HOME
        )
        with self._client._session(sk) as page:
            # Navigate to the target chat (or stay on home for new chat).
            self._client._navigate_in(page, url_target, wait_ms=0)
            page.wait_for_selector("div.ProseMirror", timeout=15000)
            self._ensure_installed(page, sk)

            # Set up the network listener BEFORE triggering — Playwright
            # buffers matching responses from the moment the `with` block
            # opens, so we won't miss the chat call even if it starts
            # before our trigger evaluate() returns.
            try:
                with page.expect_response(
                    lambda r: bool(_CHAT_URL_RE.search(r.url)),
                    timeout=timeout_s * 1000,
                ) as resp_info:
                    # Type via the extension's framework-aware setText
                    # (sets ProseMirror state without keystrokes).
                    typed = page.evaluate(
                        "(args) => window.__UC_setText && window.__UC_setText(args[0], args[1])",
                        ["div.ProseMirror", message],
                    ) or {}
                    if not typed.get("success"):
                        raise RuntimeError(
                            f"GrokFastClient: setText failed ({typed.get('error')})"
                        )
                    # Trigger via React onSubmit (skips keyboard + ProseMirror keymap).
                    trig = page.evaluate("() => window.__UC_grokTriggerSubmit()") or {}
                    if not trig.get("ok"):
                        raise RuntimeError(f"GrokFastClient: trigger failed: {trig}")
            except Exception as e:
                raise RuntimeError(
                    f"GrokFastClient: response wait failed: {e}"
                )

            response = resp_info.value
            try:
                body = response.text()
            except Exception as e:
                raise RuntimeError(f"GrokFastClient: response.text() failed: {e}")
            logger.debug(
                "[grok_fast] matched response: url=%s status=%s body_len=%d",
                response.url[:140], response.status, len(body),
            )
            text = _extract_assistant_text(body)
            conv_id = _extract_conversation_id(body) or _conv_id_from_url(response.url)
            # Falling back to the page URL covers continue-chat where the
            # response body's conversation block isn't always present.
            if not conv_id:
                conv_id = _conv_id_from_url(page.url)
            final_url = (
                f"https://grok.com/c/{conv_id}"
                if conv_id
                else page.url
            )
            return {
                "response": text,
                "url": final_url,
                "conversation_id": conv_id,
            }
