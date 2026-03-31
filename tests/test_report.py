"""Tests for markdown report generation."""

import os
import tempfile

from event_harvester.report import ticktick_deep_link, generate_report


class TestTickTickDeepLink:
    def test_basic_link(self):
        link = ticktick_deep_link("Test Event", "2026-03-21T14:00", False, "context")
        assert link.startswith("ticktick://x-callback-url/v1/add_task?")
        assert "title=Test%20Event" in link
        assert "allDay=false" in link

    def test_all_day_event(self):
        link = ticktick_deep_link("All Day", "2026-03-21", True, "ctx")
        assert "allDay=true" in link
        assert "startDate=2026-03-21T00%3A00%3A00.000%2B0000" in link

    def test_special_chars_encoded(self):
        link = ticktick_deep_link("Event @ Venue!", None, False, "From @user")
        assert "Event%20%40%20Venue%21" in link
        assert "%40user" in link

    def test_no_date(self):
        link = ticktick_deep_link("No Date", None, False, "ctx")
        assert "startDate" not in link
        assert "allDay" not in link


class TestGenerateReport:
    def test_generates_markdown_with_validated_events(self):
        events = [
            {
                "title": "AI Meetup",
                "score": 13,
                "author": "someone",
                "source": "INBOX",
                "timestamp": "2026-03-21T14:00",
                "date": "2026-03-25T17:00",
                "all_day": False,
                "details": "Join us for an AI meetup.",
                "pinned": False,
            }
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            path = f.name
        try:
            result = generate_report(
                validated_events=events,
                raw_events=[],
                links=[],
                source_counts={"discord": 10, "gmail": 20},
                total_messages=30,
                output_path=path,
            )
            assert result == path
            content = open(path, encoding="utf-8").read()
            assert "# Event Harvester - Top Events" in content
            assert "### 1. AI Meetup" in content
            assert "**Score**: 13" in content
            assert "ticktick://" in content
            assert "Discord + Gmail (30 messages)" in content
        finally:
            os.unlink(path)

    def test_falls_back_to_raw_events(self):
        raw = [
            {
                "content": "RSVP for tonight's meetup",
                "score": 10,
                "author": "bob",
                "channel": "general",
                "timestamp": "2026-03-21",
                "dates": ["tonight"],
                "times": [],
                "scheduling": True,
                "pinned": False,
            }
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            path = f.name
        try:
            generate_report(
                validated_events=[],
                raw_events=raw,
                links=[],
                source_counts={"discord": 5},
                total_messages=5,
                output_path=path,
            )
            content = open(path, encoding="utf-8").read()
            assert "RSVP for tonight" in content
            assert "ticktick://" in content
        finally:
            os.unlink(path)

    def test_includes_links_section(self):
        links = [
            {
                "url": "https://luma.com/event/123",
                "score": 9.5,
                "author": "alice",
            }
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            path = f.name
        try:
            generate_report(
                validated_events=[],
                raw_events=[],
                links=links,
                source_counts={"gmail": 3},
                total_messages=3,
                output_path=path,
            )
            content = open(path, encoding="utf-8").read()
            assert "## Top Links" in content
            assert "luma.com" in content
        finally:
            os.unlink(path)

    def test_empty_report(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            path = f.name
        try:
            generate_report(
                validated_events=[],
                raw_events=[],
                links=[],
                source_counts={},
                total_messages=0,
                output_path=path,
            )
            content = open(path, encoding="utf-8").read()
            assert "No events or links found" in content
        finally:
            os.unlink(path)
