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
import os
import time

from uc_browser import BrowserMode, UCBrowser

logger = logging.getLogger("event_harvester.uc_browser")

__all__ = [
    "BrowserMode", "NATIVE_CDP_PORT", "WebBrowser", "native_browser", "native_route",
    "prepare_native",
]

#: The debugging port of the Chrome a person keeps open for sources whose bot
#: check only a person can clear (the ``uc-native-window`` user unit).
NATIVE_CDP_PORT = 9222


def _port_open(port: int) -> bool:
    import socket

    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def native_route(service: str | None = None, window: str | None = None) -> tuple[str, dict]:
    """How native-Chrome work gets its browser now: ``(method, WebBrowser arguments)``.

    The one decision, for every caller (harvest, its native retry, the explore
    tool, the CLI). In order:

    1. A native window of the Universal Controller service, when event_tool's
       config names both the service and the window (``EH_UC_SERVICE_URL``,
       ``WEB_UC_NATIVE_WINDOW``). The service owns that browser and its
       profile, so nothing below is tried: launching locally would be a second
       browser on a held profile.
    2. A Chrome open on the native port, attached to by naming its endpoint:
       the controller launches nothing, and on exit closes only the pages
       this client opened.
    3. Otherwise the controller launches Chrome itself and closes it.

    They are different access methods and are logged as such. Measured
    2026-11-24 on erobay: a check finished when its page loaded in an open,
    unattached Chrome; it never completed in a Chrome launched with the
    automation attached (two attended runs of 120 s, two unattended of 60 s).

    ``service`` and ``window`` default to event_tool's config. The controller
    does not look for a browser on its own (it did until ``04b200f``).
    """
    if service is None or window is None:
        from event_harvester.config import load_config

        web = load_config().web
        service, window = web.uc_service, web.uc_native_window
    if service and window:
        return f"service:{window}", {
            "service": service, "window": window,
            "label": f"event_tool:{os.getpid()}:{int(time.time())}",
        }
    if _port_open(NATIVE_CDP_PORT):
        return "native:open-browser", {
            "cdp_endpoint": f"http://127.0.0.1:{NATIVE_CDP_PORT}", "label": "event_tool",
        }
    return "native", {"native_chrome": True}


def _devtools(endpoint: str, path: str, method: str = "GET"):
    """One call to the browser's own HTTP endpoint. Not a CDP session."""
    import json
    import urllib.request

    request = urllib.request.Request(f"{endpoint}{path}", method=method)
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 — loopback
        body = response.read().decode("utf-8", "replace")
    try:
        return json.loads(body)
    except ValueError:
        return body


#: Seconds an unattached page may go on showing its own address as its title
#: (not rendered yet) before it is given up on. Separate from a source's
#: ``bot_check_wait_s``, which is how long a bot-check title is waited out.
LOAD_WAIT_S = 30


def _wait_through(read_title, url: str, wait_s: int) -> tuple[bool, str]:
    """Read a page's title once a second until it is past its bot check.

    ``(through, last title)``. This is event_tool's readiness rule, the same
    for every way of loading a page unattached: through when, on two
    consecutive reads, the title is not empty, is not still the address (the
    page has not rendered yet) and is not a bot-check title.

    Two things can keep a page from being through and they have their own
    clocks: a bot-check title is waited out for ``wait_s`` seconds, a page
    that has not rendered for ``LOAD_WAIT_S``. The caller tells them apart by
    the last title (:func:`not_through`). Measured 2026-11-24 on ``ra-sf``:
    its title is ``ra.co`` while it loads, for 1 to 5 s on three runs and for
    more than 15 s on a fourth, which one shared 15 s clock reported as a bot
    check that was never there.
    """
    from event_harvester.sources.web_utils import is_bot_check

    bare = url.split("://", 1)[-1]
    title, settled, checking, loading_for = "", 0, 0, 0
    for second in range(1, wait_s + LOAD_WAIT_S + 1):
        time.sleep(1)
        now = read_title()
        if now != title:
            logger.info("Unattached page, %ds in: title %r -> %r", second, title[:80], now[:80])
            title = now
        loading = not title or title in url or bare.startswith(title)
        check = not loading and is_bot_check(title)
        settled = 0 if loading or check else settled + 1
        checking += check
        loading_for += loading
        if settled >= 2:
            return True, title
        if checking >= wait_s or loading_for >= LOAD_WAIT_S:
            break
    return False, title


