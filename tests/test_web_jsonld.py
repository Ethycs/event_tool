"""JSON-LD mode: which of a page's published events become messages."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from event_harvester.sources.web_modes import jsonld
from event_harvester.sources.web_utils import SourceBlocked, SourceDeviated

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
SRC = {"name": "venue", "url": "https://venue.example/events/"}


def _event(name, start, end="", url=""):
    return {
        "@type": "MusicEvent", "name": name, "startDate": start, "endDate": end,
        "url": url or f"https://tickets.example/{name.replace(' ', '-')}",
        "location": {"name": "The Hall", "address": {"streetAddress": "1 Main St",
                                                    "addressLocality": "San Francisco"}},
    }


def _page(*events, body="<h1>Events</h1>"):
    graph = json.dumps({"@context": "https://schema.org", "@graph": list(events)})
    return f'<html><body>{body}<script type="application/ld+json">{graph}</script></body></html>'


class _Response:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def serve(monkeypatch):
    def _serve(text, status=200):
        monkeypatch.setattr(jsonld.requests, "get", lambda *a, **k: _Response(text, status))
    return _serve


def _run(src=SRC):
    return jsonld._do_jsonld_inner(src, now=NOW)


def test_every_current_event_is_a_message_with_its_facts(serve):
    serve(_page(
        _event("First Show", "2026-11-28T20:00:00-08:00", "2026-11-28T23:00:00-08:00"),
        _event("Second Show", "2026-12-05T21:00:00-08:00"),
    ))
    first, second = _run()
    assert first["content"].splitlines()[:5] == [
        "First Show", "Date: 2026-11-28", "Time: 20:00 - 23:00",
        "Venue: The Hall, 1 Main St, San Francisco",
        "URL: https://tickets.example/First-Show",
    ]
    assert first["timestamp"] == "2026-11-28T20:00:00-08:00"
    assert first["id"] != second["id"]


def test_events_that_have_ended_are_dropped(serve):
    serve(_page(
        _event("Last Month", "2026-10-26T19:00:00-07:00", "2026-10-26T20:15:00-07:00"),
        _event("Next Week", "2026-12-01T20:00:00-08:00"),
    ))
    assert [m["content"].splitlines()[0] for m in _run()] == ["Next Week"]


def test_an_event_under_way_is_kept(serve):
    # Started three hours ago, ends in an hour.
    serve(_page(_event("Tonight", "2026-11-24T09:00:00+00:00", "2026-11-24T13:00:00+00:00")))
    assert len(_run()) == 1


def test_a_multi_day_event_given_as_bare_dates_runs_through_its_last_day(serve):
    # 12:00 UTC on the 24th is 04:00 Pacific, inside the event's final day.
    serve(_page(_event("Faire", "2026-11-22", "2026-11-24")))
    assert len(_run()) == 1


def test_forward_days_caps_the_window_only_when_configured(serve):
    serve(_page(
        _event("Soon", "2026-12-01T20:00:00-08:00"),
        _event("Next Spring", "2027-03-12T21:00:00-08:00"),
    ))
    assert len(_run()) == 2
    assert len(_run({**SRC, "forward_days": 30})) == 1


def test_a_page_whose_events_have_all_ended_is_empty_not_deviated(serve):
    serve(_page(_event("Over", "2026-09-25", "2026-09-27")))
    assert _run() == []


def test_a_page_with_no_event_items_is_a_deviation(serve):
    serve('<html><body><p>Welcome to the venue.</p>'
          '<script type="application/ld+json">{"@type": "Organization", "name": "V"}</script>'
          '</body></html>')
    with pytest.raises(SourceDeviated) as e:
        _run()
    assert not isinstance(e.value, SourceBlocked)
    assert "Welcome to the venue." in str(e.value)


def test_a_bot_check_is_blocked_whatever_the_status(serve):
    serve("<html><head><title>Just a moment...</title></head><body>"
          "Checking your browser. cf-challenge</body></html>", status=403)
    with pytest.raises(SourceBlocked):
        _run()


def test_an_http_error_is_raised_not_read_as_an_empty_listing(serve):
    serve("<html><body>Server error</body></html>", status=500)
    with pytest.raises(RuntimeError):
        _run()
