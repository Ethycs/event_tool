"""`WebBrowser` — the one place a browser and its pages come from.

What a consumer can observe: whether a browser was launched at all, whether
pages it was handed get closed, and that the controller's private members are
not reachable through it.
"""

from __future__ import annotations

import pytest

from event_harvester.sources import uc_browser
from event_harvester.sources.uc_browser import WebBrowser
from event_harvester.sources.web_utils import _fetch_event_details_parallel


class FakePage:
    def __init__(self, html: str = "", goto_error: Exception | None = None):
        self.closed = False
        self._html = html
        self._goto_error = goto_error

    def goto(self, url, **_kw):
        if self._goto_error is not None:
            raise self._goto_error

    def wait_for_timeout(self, _ms):
        pass

    def content(self):
        return self._html

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, pages):
        self._pages = list(pages)

    def new_page(self):
        return self._pages.pop(0)


class FakeController:
    """Stands in for ``UCBrowser``: counts launches, hands out canned pages."""

    launched = 0

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.exited = False
        self.timeout_ms = 30000
        self._stealth_plugin = None
        self._context = FakeContext(self.pages)

    pages: list = []

    def __enter__(self):
        type(self).launched += 1
        return self

    def __exit__(self, *_a):
        self.exited = True

    def open(self, url, **_kw):
        return self._context.new_page()

    def new_page(self):
        return self._context.new_page()

    def detect_all(self, page):
        return {"search": []}


@pytest.fixture
def controller(monkeypatch):
    FakeController.launched = 0
    FakeController.pages = []
    monkeypatch.setattr(uc_browser, "UCBrowser", FakeController)
    return FakeController


def test_entering_launches_once_and_leaving_closes(controller):
    controller.pages = [FakePage(), FakePage()]
    with WebBrowser(native_chrome=True) as uc:
        assert controller.launched == 1
        uc.open("https://a.example")
        assert uc.detect_all(object()) == {"search": []}
        uc.new_page()
        inner = uc._uc
    assert controller.launched == 1
    assert inner.kwargs == {"native_chrome": True}
    assert inner.exited is True


def test_use_before_start_is_refused_not_silently_launched():
    uc = WebBrowser()
    with pytest.raises(RuntimeError, match="not started"):
        uc.open("https://a.example")
    uc.close()  # closing what never started is not an error


def test_plain_http_sources_are_routed_past_the_browser(controller, monkeypatch):
    """The route table decides: a feed source is never handed a browser."""
    from event_harvester.sources import web_fetch

    seen: dict = {}

    def fake_ics(src, *, tracer=None, capture=None):
        seen["src"] = src
        return [{"id": "ics:1", "channel": "feed"}]

    monkeypatch.setitem(web_fetch.HTTP_MODES, "ics", fake_ics)
    monkeypatch.setattr(web_fetch, "_save_cooldown", lambda url: None)

    src = {"url": "webcal://x.example/cal.ics", "name": "feed", "mode": "ics"}
    out = web_fetch.fetch_web_sources([src], parallel=3)

    assert out == [{"id": "ics:1", "channel": "feed"}]
    assert seen["src"] is src
    assert controller.launched == 0


def test_pages_left_open_are_closed_and_closed_ones_are_left_alone(controller):
    kept, closed_by_caller, blank = FakePage(), FakePage(), FakePage()
    controller.pages = [kept, closed_by_caller, blank]
    with WebBrowser() as uc:
        uc.open("https://a.example")
        uc.open("https://b.example").close()
        uc.new_page()
        assert uc.close_pages() == 2
        assert kept.closed and blank.closed
        # Nothing is tracked twice: a second sweep has nothing to do.
        assert uc.close_pages() == 0


def test_leaving_the_block_closes_pages_before_the_browser(controller):
    page = FakePage()
    controller.pages = [page]
    with WebBrowser() as uc:
        uc.open("https://a.example")
    assert page.closed is True


def test_private_members_of_the_controller_are_not_reachable(controller):
    with WebBrowser() as uc:
        with pytest.raises(AttributeError):
            uc._context
        with pytest.raises(AttributeError):
            uc._stealth_plugin


def test_a_detail_tab_whose_navigation_fails_is_still_closed(controller):
    """The tab used to be dropped on the floor: opened, never closed."""
    article = "<html><body><p>" + "Doors at eight. " * 10 + "</p></body></html>"
    failed = FakePage(goto_error=TimeoutError("navigation timeout"))
    loaded = FakePage(html=article)
    controller.pages = [failed, loaded]
    links = [{"url": "https://e.example/1"}, {"url": "https://e.example/2"}]
    with WebBrowser() as uc:
        results = _fetch_event_details_parallel(uc, links, timeout_ms=1000)
        assert failed.closed and loaded.closed
    assert results[0] == (links[0], None)
    assert results[1][0] is links[1] and "Doors at eight." in results[1][1]


