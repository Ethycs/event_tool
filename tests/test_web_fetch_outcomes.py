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

    def new_page(self):
        return self._context.new_page()


class Harness(list):
    """The URLs whose cooldown was saved, plus the bot-check windows asked for."""

    waits: list[int]


@pytest.fixture
def harness(monkeypatch):
    """A harvest with fake browsers and no writes outside ``tmp_path``."""
    FakeController.launches = []
    FakeController.html_for = staticmethod(lambda kwargs: CONTENT)
    saved = Harness()
    monkeypatch.setattr(uc_browser, "UCBrowser", FakeController)
    monkeypatch.setattr(web_fetch, "_save_cooldown", saved.append)
    waits: list[int] = []

    def wait(page, headless=False, wait_s=15):
        waits.append(wait_s)
        return page.content()

    saved.waits = waits
    monkeypatch.setattr(web_fetch, "_wait_cloudflare", wait)
    monkeypatch.setattr(uc_browser, "_port_open", lambda port: False)
    monkeypatch.setattr(
        uc_browser, "pass_check_unattached",
        lambda endpoint, url, wait_s, **_kw: (True, "a page"),
    )
    monkeypatch.setattr(
        web_fetch, "_do_raw",
        lambda uc, src, page=None, **_kw: [{"id": src["name"], "channel": src["name"]}],
    )
    return saved


def test_attaching_to_an_open_browser_is_logged_as_its_own_method(harness, monkeypatch):
    """A check a person cleared in an open browser held for pages opened after
    it; the same check never completed in a browser launched with the
    automation attached. They must not share a line in the log."""
    from event_harvester.sources import web_access_log

    web_fetch.fetch_web_sources([_raw("erobay", native_chrome=True)])
    monkeypatch.setattr(uc_browser, "_port_open", lambda port: True)
    web_fetch.fetch_web_sources([_raw("erobay", native_chrome=True)])

    lines = [json.loads(line) for line in web_access_log.LOG_FILE.read_text().splitlines()]
    assert [line["method"] for line in lines] == ["native", "native:open-browser"]
    # The open browser is attached to by naming it. Asking the controller for
    # native Chrome there would launch a second browser on a held profile,
    # which it refuses (measured 2026-11-24: every native source failed).
    assert FakeController.launches == [
        {"native_chrome": True},
        {"cdp_endpoint": "http://127.0.0.1:9222", "label": "event_tool"},
    ]
    assert all(line["was"] is None for line in lines)  # two methods, two first sights


def test_a_check_that_does_not_finish_unattached_is_blocked_without_attaching(harness, monkeypatch):
    """Attaching to a page whose check is still running is what stops it from
    finishing, so the harvest must not attach to find out."""
    monkeypatch.setattr(uc_browser, "_port_open", lambda port: True)
    monkeypatch.setattr(
        uc_browser, "pass_check_unattached",
        lambda endpoint, url, wait_s, **_kw: (False, "Just a moment..."),
    )

    assert web_fetch.fetch_web_sources([_raw("erobay", native_chrome=True)]) == []
    assert _states() == {"erobay": "blocked"}
    assert FakeController.launches == []


