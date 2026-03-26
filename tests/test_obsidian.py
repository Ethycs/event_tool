"""Tests for Obsidian markdown report generation."""

import tempfile
from datetime import date
from pathlib import Path

from event_harvester.obsidian import write_events_report, write_recruiter_report
from event_harvester.recruiter_score import RecruiterGrade


def _sample_events():
    return [
        {
            "title": "AI Innovators Forum",
            "score": 13,
            "author": "Colton Kaplan",
            "source": "INBOX",
            "timestamp": "2026-03-18T17:39",
            "date": "2026-03-18T17:00",
            "all_day": False,
            "details": "A few last-minute pitch spots available.",
            "scheduling": True,
        },
        {
            "title": "Equinox Jam",
            "score": 10,
            "author": "Tea Tribe",
            "source": "INBOX",
            "timestamp": "2026-03-20T08:40",
            "date": "2026-03-20",
            "all_day": True,
            "details": "World folk jam at a historic venue.",
            "pinned": True,
        },
    ]


def _sample_links():
    return [
        {"score": 9.2, "url": "https://luma.com/event123", "author": "ratsuns"},
        {"score": 7.6, "url": "https://openai.com/blog/thing", "author": "ratsuns"},
    ]


def _sample_grades():
    return [
        RecruiterGrade(
            score=82, reasons=["Quality company: anthropic", "Calendar link"],
            action="respond", message_id="abc123",
            subject="AI Safety Role", sender="alice@anthropic.com",
        ),
        RecruiterGrade(
            score=55, reasons=["Full-time", "SF hybrid"],
            action="review", message_id="def456",
            subject="Software Eng", sender="bob@startup.com",
        ),
        RecruiterGrade(
            score=35, reasons=["Staffing firm: randstadusa.com"],
            action="ignore", message_id="ghi789",
            subject="Data Engineer", sender="rec@randstadusa.com",
        ),
        RecruiterGrade(
            score=8, reasons=["Staffing firm", "Body shop format", "Template opener"],
            action="trash", message_id="jkl012",
            subject="Urgent Requirement", sender="mukul@q1tech.com",
        ),
    ]


class TestEventsReport:
    def test_has_frontmatter(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_events_report(
                _sample_events(), [], _sample_links(),
                {"discord": 5, "gmail": 10}, 15, d, run_date=date(2026, 3, 23),
            )
            content = Path(path).read_text(encoding="utf-8")
            assert content.startswith("---\n")
            assert "date: 2026-03-23" in content
            assert "type: events" in content
            assert "event_count: 2" in content
            assert "tags: [events-pool, harvester]" in content

    def test_has_obsidian_features(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_events_report(
                _sample_events(), [], _sample_links(),
                {"discord": 5, "gmail": 10}, 15, d, run_date=date(2026, 3, 23),
            )
            content = Path(path).read_text(encoding="utf-8")
            assert "> [!info]" in content
            assert "- [ ]" in content
            assert "#event" in content
            assert "[[Recruiters 2026-03-23]]" in content

    def test_filename_is_date_stamped(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_events_report(
                _sample_events(), [], [], {"discord": 0}, 0, d,
                run_date=date(2026, 3, 23),
            )
            assert Path(path).name == "Events 2026-03-23.md"

    def test_overwrites_same_day(self):
        with tempfile.TemporaryDirectory() as d:
            path1 = write_events_report(
                _sample_events(), [], [], {"discord": 5}, 5, d,
                run_date=date(2026, 3, 23),
            )
            path2 = write_events_report(
                _sample_events()[:1], [], [], {"discord": 1}, 1, d,
                run_date=date(2026, 3, 23),
            )
            assert path1 == path2
            content = Path(path2).read_text(encoding="utf-8")
            assert "event_count: 1" in content

    def test_links_as_table(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_events_report(
                [], [], _sample_links(), {"gmail": 2}, 2, d,
                run_date=date(2026, 3, 23),
            )
            content = Path(path).read_text(encoding="utf-8")
            assert "| Score | Link | Author |" in content
            assert "luma.com" in content

    def test_ticktick_deep_link(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_events_report(
                _sample_events(), [], [], {"gmail": 1}, 1, d,
                run_date=date(2026, 3, 23),
            )
            content = Path(path).read_text(encoding="utf-8")
            assert "ticktick://x-callback-url/v1/add_task" in content

    def test_scheduled_tag(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_events_report(
                _sample_events(), [], [], {"gmail": 1}, 1, d,
                run_date=date(2026, 3, 23),
            )
            content = Path(path).read_text(encoding="utf-8")
            assert "#scheduled" in content

    def test_pinned_tag(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_events_report(
                _sample_events(), [], [], {"gmail": 1}, 1, d,
                run_date=date(2026, 3, 23),
            )
            content = Path(path).read_text(encoding="utf-8")
            assert "#pinned" in content


class TestRecruiterReport:
    def test_has_frontmatter(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_recruiter_report(
                _sample_grades(), d, run_date=date(2026, 3, 23),
            )
            content = Path(path).read_text(encoding="utf-8")
            assert content.startswith("---\n")
            assert "date: 2026-03-23" in content
            assert "type: recruiter-grades" in content
            assert "total: 4" in content
            assert "respond: 1" in content
            assert "review: 1" in content
            assert "ignore: 1" in content
            assert "trash: 1" in content

    def test_respond_has_unchecked_checkbox(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_recruiter_report(
                _sample_grades(), d, run_date=date(2026, 3, 23),
            )
            content = Path(path).read_text(encoding="utf-8")
            assert "- [ ] **AI Safety Role**" in content

    def test_trash_has_checked_checkbox(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_recruiter_report(
                _sample_grades(), d, run_date=date(2026, 3, 23),
            )
            content = Path(path).read_text(encoding="utf-8")
            assert "- [x] **Urgent Requirement**" in content

    def test_has_gmail_links(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_recruiter_report(
                _sample_grades(), d, run_date=date(2026, 3, 23),
            )
            content = Path(path).read_text(encoding="utf-8")
            assert "mail.google.com/mail/u/0/#all/abc123" in content
            assert "Open in Gmail" in content

    def test_trash_items_have_strikethrough(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_recruiter_report(
                _sample_grades(), d, run_date=date(2026, 3, 23),
            )
            content = Path(path).read_text(encoding="utf-8")
            assert "~~[Open in Gmail]" in content

    def test_has_wikilink_to_events(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_recruiter_report(
                _sample_grades(), d, run_date=date(2026, 3, 23),
            )
            content = Path(path).read_text(encoding="utf-8")
            assert "[[Events 2026-03-23]]" in content

    def test_filename_is_date_stamped(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_recruiter_report(
                _sample_grades(), d, run_date=date(2026, 3, 23),
            )
            assert Path(path).name == "Recruiters 2026-03-23.md"

    def test_empty_grades(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_recruiter_report(
                [], d, run_date=date(2026, 3, 23),
            )
            content = Path(path).read_text(encoding="utf-8")
            assert "total: 0" in content
            assert "No recruiter emails to grade." in content

    def test_callout_types(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_recruiter_report(
                _sample_grades(), d, run_date=date(2026, 3, 23),
            )
            content = Path(path).read_text(encoding="utf-8")
            assert "> [!important]" in content
            assert "> [!warning]" in content
            assert "> [!caution]" in content