class FakeDevtools:
    """Chrome's HTTP endpoint: one tab, whose title follows ``titles``."""

    def __init__(self, titles):
        self.titles = list(titles)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, endpoint, path, method="GET"):
        self.calls.append((method, path))
        if path.startswith("/json/new"):
            return {"id": "TAB"}
        if path == "/json/list":
            title = self.titles.pop(0) if len(self.titles) > 1 else self.titles[0]
            return [{"id": "OTHER", "title": "New Tab"}, {"id": "TAB", "title": title}]
        return "Target is closing"


URL = "https://venue.example/cal.pl?Op=ShowIt&Name=Community"


@pytest.fixture
def devtools(monkeypatch):
    monkeypatch.setattr(uc_browser.time, "sleep", lambda _s: None)

    def install(titles):
        fake = FakeDevtools(titles)
        monkeypatch.setattr(uc_browser, "_devtools", fake)
        return fake

    return install


def test_an_unattached_page_is_through_once_its_check_is_gone(devtools):
    fake = devtools(["venue.example/cal.pl?Op=ShowIt", "Just a moment...", "Venue calendar"])

    assert uc_browser.pass_check_unattached("http://b", URL, 30) == (True, "Venue calendar")
    # The whole address reaches the browser: a bare `&` would end it early.
    assert fake.calls[0] == ("PUT", "/json/new?https://venue.example/cal.pl?Op=ShowIt%26Name=Community")
    assert fake.calls[-1] == ("GET", "/json/close/TAB")


def test_a_check_that_never_finishes_is_not_through_and_its_tab_is_closed(devtools):
    fake = devtools(["Just a moment..."])

    assert uc_browser.pass_check_unattached("http://b", URL, 5) == (False, "Just a moment...")
    assert fake.calls[-1] == ("GET", "/json/close/TAB")


def test_a_page_still_showing_its_address_has_not_loaded(devtools):
    devtools(["venue.example/cal.pl?Op=ShowIt&Name=Community"])

    assert uc_browser.pass_check_unattached("http://b", URL, 4)[0] is False


def test_a_slow_page_gets_its_own_clock_and_is_not_called_a_bot_check(devtools):
    """Measured on ra-sf: its title is `ra.co` while it loads. One run took more
    than its 15 s bot-check wait to render and was reported blocked by a check
    that was never on the page."""
    from event_harvester.sources.web_utils import SourceBlocked, SourceDeviated

    slow = ["venue.example"] * 20 + ["Venue calendar"]
    devtools(slow)
    assert uc_browser.pass_check_unattached("http://b", URL, 15) == (True, "Venue calendar")

    devtools(["venue.example"])
    through, title = uc_browser.pass_check_unattached("http://b", URL, 15)
    assert not through
    never_rendered = uc_browser.not_through(title, 15)
    assert isinstance(never_rendered, SourceDeviated) and not isinstance(never_rendered, SourceBlocked)
    assert isinstance(uc_browser.not_through("Just a moment...", 15), SourceBlocked)


def test_a_configured_service_window_wins_and_nothing_else_is_tried(monkeypatch):
    """Once the service owns the native profile, a caller that fell back to
    the open port or to launching would start a second browser on it."""
    probed: list[int] = []
    monkeypatch.setattr(uc_browser, "_port_open", lambda port: probed.append(port) or True)

    method, kwargs = uc_browser.native_route("http://127.0.0.1:4010", "native")

    assert method == "service:native"
    assert (kwargs["service"], kwargs["window"]) == ("http://127.0.0.1:4010", "native")
    assert kwargs["label"].startswith("event_tool:")
    assert probed == []


def test_without_a_service_window_the_open_browser_is_named_else_chrome_is_launched(monkeypatch):
    monkeypatch.setattr(uc_browser, "_port_open", lambda port: True)
    assert uc_browser.native_route("http://127.0.0.1:4010", "") == (
        "native:open-browser", {"cdp_endpoint": "http://127.0.0.1:9222", "label": "event_tool"},
    )
    monkeypatch.setattr(uc_browser, "_port_open", lambda port: False)
    assert uc_browser.native_route("", "") == ("native", {"native_chrome": True})


def test_no_browser_is_handed_out_for_a_page_that_did_not_get_through(controller, monkeypatch):
    """The explore tool and the CLI hold one browser on one address. Attaching
    to a page whose check is still running is what keeps it from finishing."""
    from event_harvester.sources.web_utils import SourceBlocked

    monkeypatch.setattr(uc_browser, "_port_open", lambda port: True)
    monkeypatch.setattr(
        uc_browser, "pass_check_unattached", lambda endpoint, url, wait_s: (False, "Just a moment..."),
    )
    with pytest.raises(SourceBlocked):
        uc_browser.native_browser("https://venue.example/", 5)
    assert controller.launched == 0

    monkeypatch.setattr(
        uc_browser, "pass_check_unattached", lambda endpoint, url, wait_s: (True, "Venue"),
    )
    with uc_browser.native_browser("https://venue.example/", 5) as uc:
        assert uc._uc.kwargs == {"cdp_endpoint": "http://127.0.0.1:9222", "label": "event_tool"}


