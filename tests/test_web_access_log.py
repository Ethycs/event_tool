"""The access log: what changed for a source, by which method, and how long
the old state had held.

It exists to measure things that cannot be read off the site, such as how long
a passed bot check lasts. So what matters is that the bounds it reports are
the true ones for the observations it was given, and that one method's result
is never taken for another's.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from functools import partial

from event_harvester.sources import web_access_log as log

T0 = datetime(2026, 11, 24, 2, 15, 30, tzinfo=timezone.utc)

native = partial(log.record, method="native")


def _lines() -> list[dict]:
    if not log.LOG_FILE.exists():
        return []
    return [json.loads(line) for line in log.LOG_FILE.read_text().splitlines()]


def test_the_first_sight_of_a_source_is_a_change_from_nothing():
    line = native("erobay", "blocked", "bot check did not clear", at=T0)

    assert line["event"] == "change" and line["state"] == "blocked"
    assert line["method"] == "native"
    assert line["was"] is None and line["held_max_s"] is None
    assert _lines() == [line]


def test_an_unchanged_good_state_writes_nothing():
    native("stud", "ok", at=T0)
    assert native("stud", "ok", at=T0 + timedelta(hours=24)) is None
    assert len(_lines()) == 1


def test_a_change_reports_how_long_the_old_state_held_as_two_bounds():
    """Cleared at 02:15, still fine at 07:05, blocked the next night at 07:05:
    the clearance lasted at least 4h50m and at most 28h50m. Both are true and
    neither is the answer; the gap is how often we looked."""
    native("erobay", "ok", at=T0)
    native("erobay", "ok", at=T0 + timedelta(hours=4, minutes=50))
    line = native("erobay", "blocked", "bot check did not clear",
                  at=T0 + timedelta(hours=28, minutes=50))

    assert (line["was"], line["state"]) == ("ok", "blocked")
    assert line["was_since"] == "2026-11-24T02:15:30+00:00"
    assert line["held_min_s"] == 4 * 3600 + 50 * 60
    assert line["held_max_s"] == 28 * 3600 + 50 * 60


def test_methods_do_not_share_a_state():
    """A check passed by one method says nothing about another. Standard being
    blocked must not read as native having changed, in either direction."""
    native("venue", "ok", at=T0)
    blocked = log.record("venue", "blocked", "bot check", method="standard",
                         at=T0 + timedelta(minutes=15))
    still = native("venue", "ok", at=T0 + timedelta(minutes=30))

    assert blocked["was"] is None  # standard's first sight, not a change from ok
    assert still is None           # native never changed
    states = json.loads(log.STATE_FILE.read_text())
    assert states["venue via native"]["state"] == "ok"
    assert states["venue via standard"]["state"] == "blocked"


def test_a_failure_that_has_not_changed_is_still_logged_every_time():
    native("midway", "deviated", "found 2 dated links", at=T0)
    again = native("midway", "deviated", "found 2 dated links", at=T0 + timedelta(days=1))

    assert again["event"] == "fail"
    assert again["since"] == "2026-11-24T02:15:30+00:00"
    assert [line["event"] for line in _lines()] == ["change", "fail"]


def test_a_source_that_was_not_reached_is_logged_without_moving_its_state():
    native("makerfaire", "ok", at=T0)
    skipped = native("makerfaire", "skipped", "queued behind ra-sf, which hung",
                     observed=False, at=T0 + timedelta(days=1))
    back = native("makerfaire", "ok", at=T0 + timedelta(days=2))

    assert skipped["event"] == "fail" and skipped["state"] == "skipped"
    assert back is None  # still ok as far as anyone observed
    state = json.loads(log.STATE_FILE.read_text())["makerfaire via native"]
    assert state["since"] == "2026-11-24T02:15:30+00:00"
