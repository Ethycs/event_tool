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