def test_a_source_whose_check_is_clicked_has_its_own_method_and_a_line_per_click(
    harness, monkeypatch,
):
    """How a check was passed is part of the method: a source this program
    clicks through must not share a state with one that got through unaided,
    and each press is in the log, made by the program that pressed."""
    from event_harvester.sources import native_pointer, native_window, web_access_log

    asked: list = []
    pressed: list = []

    def load(endpoint, url, wait_s, on_check=None):
        asked.append((url, on_check is not None))
        if on_check is not None:
            for second in range(1, 8):  # a check that is up for seven reads
                on_check(second, "Just a moment...")
        return True, "a page"

    def press(pid, offset, expect_title):
        pressed.append((pid, offset, expect_title))
        return 292, 443

    monkeypatch.setattr(uc_browser, "_port_open", lambda port: True)
    monkeypatch.setattr(uc_browser, "pass_check_unattached", load)
    monkeypatch.setattr(native_window, "browser_pid", lambda port: 4242)
    monkeypatch.setattr(native_pointer, "click", press)

    web_fetch.fetch_web_sources([
        _raw("erobay", native_chrome=True, bot_check_click=[48, 423]),
        _raw("venue", native_chrome=True),
    ])

    assert asked == [("https://erobay.example/", True), ("https://venue.example/", False)]
    assert pressed == [(4242, (48, 423), "Just a moment...")]  # once, at its second
    lines = [json.loads(line) for line in web_access_log.LOG_FILE.read_text().splitlines()]
    assert [(line["source"], line["method"], line["event"]) for line in lines] == [
        ("erobay", "native:open-browser+click", "click"),
        ("erobay", "native:open-browser+click", "change"),
        ("venue", "native:open-browser", "change"),
    ]
    assert "292,443" in lines[0]["detail"]
    run = json.loads(web_fetch._REPORT_FILE.read_text())["runs"][-1]
    assert run["sources"]["erobay"]["method"] == "native:open-browser+click"


def test_a_click_that_cannot_be_made_is_logged_and_does_not_end_the_wait(harness, monkeypatch):
    from event_harvester.sources import native_pointer, native_window, web_access_log

    def load(endpoint, url, wait_s, on_check=None):
        on_check(6, "Just a moment...")
        return False, "Just a moment..."

    def refuse(pid, offset, expect_title):
        raise native_pointer.ClickRefused("another window lies over the point 292,443")

    monkeypatch.setattr(uc_browser, "_port_open", lambda port: True)
    monkeypatch.setattr(uc_browser, "pass_check_unattached", load)
    monkeypatch.setattr(native_window, "browser_pid", lambda port: 4242)
    monkeypatch.setattr(native_pointer, "click", refuse)

    web_fetch.fetch_web_sources([_raw("erobay", native_chrome=True, bot_check_click=[48, 423])])

    lines = [json.loads(line) for line in web_access_log.LOG_FILE.read_text().splitlines()]
    assert [line["event"] for line in lines] == ["click", "change"]
    assert "not clicked" in lines[0]["detail"] and "lies over" in lines[0]["detail"]
    assert lines[1]["state"] == "blocked"


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


def test_a_harvest_logs_the_change_in_a_sources_access_state(harness):
    """Blocked one night, cleared by the next run: both changes are on record,
    and the second says how long the block had held."""
    from event_harvester.sources import web_access_log

    FakeController.html_for = staticmethod(lambda kwargs: CHALLENGE)
    web_fetch.fetch_web_sources([_raw("erobay", native_chrome=True)])
    FakeController.html_for = staticmethod(lambda kwargs: CONTENT)
    web_fetch.fetch_web_sources([_raw("erobay", native_chrome=True)])

    lines = [json.loads(line) for line in web_access_log.LOG_FILE.read_text().splitlines()]
    assert [(line["was"], line["state"]) for line in lines] == [(None, "blocked"), ("blocked", "ok")]
    assert lines[1]["held_max_s"] is not None


def test_a_standard_bot_check_goes_to_native_and_each_method_keeps_its_own_state(harness):
    """Cleared on the retry: one message, state ok, and two launches to get it.
    The log holds both truths, because a check passed by one method says
    nothing about the other: standard was blocked, native was fine."""
    from event_harvester.sources import web_access_log

    FakeController.html_for = staticmethod(
        lambda kwargs: CONTENT if kwargs.get("native_chrome") else CHALLENGE,
    )

    out = web_fetch.fetch_web_sources([_raw("venue")])

    assert [m["id"] for m in out] == ["venue"]
    assert FakeController.launches == [{}, {"native_chrome": True}]
    assert _states() == {"venue": "ok"}
    lines = [json.loads(line) for line in web_access_log.LOG_FILE.read_text().splitlines()]
    assert [(line["method"], line["state"]) for line in lines] == [
        ("standard", "blocked"), ("native", "ok"),
    ]
    run = json.loads(web_fetch._REPORT_FILE.read_text())["runs"][-1]
    assert run["sources"]["venue"]["method"] == "native"


