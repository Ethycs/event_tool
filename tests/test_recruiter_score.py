"""Tests for recruiter email grading."""

from event_harvester.recruiter_score import (
    _action_for_score,
    _extract_domain,
    _score_body,
    _score_sender,
    _score_subject,
    grade_email,
    grade_emails_batch,
)


class TestExtractDomain:
    def test_angle_bracket_email(self):
        assert _extract_domain("Bob <bob@q1tech.com>") == "q1tech.com"

    def test_plain_email(self):
        assert _extract_domain("alice@stripe.com") == "stripe.com"

    def test_no_domain(self):
        assert _extract_domain("no email here") == ""


class TestScoreSender:
    def test_staffing_firm_penalized(self):
        delta, reasons = _score_sender("Mukul <mukul@q1tech.com>")
        assert delta < 0
        assert any("staffing" in r.lower() for r in reasons)

    def test_normal_domain_neutral(self):
        delta, reasons = _score_sender("Alice <alice@stripe.com>")
        assert delta == 0
        assert not reasons


class TestScoreSubject:
    def test_mass_blast_subject(self):
        delta, reasons = _score_subject(
            "Urgent Requirement - Java Developer"
        )
        assert delta < 0

    def test_normal_subject(self):
        delta, reasons = _score_subject(
            "Re: AI Eng opportunity - Arbital Health"
        )
        assert delta == 0


class TestScoreBody:
    def test_body_shop_format(self):
        body = (
            "Job Title: Senior Java Developer "
            "// Location: Remote // Duration: 12 months"
        )
        delta, reasons = _score_body(body)
        assert delta < 0
        assert any("body shop" in r.lower() for r in reasons)

    def test_template_opener(self):
        body = (
            "Hope you are well. I came across your profile "
            "and wanted to reach out."
        )
        delta, reasons = _score_body(body)
        assert delta < 0

    def test_calendar_link_positive(self):
        body = (
            "I'd love to chat! Here's my calendar: "
            "https://calendly.com/alice/30min"
        )
        delta, reasons = _score_body(body)
        assert delta > 0
        assert any("calendar" in r.lower() for r in reasons)

    def test_meeting_request_positive(self):
        body = (
            "Would love to grab coffee and discuss "
            "the role in more detail."
        )
        delta, reasons = _score_body(body)
        assert delta > 0
        assert any("meeting" in r.lower() for r in reasons)

    def test_quality_company_positive(self):
        body = (
            "We're hiring at Anthropic for an AI "
            "safety researcher role."
        )
        delta, reasons = _score_body(body)
        assert delta > 0
        assert any("anthropic" in r.lower() for r in reasons)

    def test_fulltime_positive(self):
        body = "This is a full-time position with competitive comp."
        delta, reasons = _score_body(body)
        assert delta > 0

    def test_sf_hybrid_positive(self):
        body = "The role is hybrid in San Francisco, 3 days in office."
        delta, reasons = _score_body(body)
        assert delta > 0
        assert any(
            "sf" in r.lower() or "bay area" in r.lower()
            for r in reasons
        )

    def test_salary_mentioned(self):
        body = "Compensation is $180k base + equity."
        delta, reasons = _score_body(body)
        assert delta > 0

    def test_contract_only_negative(self):
        body = "This is a C2C contract position, 6 months, remote."
        delta, reasons = _score_body(body)
        assert delta < 0


class TestActionForScore:
    def test_trash(self):
        assert _action_for_score(15) == "trash"

    def test_ignore(self):
        assert _action_for_score(35) == "ignore"

    def test_review(self):
        assert _action_for_score(55) == "review"

    def test_respond(self):
        assert _action_for_score(80) == "respond"

    def test_boundaries(self):
        assert _action_for_score(0) == "trash"
        assert _action_for_score(20) == "trash"
        assert _action_for_score(21) == "ignore"
        assert _action_for_score(45) == "ignore"
        assert _action_for_score(46) == "review"
        assert _action_for_score(65) == "review"
        assert _action_for_score(66) == "respond"
        assert _action_for_score(100) == "respond"


class TestGradeEmail:
    def test_obvious_spam(self):
        msg = {
            "platform": "gmail",
            "id": "spam1",
            "author": "Recruiter <recruiter@q1tech.com>",
            "content": (
                "Urgent Requirement\n"
                "Hope you are well. "
                "Job Title: Java Dev // Location: Remote "
                "// Duration: 6 months. C2C only."
            ),
            "timestamp": "2026-03-21",
        }
        grade = grade_email(msg)
        assert grade.score <= 20
        assert grade.action == "trash"

    def test_quality_outreach(self):
        msg = {
            "platform": "gmail",
            "id": "good1",
            "author": "Alice <alice@anthropic.com>",
            "content": (
                "AI Safety Role at Anthropic\n"
                "We noticed your background in AI safety."
            ),
        }
        body = (
            "Hi, I noticed your background in AI safety research "
            "and your work on the event_harvester project. We're "
            "hiring at Anthropic for a full-time AI safety "
            "researcher in San Francisco (hybrid). Would love to "
            "grab coffee and chat. Here's my calendar: "
            "https://calendly.com/alice/30min"
        )
        grade = grade_email(msg, body)
        assert grade.score >= 66
        assert grade.action == "respond"

    def test_borderline(self):
        msg = {
            "platform": "gmail",
            "id": "mid1",
            "author": "Bob <bob@somerecruiter.com>",
            "content": (
                "Software Engineer role\n"
                "Exciting opportunity for a software engineer "
                "at a fast-growing startup."
            ),
        }
        grade = grade_email(msg)
        assert 21 <= grade.score <= 65

    def test_score_clamped(self):
        msg = {
            "platform": "gmail",
            "id": "extreme",
            "author": "X <x@q1tech.com>",
            "content": (
                "Urgent Requirement\n"
                "Hope you are well. "
                "Job Title: Dev // Location: Remote. C2C."
            ),
        }
        grade = grade_email(msg)
        assert 0 <= grade.score <= 100


class TestGradeEmailsBatch:
    def test_sorts_by_score_descending(self):
        msgs = [
            {
                "platform": "gmail",
                "id": "a",
                "author": "x@q1tech.com",
                "content": "spam\nHope you are well",
            },
            {
                "platform": "gmail",
                "id": "b",
                "author": "y@anthropic.com",
                "content": (
                    "Good role\n"
                    "Let's chat about this full-time role."
                ),
            },
        ]
        grades = grade_emails_batch(msgs)
        assert grades[0].score >= grades[1].score

    def test_empty_input(self):
        assert grade_emails_batch([]) == []

    def test_uses_bodies_when_provided(self):
        msg = {
            "platform": "gmail",
            "id": "with_body",
            "author": "alice@stripe.com",
            "content": "Role at Stripe",
        }
        body_with_signal = (
            "We'd love to grab coffee. Full-time in "
            "San Francisco, hybrid. "
            "https://calendly.com/alice"
        )
        grades_without = grade_emails_batch([msg])
        grades_with = grade_emails_batch(
            [msg], bodies={"with_body": body_with_signal}
        )
        assert grades_with[0].score > grades_without[0].score
