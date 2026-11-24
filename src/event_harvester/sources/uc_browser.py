"""The one place event_tool obtains a browser and its pages.

A harvest worker, an explore session and a CLI command each hold one
:class:`WebBrowser`. Nothing else constructs ``UCBrowser`` (the controller
installed from ``ext/universal_controller/``), and nothing outside this module
reads its private members.

Which sources need a browser at all is decided by the routing in
``web_fetch`` (``HTTP_MODES``), not here: a feed or API source is never handed
a :class:`WebBrowser`. This class only says how the browser technique is
carried out.

What :class:`WebBrowser` owns:

* **Starting and stopping.** ``with WebBrowser(...)`` launches on entry and
  closes on exit; ``start()`` / ``close()`` do the same for a holder that
  outlives one block (an explore session).
* **Pages.** ``open``, ``open_with_intercept`` and ``new_page`` are the three
  ways a page comes to exist. Each one is remembered, and ``close_pages`` /
  ``close`` shut whatever a caller left open.

Everything else is the controller's and is forwarded untouched: the page
controls (``detect_all``, ``dismiss_cookies``, ``search``, ...), the signature
store, ``timeout_ms``.
"""

from __future__ import annotations

import logging

from uc_browser import BrowserMode, UCBrowser

logger = logging.getLogger("event_harvester.uc_browser")

__all__ = ["BrowserMode", "NATIVE_CDP_PORT", "WebBrowser", "native_route"]

#: The debugging port of the Chrome a person keeps open for sources whose bot
#: check only a person can clear (the ``uc-native-window`` user unit).
NATIVE_CDP_PORT = 9222


def _port_open(port: int) -> bool:
    import socket

    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def native_route() -> tuple[str, dict]:
    """How native-Chrome work gets its browser now: ``(method, WebBrowser arguments)``.

    A Chrome open on the native port is attached to by naming its endpoint:
    the controller launches nothing, and on exit closes only the pages this
    client opened. With nothing listening there, the controller launches
    Chrome itself and closes it. They are different access methods and are
    logged as such. Measured 2026-11-24 on erobay: a check a person cleared in
    an open, unattached Chrome held for the pages opened after it; the same
    check never completed in a Chrome launched with the automation attached
    (two attended runs of 120 s, two unattended of 60 s).

    The endpoint is named here, by event_tool. The controller does not look
    for an open browser on its own (it did until ``04b200f``), and it refuses
    to launch on a profile another browser holds.
    """
    if _port_open(NATIVE_CDP_PORT):
        return "native:open-browser", {
            "cdp_endpoint": f"http://127.0.0.1:{NATIVE_CDP_PORT}", "label": "event_tool",
        }
    return "native", {"native_chrome": True}


class WebBrowser:
    """A controlled browser for one thread.

    ``uc_kwargs`` go to ``UCBrowser`` unchanged (``native_chrome=True``,
    ``mode=BrowserMode.CHROMIUM_EXT``, ``cdp_endpoint=...``, ...). Native work
    takes them from :func:`native_route`. Sync Playwright is bound to the
    thread that started it, so use one instance per thread.
    """

    def __init__(self, **uc_kwargs):
        self._uc_kwargs = uc_kwargs
        self._uc = None
        self._pages: list = []

    def start(self) -> "WebBrowser":
        """Launch the browser. Entering a ``with`` block does this."""
        if self._uc is None:
            uc = UCBrowser(**self._uc_kwargs)
            uc.__enter__()
            self._uc = uc
        return self

    def __enter__(self) -> "WebBrowser":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    def _controller(self):
        if self._uc is None:
            raise RuntimeError(
                "WebBrowser is not started: use it in a `with` block or call start()",
            )
        return self._uc

    def __getattr__(self, name: str):
        # Reached only for names not defined here: the controller's public API.
        # Private names stop here, so a caller cannot reach past this class.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._controller(), name)

    # ── Pages ────────────────────────────────────────────────────────

    def open(self, url: str, *args, **kwargs):
        """Open ``url`` in a new tab and return the page (``UCBrowser.open``)."""
        page = self._controller().open(url, *args, **kwargs)
        self._pages.append(page)
        return page

    def open_with_intercept(self, *args, **kwargs):
        """``UCBrowser.open_with_intercept``: ``(page, captured_responses)``."""
        page, captured = self._controller().open_with_intercept(*args, **kwargs)
        self._pages.append(page)
        return page, captured

    def new_page(self):
        """A blank page, prepared the way ``open`` prepares one, not navigated.

        For callers that must attach listeners before the first request, or
        that navigate with their own wait (``UCBrowser.new_page``). Attached
        to the UC service, the page is leased to this client there.
        """
        page = self._controller().new_page()
        self._pages.append(page)
        return page

    def close_pages(self) -> int:
        """Close every page handed out that is still open. Returns how many."""
        pages, self._pages = self._pages, []
        closed = 0
        for page in pages:
            try:
                if not page.is_closed():
                    page.close()
                    closed += 1
            except Exception as e:  # noqa: BLE001 — teardown is best-effort
                logger.debug("page close failed: %s", e)
        return closed

    def close(self) -> None:
        """Close open pages, then the browser. A no-op if it never started."""
        if self._uc is None:
            return
        self.close_pages()
        uc, self._uc = self._uc, None
        uc.__exit__(None, None, None)