def test_a_source_can_give_a_person_longer_to_clear_its_check(harness):
    """Fifteen seconds is not long enough to reach a KVM. The window is the
    source's to set, and it applies in the browser the harvest itself drives."""
    web_fetch.fetch_web_sources([
        _raw("erobay", native_chrome=True, bot_check_wait_s=120),
        _raw("other", native_chrome=True),
    ])

    assert harness.waits == [120, 15]


def test_the_native_browser_opens_only_once_the_standard_one_has_closed(harness, monkeypatch):
    """Two headed browsers at once put one window over the other, and a bot
    check is not passed on a page that is not being drawn."""
    order: list[str] = []

    def kind(controller) -> str:
        return "native" if controller.kwargs.get("native_chrome") else "standard"

    def enter(self):
        order.append(f"{kind(self)} opens")
        return self

    def leave(self, *_a):
        time.sleep(0.2)  # a standard browser still closing when a native one could start
        order.append(f"{kind(self)} closes")

    monkeypatch.setattr(FakeController, "__enter__", enter)
    monkeypatch.setattr(FakeController, "__exit__", leave)

    out = web_fetch.fetch_web_sources([_raw("checked", native_chrome=True), _raw("plain")])

    assert sorted(m["id"] for m in out) == ["checked", "plain"]
    assert order == ["standard opens", "standard closes", "native opens", "native closes"]


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


# ── Deviation: the handler knows what page it was written for ─────────


class SignalPage(FakePage):
    """A page whose one diagnostic ``evaluate`` returns ``signals``."""

    def __init__(self, signals: dict):
        super().__init__("<html><body>not a listing</body></html>")
        self.signals = signals

    def evaluate(self, *_a, **_kw):
        return self.signals

    def wait_for_load_state(self, *_a, **_kw):
        pass


class Controls:
    def detect_all(self, page):
        return {}

    def dismiss_cookies(self, page):
        return False

    def close_modal(self, page):
        return False


def _calendar(page, monkeypatch):
    from event_harvester.sources.web_modes import calendar

    monkeypatch.setattr(calendar, "_scroll_for_seconds", lambda *a, **k: None)
    monkeypatch.setattr(calendar, "_extract_event_links", lambda page: [])
    src = {"url": "https://venue.example/events", "name": "venue", "mode": "calendar"}
    return calendar._do_calendar_inner(Controls(), src, 1000, 5, page=page)


def test_a_calendar_page_without_a_listing_deviates_and_says_what_it_is(monkeypatch):
    """It used to be scraped as raw text: one message, every night, of whatever
    stood where the calendar should be."""
    from event_harvester.sources.web_utils import SourceBlocked, SourceDeviated

    page = SignalPage({"text_sample": "Welcome back", "text_len": 40, "links": 2,
                       "password_inputs": 1})
    with pytest.raises(SourceDeviated) as raised:
        _calendar(page, monkeypatch)

    assert not isinstance(raised.value, SourceBlocked)
    assert raised.value.kind == "login_wall"
    assert "found 0" in str(raised.value)
    assert page.closed


def test_a_calendar_page_showing_a_bot_check_is_blocked(monkeypatch):
    from event_harvester.sources.web_utils import SourceBlocked

    page = SignalPage({"text_sample": "Just a moment... verify you are human",
                       "text_len": 60, "links": 0})
    with pytest.raises(SourceBlocked):
        _calendar(page, monkeypatch)


def test_a_deviated_source_is_reported_and_emits_nothing(harness, monkeypatch):
    from event_harvester.sources.web_utils import SourceDeviated

    def calendar(uc, src, *a, **k):
        raise SourceDeviated("a calendar listing", "login_wall", "a password input is present")

    monkeypatch.setattr(web_fetch, "_do_calendar", calendar)
    src = {"url": "https://venue.example/events", "name": "venue", "mode": "calendar"}

    assert web_fetch.fetch_web_sources([src]) == []
    run = json.loads(web_fetch._REPORT_FILE.read_text())["runs"][-1]
    assert run["sources"]["venue"]["state"] == "deviated"
    assert "login_wall" in run["sources"]["venue"]["detail"]
    assert harness == []
