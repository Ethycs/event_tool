"""Tests for event fingerprinting and structured matching."""

import json
from datetime import date, timedelta

import pytest

from event_harvester.pipeline.event_match import (
    _normalize_title,
    _titles_overlap,
    dedup_events,
    events_match,
    find_fingerprint,
    load_fingerprints,
    save_fingerprint,
)


class TestNormalizeTitle:
    def test_lowercases_and_strips_punctuation(self):
        words = _normalize_title("HOPE 26!")
        assert "hope" in words
        assert "26" in words

    def test_removes_stop_words(self):
        words = _normalize_title("The Big Event at the Park")
        assert "the" not in words
        assert "at" not in words
        assert "big" in words
        assert "event" in words
        assert "park" in words


class TestTitlesOverlap:
    def test_identical_titles(self):
        a = _normalize_title("HOPE 26")
        b = _normalize_title("HOPE 26")
        assert _titles_overlap(a, b) is True

    def test_sufficient_overlap(self):
        a = _normalize_title("Belgian Waffle Party")
        b = _normalize_title("Belgian Waffle Brunch")
        # overlap = {belgian, waffle} = 2, smaller = 3, 2/3 = 66% >= 50%
        assert _titles_overlap(a, b) is True

    def test_insufficient_overlap(self):
        a = _normalize_title("LVFC Waffle Party")
        b = _normalize_title("LVFC Mainstage Dances")
        # overlap = {lvfc} = 1, smaller = 3, 1/3 = 33% < 50%
        assert _titles_overlap(a, b) is False


class TestEventsMatch:
    def test_same_title_same_date_match(self):
        a = {"title": "HOPE 26", "date": "2026-08-14", "location": None, "link": None}
        b = {"title": "HOPE 26", "date": "2026-08-14", "location": None, "link": None}
        is_match, score = events_match(a, b)
        assert is_match is True
        assert score >= 2

    def test_same_title_different_date_no_match(self):
        a = {"title": "HOPE 26", "date": "2026-08-14", "location": None, "link": None}
        b = {"title": "HOPE 26", "date": "2026-09-01", "location": None, "link": None}
        is_match, score = events_match(a, b)
        assert is_match is False
        assert score == 1  # only title matches

    def test_different_titles_same_date_no_match(self):
        a = {"title": "LVFC Waffle Party", "date": "2026-05-01", "location": None, "link": None}
        b = {"title": "LVFC Mainstage Dances", "date": "2026-05-01", "location": None, "link": None}
        is_match, score = events_match(a, b)
        assert is_match is False  # title overlap < 50%

    def test_same_link_match(self):
        # A specific event URL (with a path) is identifying; two events sharing
        # it are the same event even with different LLM-written titles.
        a = {"title": "Some Event", "date": None, "location": None, "link": "https://hope.net/talks/42"}
        b = {"title": "Another Event", "date": None, "location": None, "link": "https://hope.net/talks/42"}
        is_match, score = events_match(a, b)
        assert is_match is True
        assert score >= 2  # link counts as 2

    def test_same_date_same_location_different_titles_no_match(self):
        # A venue hosts many events a day; date + location is not identity.
        a = {"title": "Go Topless Subs Meetup", "date": "2026-09-02",
             "location": "Munch", "link": None}
        b = {"title": "Miss Vickis Munch", "date": "2026-09-02",
             "location": "Munch", "link": None}
        is_match, score = events_match(a, b)
        assert is_match is False  # score reaches 2 (date+location) but no title/link identity

    def test_bare_domain_link_is_not_identity(self):
        # The LLM fills the link with the bare source domain for calendar rows.
        a = {"title": "One", "date": "2026-09-02", "location": None, "link": "https://erobay.com"}
        b = {"title": "Two", "date": "2026-09-02", "location": None, "link": "https://erobay.com"}
        assert events_match(a, b)[0] is False

    def test_exact_title_same_date_match(self):
        a = {"title": "Belgian Waffle Party", "date": "2026-05-01", "location": None, "link": None}
        b = {"title": "Belgian Waffle Party", "date": "2026-05-01", "location": None, "link": None}
        is_match, score = events_match(a, b)
        assert is_match is True

    def test_placeholder_link_is_not_a_match_key(self):
        # Regression (erobay 2026-09-02): calendar events have no URL and the
        # LLM emits `link=TBD`. Treating "TBD"=="TBD" as a link match (+2)
        # collapsed a whole day of distinct same-date events into one.
        a = {"title": "Exiles Munch", "date": "2026-09-02",
             "location": "Exiles", "link": "TBD"}
        b = {"title": "Go Topless Subs Meetup", "date": "2026-09-02",
             "location": "Oakland", "link": "TBD"}
        is_match, score = events_match(a, b)
        assert is_match is False   # different events; shared date alone is score 1
        assert score == 1

    def test_placeholder_variants_all_neutralized(self):
        for ph in ("TBD", "n/a", "None", "-", "unknown", "TODO"):
            a = {"title": "Event One", "date": "2026-09-02", "location": None, "link": ph}
            b = {"title": "Event Two", "date": "2026-09-02", "location": None, "link": ph}
            is_match, _ = events_match(a, b)
            assert is_match is False, f"placeholder {ph!r} wrongly matched"

    def test_real_shared_link_still_matches(self):
        a = {"title": "One Name", "date": "2026-09-02", "location": None, "link": "https://x.com/e/1"}
        b = {"title": "Other Name", "date": "2026-09-02", "location": None, "link": "https://x.com/e/1"}
        is_match, score = events_match(a, b)
        assert is_match is True and score >= 2

    def test_dedup_keeps_distinct_same_day_no_url_events(self):
        events = [
            {"title": "Exiles Munch", "date": "2026-09-02",
             "location": "Exiles", "link": "TBD"},
            {"title": "Go Topless Subs Meetup", "date": "2026-09-02",
             "location": "Oakland", "link": "TBD"},
            {"title": "SpankLab", "date": "2026-09-02",
             "location": "Wicked Grounds", "link": "TBD"},
            {"title": "Miss Vickis Munch", "date": "2026-09-02",
             "location": "Milpitas", "link": "TBD"},
        ]
        assert len(dedup_events(events)) == 4


