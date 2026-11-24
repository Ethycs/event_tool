"""The access log: what changed for a source, when, and how long it had held.

It exists to measure things that cannot be read off the site, such as how long
a passed bot check lasts. So what matters is that the bounds it reports are
the true ones for the observations it was given.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from event_harvester.sources import web_access_log as log

T0 = datetime(2026, 11, 24, 2, 15, 30, tzinfo=timezone.utc)


def _lines() -> list[dict]:
    if not log.LOG_FILE.exists():
        return []
    return [json.loads(line) for line in log.LOG_FILE.read_text().splitlines()]


def test_the_first_sight_of_a_source_is_a_change_from_nothing():
    line = log.record("erobay", "blocked", "bot check did not clear", at=T0)

    assert line["event"] == "change" and line["state"] == "blocked"
    assert line["was"] is None and line["held_max_s"] is None
    assert _lines() == [line]


def test_an_unchanged_good_state_writes_nothing():
    log.record("stud", "ok", at=T0)
    assert log.record("stud", "ok", at=T0 + timedelta(hours=24)) is None
    assert len(_lines()) == 1


def test_a_change_reports_how_long_the_old_state_held_as_two_bounds():
    """Cleared at 02:15, still fine at 07:05, blocked the next night at 07:05:
    the clearance lasted at least 4h50m and at most 28h50m. Both are true and
    neither is the answer; the gap is how often we looked."""
    log.record("erobay", "ok", "cleared by a person", at=T0)
    log.record("erobay", "ok", at=T0 + timedelta(hours=4, minutes=50))
    line = log.record("erobay", "blocked", "bot check did not clear",
                      at=T0 + timedelta(hours=28, minutes=50))

    assert (line["was"], line["state"]) == ("ok", "blocked")
    assert line["was_since"] == "2026-11-24T02:15:30+00:00"
    assert line["held_min_s"] == 4 * 3600 + 50 * 60
    assert line["held_max_s"] == 28 * 3600 + 50 * 60


def test_a_failure_that_has_not_changed_is_still_logged_every_time():
    log.record("midway", "deviated", "found 2 dated links", at=T0)
    again = log.record("midway", "deviated", "found 2 dated links", at=T0 + timedelta(days=1))

    assert again["event"] == "fail"
    assert again["since"] == "2026-11-24T02:15:30+00:00"
    assert [line["event"] for line in _lines()] == ["change", "fail"]


def test_a_source_that_was_not_reached_is_logged_without_moving_its_state():
    log.record("makerfaire", "ok", at=T0)
    skipped = log.record("makerfaire", "skipped", "queued behind ra-sf, which hung",
                         observed=False, at=T0 + timedelta(days=1))
    back = log.record("makerfaire", "ok", at=T0 + timedelta(days=2))

    assert skipped["event"] == "fail" and skipped["state"] == "skipped"
    assert back is None  # still ok as far as anyone observed
    state = json.loads(log.STATE_FILE.read_text())["makerfaire"]
    assert state["since"] == "2026-11-24T02:15:30+00:00"
