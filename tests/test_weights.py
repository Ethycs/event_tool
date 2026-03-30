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


class TestGateSignal:
    """Test the has_date_or_event_signal() gate function."""

    def test_ordinal_date(self):
        from event_harvester.weights import has_date_or_event_signal
        assert has_date_or_event_signal("Party on April 1st")
        assert has_date_or_event_signal("Meet on the 15th")
        assert has_date_or_event_signal("March 3rd meetup")

    def test_in_x_time(self):
        from event_harvester.weights import has_date_or_event_signal
        assert has_date_or_event_signal("Starting in 2 hours")
        assert has_date_or_event_signal("Event in 3 days")
        assert has_date_or_event_signal("Launching in 1 week")

    def test_24_hour_time(self):
        from event_harvester.weights import has_date_or_event_signal
        assert has_date_or_event_signal("Starts at 18:00")
        assert has_date_or_event_signal("Doors open 09:30")

    def test_iso_date(self):
        from event_harvester.weights import has_date_or_event_signal
        assert has_date_or_event_signal("Event on 2026-04-02")
        assert has_date_or_event_signal("2026-04-02T18:00 kickoff")

    def test_time_range(self):
        from event_harvester.weights import has_date_or_event_signal
        assert has_date_or_event_signal("Open 2-6pm")
        assert has_date_or_event_signal("Available 9:30-11am")

    def test_lowercase_timezone(self):
        from event_harvester.weights import has_date_or_event_signal
        assert has_date_or_event_signal("Call at 7:00 et")
        assert has_date_or_event_signal("Doors at 9:00 pst")

    def test_date_range(self):
        from event_harvester.weights import has_date_or_event_signal
        assert has_date_or_event_signal("April 3-5 convention")
        assert has_date_or_event_signal("March 28-30")

    def test_year_mention(self):
        from event_harvester.weights import has_date_or_event_signal
        assert has_date_or_event_signal("Coming in 2026")
        assert has_date_or_event_signal("January 2026")

    def test_phrases(self):
        from event_harvester.weights import has_date_or_event_signal
        assert has_date_or_event_signal("This weekend plans")
        assert has_date_or_event_signal("Later today we meet")
        assert has_date_or_event_signal("End of month deadline")

    def test_event_links(self):
        from event_harvester.weights import has_date_or_event_signal
        assert has_date_or_event_signal("Register: lu.ma/event")
        assert has_date_or_event_signal("Sign up eventbrite.com/e/123")
        assert has_date_or_event_signal("Join zoom.us/j/12345")

    def test_prefix_month_day(self):
        from event_harvester.weights import has_date_or_event_signal
        assert has_date_or_event_signal("next Mar 21 meetup")
        assert has_date_or_event_signal("this April 5th")
        assert has_date_or_event_signal("next January 15")

    def test_existing_patterns_still_work(self):
        from event_harvester.weights import has_date_or_event_signal
        assert has_date_or_event_signal("March 25 meetup")
        assert has_date_or_event_signal("3/25 party")
        assert has_date_or_event_signal("See you tomorrow")
        assert has_date_or_event_signal("This Friday drinks")
        assert has_date_or_event_signal("Next Monday standup")
        assert has_date_or_event_signal("4:30pm ET")

    def test_no_signal(self):
        from event_harvester.weights import has_date_or_event_signal
        assert not has_date_or_event_signal("Nice weather outside")
        assert not has_date_or_event_signal("Great code review")
        assert not has_date_or_event_signal("lol that was funny")
        assert not has_date_or_event_signal("THE MOUSE IS EVIL")