def not_through(title: str, wait_s: int) -> Exception:
    """The error for a page that did not get through, by what it last showed.

    A bot-check title is a block: it needs a person. Anything else is a page
    that did not render, which is a deviation and says nothing about a check.
    """
    from event_harvester.sources.web_utils import SourceBlocked, SourceDeviated, is_bot_check

    if is_bot_check(title):
        return SourceBlocked(
            "the source's page", "cloudflare_challenge",
            f"a bot check was still up after {wait_s}s in the native browser with "
            f"nothing attached (title {title[:60]!r})",
        )
    return SourceDeviated(
        "the source's page", "not_loaded",
        f"the page had not rendered after {LOAD_WAIT_S}s in the native browser with "
        f"nothing attached (title still {title[:60]!r})",
    )


def pass_check_unattached(endpoint: str, url: str, wait_s: int) -> tuple[bool, str]:
    """Load ``url`` in the open browser with no client attached; wait for it.

    Returns ``(through, last title)``. The page is opened, watched and closed
    through the browser's HTTP endpoints (``/json/new``, ``/json/list``,
    ``/json/close``), so no CDP session exists while its bot check runs.
    Measured 2026-11-24 on erobay: loaded this way the check finished by
    itself within 8 s and the harvest, attaching afterwards, was not
    challenged; with a client attached from the start the same check span for
    120 s. It did not finish this way while the X screen was blanked.
    """
    from urllib.parse import quote

    tab_id = _devtools(endpoint, "/json/new?" + quote(url, safe=":/?="), method="PUT")["id"]

    def title() -> str:
        return next(
            (str(t.get("title", "")) for t in _devtools(endpoint, "/json/list")
             if t.get("id") == tab_id), "",
        )

    try:
        return _wait_through(title, url, wait_s)
    finally:
        try:
            _devtools(endpoint, f"/json/close/{tab_id}")
        except Exception as e:  # noqa: BLE001 — the tab may already be gone
            logger.debug("closing the unattached tab failed: %s", e)


def pass_check_in_service(
    service: str, window: str, label: str | None, url: str, wait_s: int,
) -> tuple[bool, str]:
    """Load ``url`` in a quiet window of the UC service; wait for it.

    Returns ``(through, last title)``. A quiet reservation
    (``register_client(unattached=True)``) is refused while anything else is
    registered on or connected to the window, and while it is held the service
    attaches nothing: the page is created and its title read over HTTP only.
    The service reports; whether the page is through is decided here.

    A refused reservation (``busy``, naming who holds the window) is raised,
    not waited on: it is not permission to attach or to launch another browser.
    """
    from uc_browser.service.client import ServiceClient

    client = ServiceClient(service)
    client_id = client.register_client(window, label, unattached=True)
    target_id = None
    try:
        client.start_window(window)
        target_id = client.new_page(window, client_id, url=url)
        return _wait_through(
            lambda: str(client.page_info(window, target_id).get("title", "")), url, wait_s,
        )
    finally:
        for what, close in (
            ("page", lambda: target_id and client.close_page(window, target_id)),
            ("reservation", lambda: client.close_client(client_id)),
        ):
            try:
                close()
            except Exception as e:  # noqa: BLE001 — the lease expires at the service
                logger.warning("closing the quiet %s at the UC service failed: %s", what, e)


def prepare_native(uc_kwargs: dict, url: str, wait_s: int) -> tuple[bool, str]:
    """Let ``url`` load in the native browser with nothing attached, before any attach.

    ``(through, last title)``, by whichever way the route offers: a quiet
    reservation in the UC service's window, or the open browser's own HTTP
    endpoints. A Chrome this run launches has no unattached state to load a
    page in, so there is nothing to prepare.

    A browser whose window is minimized is refused first
    (:class:`native_window.NativeWindowNotDrawn`): nothing in it is drawn, so
    a page there would not get through and the wait would only be spent.
    """
    from event_harvester.sources.native_window import require_drawn

    require_drawn(uc_kwargs)
    if "service" in uc_kwargs:
        return pass_check_in_service(
            uc_kwargs["service"], uc_kwargs["window"], uc_kwargs.get("label"), url, wait_s,
        )
    if "cdp_endpoint" in uc_kwargs:
        return pass_check_unattached(uc_kwargs["cdp_endpoint"], url, wait_s)
    return True, ""


def native_browser(url: str, wait_s: int) -> "WebBrowser":
    """A not-yet-started browser for native work on ``url``, its page prepared.

    For callers that hold one browser on one address (the explore tool, the
    CLI replay). Attaching first can itself stop a bot check from finishing,
    so the address is loaded unattached before the browser is handed out. A
    page that does not get through raises what :func:`not_through` says it is.
    """
    _method, uc_kwargs = native_route()
    through, title = prepare_native(uc_kwargs, url, wait_s)
    if not through:
        raise not_through(title, wait_s)
    return WebBrowser(**uc_kwargs)


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
