"""Tests for ICS mode (sources.web_modes.ics).

The parser must (1) convert timed events out of the feed's UTC into the venue's
local zone before taking the date/time — a 6pm Pacific event stored as 01:00 UTC
must not land on the next day at 1am; (2) drop the scaffolding that
spreadsheet-backed Google Calendars leak (``#REF!``, ``Event Name``, and rows
whose date defaulted to the 1970 epoch); (3) honor the source's forward window;
and (4) emit the calendar-row format the analysis bypass consumes, with a real
per-event link (falling back to the source's ``link``).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from event_harvester.sources.web_modes import ics
from event_harvester.pipeline.analysis import _calendar_rows_to_events

LA = ZoneInfo("America/Los_Angeles")


# ── timezone / value helpers ──────────────────────────────────────────────

def test_localize_converts_utc_to_pacific_and_crosses_midnight():
    # 01:00 UTC on the 5th is 18:00 PDT on the 4th — the day must move back.
    dt = datetime(2026, 9, 5, 1, 0, tzinfo=timezone.utc)
    local = ics._localize(dt, LA)
    assert ics._start_date(local) == date(2026, 9, 4)
    assert ics._start_time(local) == "18:00"


def test_localize_leaves_naive_datetime_alone():
    dt = datetime(2026, 9, 5, 18, 30)  # already local, no zone
    local = ics._localize(dt, LA)
    assert ics._start_date(local) == date(2026, 9, 5)
    assert ics._start_time(local) == "18:30"


def test_all_day_date_has_no_time_and_no_shift():
    d = date(2026, 9, 5)
    local = ics._localize(d, LA)
    assert ics._start_date(local) == d
    assert ics._start_time(local) is None


# ── full parse, with a mocked feed ────────────────────────────────────────

def _ics_bytes(vevents: str) -> bytes:
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
            + vevents + "END:VCALENDAR\r\n").encode()


def _vevent(summary: str, dtstart_line: str, *, uid: str, url: str = "",
            location: str = "") -> str:
    lines = [f"BEGIN:VEVENT", f"UID:{uid}", f"SUMMARY:{summary}", dtstart_line]
    if location:
        lines.append(f"LOCATION:{location}")
    if url:
        lines.append(f"URL:{url}")
    lines.append("END:VEVENT")
    return "\r\n".join(lines) + "\r\n"


class _Resp:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        pass


@pytest.fixture
def patch_get(monkeypatch):
    def _install(payload: bytes):
        monkeypatch.setattr(ics.requests, "get", lambda *a, **k: _Resp(payload))
    return _install


def test_garbage_and_window_are_dropped(patch_get):
    future = (date.today() + timedelta(days=10)).isoformat().replace("-", "")
    past = (date.today() - timedelta(days=5)).isoformat().replace("-", "")
    far = (date.today() + timedelta(days=400)).isoformat().replace("-", "")
    payload = _ics_bytes(
        _vevent("Real Show", f"DTSTART;VALUE=DATE:{future}", uid="a")
        + _vevent("#REF!", f"DTSTART;VALUE=DATE:{future}", uid="b")
        + _vevent("Event Name", f"DTSTART;VALUE=DATE:{future}", uid="c")
        + _vevent("Epoch Junk", "DTSTART:19700101T000000Z", uid="d")
        + _vevent("Old Gig", f"DTSTART;VALUE=DATE:{past}", uid="e")
        + _vevent("Too Far Out", f"DTSTART;VALUE=DATE:{far}", uid="f")
    )
    patch_get(payload)
    src = {"url": "https://cal.example/basic.ics", "name": "venue",
           "link": "https://venue.example/calendar", "forward_days": 60}
    msgs = ics._do_ics_inner(src)
    titles = [m["content"].splitlines()[0] for m in msgs]
    assert titles == ["Real Show"]           # everything else dropped


def test_emitted_rows_carry_link_and_feed_the_bypass(patch_get):
    d1 = (date.today() + timedelta(days=8)).isoformat()
    d2 = (date.today() + timedelta(days=9)).isoformat()
    payload = _ics_bytes(
        _vevent("All Day Fair", f"DTSTART;VALUE=DATE:{d1.replace('-', '')}",
                uid="x", location="The Park")
        + _vevent("Ticketed Gig", f"DTSTART;VALUE=DATE:{d2.replace('-', '')}",
                  uid="y", url="https://tickets.example/gig")
    )
    patch_get(payload)
    src = {"url": "https://cal.example/basic.ics", "name": "venue",
           "link": "https://venue.example/calendar", "forward_days": 60}
    msgs = ics._do_ics_inner(src)
    assert len(msgs) == 2
    # per-event URL wins; otherwise the source link is the fallback, made unique
    # with a per-event #fragment so dedup does not collapse same-link events
    by_uid = {m["id"]: m for m in msgs}
    assert by_uid["ics:venue:x"]["url"].startswith("https://venue.example/calendar#")
    assert by_uid["ics:venue:y"]["url"] == "https://tickets.example/gig"
    assert "date: " + d1 in by_uid["ics:venue:x"]["content"]
    assert "location: The Park" in by_uid["ics:venue:x"]["content"]

    # and the analysis bypass turns them into events, skipping the LLM
    bypass, remaining = _calendar_rows_to_events(msgs)
    assert remaining == []
    titles = {e["title"] for e in bypass}
    assert titles == {"All Day Fair", "Ticketed Gig"}
    fair = next(e for e in bypass if e["title"] == "All Day Fair")
    assert fair["date"] == d1
    assert fair["link"].startswith("https://venue.example/calendar#")


def test_fallback_links_are_distinct_per_event_so_dedup_keeps_them(patch_get):
    # Two events on a feed with no per-event URLs must not collapse into one.
    d1 = (date.today() + timedelta(days=7)).isoformat().replace("-", "")
    d2 = (date.today() + timedelta(days=8)).isoformat().replace("-", "")
    payload = _ics_bytes(
        _vevent("Night One", f"DTSTART;VALUE=DATE:{d1}", uid="one")
        + _vevent("Night Two", f"DTSTART;VALUE=DATE:{d2}", uid="two")
    )
    patch_get(payload)
    src = {"url": "https://cal.example/basic.ics", "name": "venue",
           "link": "https://venue.example/calendar", "forward_days": 60}
    msgs = ics._do_ics_inner(src)
    links = {m["url"] for m in msgs}
    assert len(links) == 2   # distinct fragments


def test_fetch_failure_returns_empty(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(ics.requests, "get", boom)
    assert ics._do_ics_inner({"url": "https://x/y.ics", "name": "v"}) == []
