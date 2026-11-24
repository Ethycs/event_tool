"""What `fetch_web_sources` says happened to each source.

The nightly total stayed between 912 and 1021 messages while one source
returned a Cloudflare page as its only "event" every night and two nights out
of seven were lost whole to a single hung source (2026-11-17 to 2026-11-23).
These tests pin the properties that would have made both visible and bounded.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from event_harvester.sources import uc_browser, web_fetch
from event_harvester.sources.uc_browser import WebBrowser
from event_harvester.sources.web_utils import _fetch_event_details_parallel

CHALLENGE = "<html><title>Just a moment...</title>Performing security verification</html>"
CONTENT = "<html><body>doors at eight</body></html>"


class FakePage:
    def __init__(self, html: str = CONTENT):
        self.html = html
        self.closed = False

    def goto(self, url, **_kw):
        pass

    def wait_for_timeout(self, _ms):
        pass

    def content(self):
        return self.html

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, html):
        self._html = html

    def new_page(self):
        return FakePage(self._html)


class FakeController:
    """Stands in for ``UCBrowser``. ``html_for`` decides what each launch serves."""

    launches: list[dict] = []
    html_for = staticmethod(lambda kwargs: CONTENT)

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.timeout_ms = 1000
        self._stealth_plugin = None
        self._context = FakeContext(type(self).html_for(kwargs))

    def __enter__(self):
        type(self).launches.append(self.kwargs)
        return self

    def __exit__(self, *_a):
        pass

    def open(self, url, **_kw):
        return self._context.new_page()


@pytest.fixture
def harness(monkeypatch):
    """A harvest with fake browsers and no writes outside ``tmp_path``."""
    FakeController.launches = []
    FakeController.html_for = staticmethod(lambda kwargs: CONTENT)
    saved: list[str] = []
    monkeypatch.setattr(uc_browser, "UCBrowser", FakeController)
    monkeypatch.setattr(web_fetch, "_save_cooldown", saved.append)
    monkeypatch.setattr(web_fetch, "_wait_cloudflare", lambda page, headless=False: page.content())
    monkeypatch.setattr(
        web_fetch, "_do_raw",
        lambda uc, src, page=None, **_kw: [{"id": src["name"], "channel": src["name"]}],
    )
    return saved


def _states() -> dict[str, str]:
    run = json.loads(web_fetch._REPORT_FILE.read_text())["runs"][-1]
    return {name: entry["state"] for name, entry in run["sources"].items()}


def _raw(name: str, **extra) -> dict:
    return {"url": f"https://{name}.example/", "name": name, "mode": "raw", **extra}


def test_a_bot_check_in_native_chrome_is_blocked_not_scraped(harness):
    FakeController.html_for = staticmethod(lambda kwargs: CHALLENGE)

    out = web_fetch.fetch_web_sources([_raw("erobay", native_chrome=True)])

    assert out == []
    assert _states() == {"erobay": "blocked"}
    assert harness == []  # a blocked source must not start its cooldown


def test_a_standard_bot_check_goes_to_native_and_its_final_state_is_reported(harness):
    """Cleared on the retry: one message, state ok, and two launches to get it."""
    FakeController.html_for = staticmethod(
        lambda kwargs: CONTENT if kwargs.get("native_chrome") else CHALLENGE,
    )

    out = web_fetch.fetch_web_sources([_raw("venue")])

    assert [m["id"] for m in out] == ["venue"]
    assert FakeController.launches == [{}, {"native_chrome": True}]
    assert _states() == {"venue": "ok"}


def test_a_hung_source_costs_only_itself_and_what_was_queued_behind_it(harness, monkeypatch):
    release = threading.Event()

    def raw(uc, src, page=None, **_kw):
        if src["name"] == "stuck":
            release.wait(30)
        return [{"id": src["name"], "channel": src["name"]}]

    monkeypatch.setattr(web_fetch, "_do_raw", raw)
    monkeypatch.setitem(
        web_fetch.HTTP_MODES, "ics",
        lambda src, **_kw: [{"id": "feed", "channel": "feed"}],
    )
    sources = [
        {"url": "https://feed.example/x.ics", "name": "feed", "mode": "ics"},
        _raw("first", native_chrome=True),
        _raw("stuck", native_chrome=True, deadline_s=0.2),
        _raw("behind", native_chrome=True),
        _raw("elsewhere"),
    ]

    started = time.monotonic()
    out = web_fetch.fetch_web_sources(sources)
    waited = time.monotonic() - started

    try:
        assert waited < 10
        assert sorted(m["id"] for m in out) == ["elsewhere", "feed", "first"]
        assert _states() == {
            "feed": "ok", "first": "ok", "elsewhere": "ok",
            "stuck": "hung", "behind": "skipped",
        }
    finally:
        release.set()

    # The stuck call returns after the run was reported. Its late result must
    # not be counted anywhere, or the report and the messages would disagree.
    for thread in threading.enumerate():
        if thread.name == "web-native":
            thread.join(5)
    assert sorted(m["id"] for m in out) == ["elsewhere", "feed", "first"]
    assert "https://stuck.example/" not in harness
    assert "https://behind.example/" not in harness


def test_a_source_below_its_floor_is_reported_low(harness, monkeypatch):
    monkeypatch.setitem(
        web_fetch.HTTP_MODES, "ics", lambda src, **_kw: [{"id": "one", "channel": "cal"}],
    )
    src = {"url": "https://cal.example/x.ics", "name": "cal", "mode": "ics",
           "expect_min_events": 20}

    assert len(web_fetch.fetch_web_sources([src])) == 1
    assert _states() == {"cal": "low"}


def test_a_failing_source_and_an_empty_one_are_told_apart(harness, monkeypatch):
    def ics(src, **_kw):
        if src["name"] == "broken":
            raise RuntimeError("feed returned HTTP 500")
        return []

    monkeypatch.setitem(web_fetch.HTTP_MODES, "ics", ics)
    sources = [
        {"url": "https://a.example/x.ics", "name": "broken", "mode": "ics"},
        {"url": "https://b.example/x.ics", "name": "quiet", "mode": "ics"},
    ]

    assert web_fetch.fetch_web_sources(sources) == []
    assert _states() == {"broken": "failed", "quiet": "empty"}


def test_a_detail_page_still_showing_a_bot_check_yields_nothing(harness):
    FakeController.html_for = staticmethod(lambda kwargs: CHALLENGE)
    link = {"url": "https://e.example/1"}

    with WebBrowser() as uc:
        results = _fetch_event_details_parallel(uc, [link], timeout_ms=1000)

    assert results == [(link, None)]
