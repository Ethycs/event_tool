"""Tests for _carry_calendar_dates: calendar rows keep their own date.

Regression for erobay 2026-09-02 — the LLM re-dated the recurring
'Playland at the Cat Club (every Wednesday)' from today to next Wednesday,
so today's occurrence vanished from the feed. The row states its date; the
extractor must not move it.
"""

from __future__ import annotations

from event_harvester.pipeline.analysis import _carry_calendar_dates, _norm_title


def _msg(title: str, date: str, url: str = "", platform: str = "web") -> dict:
    return {
        "platform": platform,
        "channel": url,
        "content": f"{title}\ndate: {date}\ntime: 7:00pm\nlocation: Somewhere",
    }


def test_norm_title_strips_asides_and_punctuation():
    assert _norm_title("Playland at the Cat Club (every Wednesday)") == "playland at the cat club"
    assert _norm_title("Go Topless! Subs Meetup [Oakland]") == "go topless subs meetup"


def test_recurring_event_snaps_to_soonest_upcoming_instance():
    # Two future instances of one weekly event; the LLM picked the later one.
    events = [{"title": "Playland at the Cat Club", "date": "2099-01-12"}]
    messages = [
        _msg("Playland at the Cat Club (every Wednesday)", "2099-01-05"),
        _msg("Playland at the Cat Club (every Wednesday)", "2099-01-12"),
    ]
    changed = _carry_calendar_dates(events, messages)
    assert changed == 1
    assert events[0]["date"] == "2099-01-05"  # soonest upcoming, not the LLM's 01-12


def test_lone_event_snaps_to_its_row_date():
    events = [{"title": "Go Topless Subs Meetup", "date": "2099-02-20"}]
    messages = [_msg("Go Topless! Subs Meetup [Oakland]", "2099-02-13")]
    assert _carry_calendar_dates(events, messages) == 1
    assert events[0]["date"] == "2099-02-13"


def test_correct_date_is_left_alone():
    events = [{"title": "SF Munch", "date": "2099-03-01"}]
    messages = [_msg("SF Munch", "2099-03-01")]
    assert _carry_calendar_dates(events, messages) == 0
    assert events[0]["date"] == "2099-03-01"


def test_link_match_when_title_differs():
    events = [{"title": "Rewritten Title", "date": "2099-05-08",
               "link": "https://buytickets.at/voxbodystudio"}]
    messages = [_msg("Original", "2099-05-01", url="https://buytickets.at/voxbodystudio/2392597")]
    assert _carry_calendar_dates(events, messages) == 1
    assert events[0]["date"] == "2099-05-01"


def test_non_web_messages_are_ignored():
    # A Discord message with a date: line must NOT drive calendar date-carry;
    # chat extraction's date reasoning is load-bearing there.
    events = [{"title": "Standup", "date": "2099-04-10"}]
    messages = [_msg("Standup", "2099-04-01", platform="discord")]
    assert _carry_calendar_dates(events, messages) == 0
    assert events[0]["date"] == "2099-04-10"


def test_unmatched_event_is_untouched():
    events = [{"title": "Unrelated Thing", "date": "2099-06-01"}]
    messages = [_msg("Completely Other Event", "2099-06-05")]
    assert _carry_calendar_dates(events, messages) == 0
    assert events[0]["date"] == "2099-06-01"
