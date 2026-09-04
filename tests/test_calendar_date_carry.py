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
        "channel": url[:120],
        "url": url,  # full, untruncated event URL (calcium messages carry this)
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


def test_open_link_is_carried_from_the_row_url():
    # The LLM leaves erobay events with a useless link; the row's own event
    # URL should become the card's open link.
    full = "https://erobay.com/calendar/Calcium40.pl?ID=99&Date=2099/2/13&Op=PopupWindow"
    events = [{"title": "Go Topless Subs Meetup", "date": "2099-02-13", "link": "https://erobay.com"}]
    messages = [_msg("Go Topless! Subs Meetup [Oakland]", "2099-02-13", url=full)]
    _carry_calendar_dates(events, messages)
    assert events[0]["link"] == full


def test_title_match_beats_a_wrong_link_to_another_row():
    # Regression for erobay 2026-09-03: the LLM gave 'House Party' the link of a
    # sibling 'Wild Jam' row (batch cross-contamination). The event then matched
    # BOTH its own row (by title) and Wild Jam's row (by that bad link), and the
    # soonest-upcoming pick snapped it onto Wild Jam's earlier date + URL. Title
    # must win, so it lands on its own row.
    wild_jam_url = "https://forbiddentickets.com/events/afterdark/9ebf4f4b7b"
    house_url = "https://forbiddentickets.com/events/stopgap/bf9c203bc2"
    events = [{
        "title": "House Party - On Every First Friday!",
        "date": "2099-09-03",           # the LLM's wrong (Wild Jam) date
        "link": wild_jam_url,           # the LLM's wrong (Wild Jam) link
    }]
    messages = [
        _msg("Wild Jam - A Primal Play Workshop", "2099-09-03", url=wild_jam_url),
        _msg("House Party - On Every First Friday!", "2099-09-04", url=house_url),
    ]
    _carry_calendar_dates(events, messages)
    assert events[0]["date"] == "2099-09-04"   # its own row, not Wild Jam's 09-03
    assert events[0]["link"] == house_url      # its own URL, not the afterdark one


def test_link_only_fallback_when_title_was_rewritten():
    # The link fallback must still work when the LLM rewrote the title so it no
    # longer matches any row (the House of Braids / external-ticket case).
    events = [{"title": "Totally Rewritten Name", "date": "2099-05-08",
               "link": "https://buytickets.at/voxbodystudio"}]
    messages = [_msg("Original Row Title", "2099-05-01",
                     url="https://buytickets.at/voxbodystudio/2392597")]
    _carry_calendar_dates(events, messages)
    assert events[0]["date"] == "2099-05-01"   # matched by link alone


def test_unmatched_event_keeps_its_own_link():
    events = [{"title": "Unrelated Thing", "date": "2099-06-01", "link": "https://real.example/e/1"}]
    messages = [_msg("Completely Other Event", "2099-06-05", url="https://erobay.com/x?ID=1")]
    _carry_calendar_dates(events, messages)
    assert events[0]["link"] == "https://real.example/e/1"
