"""ICS handling in the `web test` / `web add` diagnostic path.

Regression coverage: the harvest path branched to ICS before opening a
browser, but the diagnostic path did not, so `event-harvester web test
webcal://...` died with

    Page.goto: net::ERR_ABORTED at webcal://...

which says nothing about the real reason (an iCalendar feed is not a
browsable page).
"""

import pytest

from event_harvester.cli.commands.web import (
    _diagnose_ics,
    _is_ics_url,
    _run_url_diagnostics,
)


@pytest.mark.parametrize(
    "url",
    [
        "webcal://cdn.dnalounge.com/calendar/dnalounge.ics",
        "webcals://example.com/feed.ics",
        "https://example.com/calendar.ics",
        "https://example.com/calendar.ics/",
        "webcal://example.com/no-suffix",
    ],
)
def test_is_ics_url_true(url):
    assert _is_ics_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://tockify.com/mothership/agenda",
        "https://lu.ma/discover",
        "https://example.com/ics-events",       # 'ics' in path, not a feed
        "https://example.com/calendar.icst",
    ],
)
def test_is_ics_url_false(url):
    assert _is_ics_url(url) is False


def test_diagnostics_short_circuits_without_a_browser(monkeypatch):
    """An ICS URL must never reach the browser path."""
    calls = {}

    def fake_inner(src):
        calls["src"] = src
        return [
            {"timestamp": "2026-07-24T21:30:00+00:00", "content": "Nightbreed: Horror Rave"},
            {"timestamp": "2026-07-25T14:00:00+00:00", "content": "Big Muscle Party"},
        ]

    monkeypatch.setattr(
        "event_harvester.sources.web_modes.ics._do_ics_inner", fake_inner
    )
    # If the browser path were taken this would blow up rather than skip.
    monkeypatch.setattr(
        "event_harvester.sources.uc_browser.UCBrowser",
        lambda *a, **k: pytest.fail("UCBrowser must not be constructed for an ICS URL"),
    )

    url = "webcal://cdn.dnalounge.com/calendar/dnalounge.ics"
    assert _run_url_diagnostics(url)["mode"] == "ics"
    assert calls["src"]["url"] == url


def test_diagnose_ics_returns_none_when_feed_yields_nothing(monkeypatch):
    """_do_ics_inner returns [] (it logs and swallows); report, don't claim success."""
    monkeypatch.setattr(
        "event_harvester.sources.web_modes.ics._do_ics_inner", lambda src: []
    )
    assert _diagnose_ics("https://example.com/empty.ics") is None