class TestFingerprintStore:
    @pytest.fixture(autouse=True)
    def _use_tmp_fingerprint_file(self, tmp_path, monkeypatch):
        """Use a temporary file for fingerprints during tests."""
        tmp_file = tmp_path / ".event_fingerprints.json"
        monkeypatch.setattr("event_harvester.pipeline.event_match.FINGERPRINT_FILE", tmp_file)

    def test_save_and_find(self):
        # Relative, not literal. This read `"2026-08-14"` and passed for a
        # year, then failed on 2026-08-15 — `_load` prunes any fingerprint
        # dated before today (`event_match.py:196`), so the literal made the
        # test assert "a future event round-trips" only while that date
        # happened to be in the future.
        event = {
            "title": "HOPE 26",
            "date": (date.today() + timedelta(days=30)).isoformat(),
            "time": "09:00",
            "location": "New York",
            "link": "https://hope.net",
        }
        save_fingerprint(event, "task_abc123")
        fp = find_fingerprint(event)
        assert fp is not None
        assert fp["title"] == "HOPE 26"
        assert fp["ticktick_id"] == "task_abc123"

    def test_find_returns_none_when_no_match(self):
        event = {"title": "Nonexistent Event", "date": "2026-01-01", "location": None, "link": None}
        fp = find_fingerprint(event)
        assert fp is None

    def test_auto_prune_past_dates(self, monkeypatch):
        """Fingerprints with past dates are pruned on load."""
        from event_harvester.pipeline import event_match

        fp_file = event_match.FINGERPRINT_FILE
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()

        data = [
            {"title": "Past Event", "date": yesterday, "created_at": "2026-01-01"},
            {"title": "Future Event", "date": tomorrow, "created_at": "2026-01-01"},
        ]
        fp_file.parent.mkdir(parents=True, exist_ok=True)
        fp_file.write_text(json.dumps(data))

        fps = load_fingerprints()
        assert len(fps) == 1
        assert fps[0]["title"] == "Future Event"

    def test_auto_prune_old_no_date(self, monkeypatch):
        """Fingerprints with no date and created_at > 30 days ago are pruned."""
        from event_harvester.pipeline import event_match

        fp_file = event_match.FINGERPRINT_FILE
        old_date = (date.today() - timedelta(days=31)).isoformat()
        recent_date = (date.today() - timedelta(days=5)).isoformat()

        data = [
            {"title": "Old No Date", "date": None, "created_at": old_date},
            {"title": "Recent No Date", "date": None, "created_at": recent_date},
        ]
        fp_file.parent.mkdir(parents=True, exist_ok=True)
        fp_file.write_text(json.dumps(data))

        fps = load_fingerprints()
        assert len(fps) == 1
        assert fps[0]["title"] == "Recent No Date"


class TestDedupEvents:
    def test_removes_duplicates(self):
        events = [
            {"title": "HOPE 26", "date": "2026-08-14", "location": "New York", "link": None},
            {"title": "HOPE 26", "date": "2026-08-14", "location": "NYC", "link": None},
            {"title": "Other Event", "date": "2026-09-01", "location": None, "link": None},
        ]
        unique = dedup_events(events)
        assert len(unique) == 2
        titles = [e["title"] for e in unique]
        assert "HOPE 26" in titles
        assert "Other Event" in titles

    def test_keeps_all_when_no_duplicates(self):
        events = [
            {"title": "Event A", "date": "2026-05-01", "location": None, "link": None},
            {"title": "Event B", "date": "2026-06-01", "location": None, "link": None},
        ]
        unique = dedup_events(events)
        assert len(unique) == 2
