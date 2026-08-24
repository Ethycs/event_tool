"""The rolling Telegram harvest window: fetch since the last run, plus overlap.

Re-scanning the full days_back every day is wasted work. The window rolls
forward, anchored to the previous harvest minus an overlap buffer so a late
run or an edited message can't slip through a gap -- but days_back stays the
ceiling, so the window can only ever be *too wide*, never too narrow. Too wide
costs time; too narrow would cost messages, so the safety direction is the one
that matters.

State is repo-anchored (`data/.telegram_window.json` via data_path), so tests
redirect it -- a chdir is not isolation here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from event_harvester.sources import telegram as tg

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)


class TestRollingCutoff:
    def test_a_daily_run_looks_back_a_day_plus_overlap(self):
        last = NOW - timedelta(hours=24)
        cutoff = tg.rolling_cutoff(NOW, 7, 6.0, last)
        assert round((NOW - cutoff).total_seconds() / 3600, 1) == 30.0

    def test_first_run_gets_the_full_ceiling(self):
        assert tg.rolling_cutoff(NOW, 7, 6.0, None) == NOW - timedelta(days=7)

    def test_a_long_gap_is_capped_at_the_ceiling(self):
        """After a 10-day outage, days_back(7) still bounds the lookback --
        don't pull a fortnight because a run was missed."""
        last = NOW - timedelta(days=10)
        assert tg.rolling_cutoff(NOW, 7, 6.0, last) == NOW - timedelta(days=7)

    def test_a_future_last_harvest_falls_back_to_the_ceiling(self):
        """Clock skew or corrupt state must never produce an empty (future)
        window."""
        assert tg.rolling_cutoff(NOW, 7, 6.0, NOW + timedelta(hours=5)) \
            == NOW - timedelta(days=7)

    def test_the_window_only_ever_narrows_never_widens(self):
        """The invariant: a rolling cutoff is never older than the ceiling."""
        for hours_ago in (1, 12, 24, 24 * 6, 24 * 30):
            last = NOW - timedelta(hours=hours_ago)
            cutoff = tg.rolling_cutoff(NOW, 7, 6.0, last)
            assert cutoff >= NOW - timedelta(days=7)


class TestWindowState:
    def test_round_trip(self, tmp_path):
        p = tmp_path / "win.json"
        tg.save_last_harvest(NOW, p)
        assert tg.load_last_harvest(p) == NOW

    def test_missing_state_is_none(self, tmp_path):
        assert tg.load_last_harvest(tmp_path / "nope.json") is None

    def test_corrupt_state_is_none_not_a_crash(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text('{"last_harvest_at": "not-a-date"}', encoding="utf-8")
        assert tg.load_last_harvest(p) is None

    def test_a_naive_timestamp_is_read_as_utc(self, tmp_path):
        p = tmp_path / "naive.json"
        p.write_text('{"last_harvest_at": "2026-08-23T12:00:00"}', encoding="utf-8")
        assert tg.load_last_harvest(p) == NOW

    def test_saved_state_advances_the_next_window(self, tmp_path):
        """End-to-end: save now, and the next cutoff anchors to it."""
        p = tmp_path / "w.json"
        tg.save_last_harvest(NOW - timedelta(hours=24), p)
        later = NOW
        cutoff = tg.rolling_cutoff(later, 7, 6.0, tg.load_last_harvest(p))
        assert round((later - cutoff).total_seconds() / 3600, 1) == 30.0
