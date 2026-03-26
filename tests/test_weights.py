"""Tests for link and event weighting."""

from datetime import datetime, timezone

from event_harvester.weights import (
    _link_type_score,
    _recency_score,
    extract_events,
    extract_links,
)


class TestRecencyScore:
    def test_recent_is_high(self):
        now = datetime(2026, 3, 18, tzinfo=timezone.utc)
        ts = "2026-03-17T10:00:00+00:00"
        assert _recency_score(ts, now) == 10

    def test_two_weeks_ago(self):
        now = datetime(2026, 3, 18, tzinfo=timezone.utc)
        ts = "2026-03-06T10:00:00+00:00"
        assert _recency_score(ts, now) == 8

    def test_old_is_low(self):
        now = datetime(2026, 3, 18, tzinfo=timezone.utc)
        ts = "2025-01-01T10:00:00+00:00"
        assert _recency_score(ts, now) == 1


class TestLinkTypeScore:
    def test_event_links_score_high(self):
        assert _link_type_score("https://luma.com/event/123") == 8
        assert _link_type_score("https://www.eventbrite.com/e/foo") == 8

    def test_tech_links_score_medium(self):
        assert _link_type_score("https://github.com/foo/bar") == 6
        assert _link_type_score("https://openai.com/blog") == 6

    def test_social_links_score_low(self):
        assert _link_type_score("https://instagram.com/reel/abc") == 3

    def test_gif_links_skipped(self):
        assert _link_type_score("https://tenor.com/view/foo") == 0
        assert _link_type_score("https://giphy.com/gifs/abc") == 0

    def test_generic_links_default(self):
        assert _link_type_score("https://example.com/page") == 4


class TestExtractLinks:
    def test_extracts_and_scores(self):
        now = datetime(2026, 3, 18, tzinfo=timezone.utc)
        msgs = [
            {
                "platform": "discord",
                "id": "1",
                "timestamp": "2026-03-17T10:00:00+00:00",
                "author": "alice",
                "channel": "ch1",
                "content": "Check out https://luma.com/event/123",
            },
        ]
        links = extract_links(msgs, now)
        assert len(links) == 1
        assert links[0]["score"] == 9.2  # 10*0.6 + 8*0.4
        assert links[0]["url"] == "https://luma.com/event/123"

    def test_dedupes_by_url(self):
        now = datetime(2026, 3, 18, tzinfo=timezone.utc)
        msgs = [
            {
                "platform": "discord", "id": "1",
                "timestamp": "2026-03-17T10:00:00+00:00",
                "author": "a", "channel": "c",
                "content": "https://example.com/page",
            },
            {
                "platform": "discord", "id": "2",
                "timestamp": "2026-03-10T10:00:00+00:00",
                "author": "b", "channel": "c",
                "content": "https://example.com/page",
            },
        ]
        links = extract_links(msgs, now)
        assert len(links) == 1
        # Should keep the higher-scored (more recent) one
        assert links[0]["author"] == "a"

    def test_skips_gifs(self):
        msgs = [
            {
                "platform": "discord", "id": "1",
                "timestamp": "2026-03-17T10:00:00+00:00",
                "author": "a", "channel": "c",
                "content": "https://tenor.com/view/funny-gif-123",
            },
        ]
        assert extract_links(msgs) == []


class TestExtractEvents:
    def test_finds_scheduling_keywords(self):
        now = datetime(2026, 3, 18, tzinfo=timezone.utc)
        msgs = [
            {
                "platform": "discord", "id": "1",
                "timestamp": "2026-03-17T10:00:00+00:00",
                "author": "a", "channel": "c",
                "content": "Who's coming to the meeting tonight? RSVP below!",
            },
        ]
        events = extract_events(msgs, now)
        assert len(events) == 1
        assert events[0]["scheduling"] is True
        assert events[0]["score"] == 13  # 10 + 3

    def test_finds_date_references(self):
        now = datetime(2026, 3, 18, tzinfo=timezone.utc)
        msgs = [
            {
                "platform": "discord", "id": "1",
                "timestamp": "2026-03-17T10:00:00+00:00",
                "author": "a", "channel": "c",
                "content": "I have plans on Saturday, maybe we hang out after",
            },
        ]
        events = extract_events(msgs, now)
        assert len(events) == 1
        assert "Saturday" in events[0]["dates"]

    def test_skips_short_messages(self):
        msgs = [
            {
                "platform": "discord", "id": "1",
                "timestamp": "2026-03-17T10:00:00+00:00",
                "author": "a", "channel": "c",
                "content": "ok Monday",
            },
        ]
        events = extract_events(msgs)
        assert len(events) == 0

    def test_finds_times(self):
        now = datetime(2026, 3, 18, tzinfo=timezone.utc)
        msgs = [
            {
                "platform": "discord", "id": "1",
                "timestamp": "2026-03-17T10:00:00+00:00",
                "author": "a", "channel": "c",
                "content": "See you Sunday at 4:00pm at the usual spot",
            },
        ]
        events = extract_events(msgs, now)
        assert len(events) == 1
        assert "4:00pm" in events[0]["times"]