class FakeServiceClient:
    """The UC service's quiet-window calls, recorded in order."""

    calls: list[tuple] = []
    titles: list[str] = []
    busy = False
    pages: list[dict] = []
    knows_lifetime = True

    def windows(self):
        return {"native": {"pages": type(self).pages}}

    def set_page_lifetime(self, window, target_id, client_id, lifetime):
        if not type(self).knows_lifetime:
            from uc_browser.service.errors import ServiceError

            raise ServiceError("http_404", "Not Found", 404)
        type(self).calls.append(("lifetime", target_id, client_id, lifetime))

    def __init__(self, url):
        self.url = url

    def register_client(self, window, label=None, pid=None, *, unattached=False):
        type(self).calls.append(("register", window, label, unattached))
        if type(self).busy:
            from uc_browser.service.errors import ServiceError

            raise ServiceError("busy", "window native is held by explore:1", 409)
        return "C1"

    def start_window(self, window):
        type(self).calls.append(("start", window))
        return {}

    def new_page(self, window, client_id=None, url=None, wait_ms=None):
        type(self).calls.append(("new_page", window, client_id, url))
        return "T1"

    def page_info(self, window, target_id):
        titles = type(self).titles
        return {"target_id": target_id, "title": titles.pop(0) if len(titles) > 1 else titles[0]}

    def close_page(self, window, target_id):
        type(self).calls.append(("close_page", target_id))

    def close_client(self, client_id):
        type(self).calls.append(("close_client", client_id))


@pytest.fixture
def service(monkeypatch):
    import uc_browser.service.client as real

    FakeServiceClient.calls, FakeServiceClient.busy = [], False
    FakeServiceClient.pages, FakeServiceClient.knows_lifetime = [], True
    monkeypatch.setattr(real, "ServiceClient", FakeServiceClient)
    monkeypatch.setattr(uc_browser.time, "sleep", lambda _s: None)
    return FakeServiceClient


SERVICE_ROUTE = {"service": "http://svc", "window": "native", "label": "event_tool:1:2"}


def test_a_service_window_loads_the_page_in_a_quiet_reservation_and_releases_it(service):
    service.titles = ["", "Just a moment...", "Venue calendar"]

    assert uc_browser.prepare_native(SERVICE_ROUTE, URL, 30) == (True, "Venue calendar")
    assert service.calls == [
        ("register", "native", "event_tool:1:2", True),
        ("start", "native"),
        ("new_page", "native", "C1", URL),
        ("close_page", "T1"),
        ("close_client", "C1"),
    ]


def test_a_page_not_through_in_the_service_window_still_releases_the_reservation(service):
    service.titles = ["Just a moment..."]

    assert uc_browser.prepare_native(SERVICE_ROUTE, URL, 5) == (False, "Just a moment...")
    assert service.calls[-2:] == [("close_page", "T1"), ("close_client", "C1")]


def test_a_busy_service_window_is_an_error_not_a_reason_to_attach(service):
    from uc_browser.service.errors import ServiceError

    service.busy = True
    with pytest.raises(ServiceError, match="held by explore:1"):
        uc_browser.prepare_native(SERVICE_ROUTE, URL, 5)
    assert service.calls == [("register", "native", "event_tool:1:2", True)]


def test_a_kept_page_outlives_the_reservation_and_replaces_last_nights(service):
    """One kept page per site: the new one is kept first, then the old one of
    the same site is closed. Another site's kept page and the blank tab stay."""
    service.titles = ["Venue calendar"]
    service.pages = [
        {"target_id": "BLANK", "url": "about:blank", "holder": None},
        {"target_id": "OLD", "url": "https://www.venue.example/cal.pl?Op=ShowIt", "holder": "window"},
        {"target_id": "OTHER", "url": "https://elsewhere.example/", "holder": "window"},
        {"target_id": "T1", "url": URL, "holder": "window"},
    ]

    assert uc_browser.prepare_native(SERVICE_ROUTE, URL, 30, keep=True) == (True, "Venue calendar")
    assert service.calls[3:] == [
        ("lifetime", "T1", "C1", "window"),
        ("close_page", "OLD"),
        ("close_client", "C1"),
    ]


def test_a_page_that_did_not_get_through_is_never_kept(service):
    service.titles = ["Just a moment..."]

    assert uc_browser.prepare_native(SERVICE_ROUTE, URL, 5, keep=True)[0] is False
    assert service.calls[3:] == [("close_page", "T1"), ("close_client", "C1")]


def test_a_service_that_cannot_keep_pages_costs_only_the_tab(service):
    service.titles, service.knows_lifetime = ["Venue calendar"], False

    assert uc_browser.prepare_native(SERVICE_ROUTE, URL, 30, keep=True) == (True, "Venue calendar")
    assert service.calls[3:] == [("close_page", "T1"), ("close_client", "C1")]
