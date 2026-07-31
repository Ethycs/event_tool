"""Tests for ``event_harvester.observability.hints``.

The rules are the load-bearing part: they decide whether a vision call happens
at all, so a rule that under-reports its confidence costs money on every page
and one that over-reports costs a wrong diagnosis. Both directions are pinned.
"""

from __future__ import annotations

from unittest.mock import patch

from event_harvester.observability import hints
from event_harvester.observability.hints import (
    MIN_CONTENT_CHARS,
    VLM_ESCALATION_THRESHOLD,
    Hint,
    collect_signals,
    diagnose,
    rule_hints,
)


def _signals(**overrides):
    """A page that looks like healthy content unless told otherwise."""
    base = {
        "url": "https://example.com/events",
        "title": "Events",
        "text_len": 5000,
        "text_sample": "Friday night: live music. Saturday: drag brunch. Sunday: quiz.",
        "links": 40,
        "inputs": 0,
        "password_inputs": 0,
        "search_inputs": 0,
        "forms": 0,
        "iframes": 0,
        "dialogs": 0,
        "next_controls": 0,
        "scroll_height": 2000,
        "viewport_height": 1000,
    }
    base.update(overrides)
    return base


class FakePage:
    def __init__(self, signals=None, raises=False, png=b"\x89PNG"):
        self._signals = signals
        self._raises = raises
        self._png = png

    def evaluate(self, *_args, **_kwargs):
        if self._raises:
            raise RuntimeError("page is closed")
        return self._signals

    def screenshot(self, *_args, **_kwargs):
        return self._png


class TestCollectSignals:
    def test_a_dead_page_yields_no_signals_instead_of_raising(self):
        assert collect_signals(FakePage(raises=True)) == {}

    def test_signals_pass_through(self):
        got = collect_signals(FakePage(_signals(links=7)))
        assert got["links"] == 7


class TestRuleHints:
    def test_healthy_page_reads_as_content_and_does_not_escalate(self):
        top = rule_hints(_signals())[0]
        assert top.kind == "content"
        assert top.confidence > VLM_ESCALATION_THRESHOLD, (
            "a clear page must not pay for a screenshot"
        )

    def test_password_field_is_a_login_wall(self):
        hints_ = rule_hints(_signals(password_inputs=1))
        assert hints_[0].kind == "login_wall"
        assert hints_[0].confidence > VLM_ESCALATION_THRESHOLD
        assert hints_[0].suggested_step is None, "no click gets past a sign-in"

    def test_bot_check_wording_outranks_everything(self):
        hints_ = rule_hints(_signals(
            text_sample="Just a moment... checking your browser before you continue",
            text_len=120, links=1,
        ))
        assert hints_[0].kind == "cloudflare_challenge"

    def test_consent_dialog_suggests_a_dismiss_step(self):
        hints_ = rule_hints(_signals(text_sample="We use cookies to improve your experience"))
        cookie = next(h for h in hints_ if h.kind == "cookie_banner")
        assert cookie.suggested_step == {"verb": "dismiss"}

    def test_error_wording_reads_as_error(self):
        for text in ("404 Not Found", "Something went wrong", "Error 503",
                     "Internal Server Error", "This site can't be reached"):
            assert rule_hints(_signals(text_sample=text))[0].kind == "error", text

    def test_a_healthy_page_full_of_numbers_is_not_an_error(self):
        """Regression: lu.ma/discover, 51 event links, classified `error` at 0.85.

        A bare `\\b404\\b` matched "1,404 going" and `\\b500\\b` matched
        "500 attendees". The confidence was the real damage — 0.85 is above the
        escalation threshold, so `auto` would never have screenshotted the page
        that disproves it.
        """
        realistic = (
            "AI Happy Hour · 500 attendees · SF Tech Week. "
            "Founders Dinner, 1,404 going. Art & Wine, 502 interested."
        )
        top = rule_hints(_signals(text_sample=realistic))[0]
        assert top.kind != "error", f"got {top.kind} at {top.confidence}"

    def test_no_results_wording_reads_as_empty(self):
        hints_ = rule_hints(_signals(text_sample="No events found. Try a different search."))
        assert any(h.kind == "empty" for h in hints_)

    def test_a_tall_page_with_few_links_suggests_scrolling(self):
        hints_ = rule_hints(_signals(links=3, scroll_height=9000, viewport_height=1000))
        scroll = next(h for h in hints_ if h.kind == "needs_scroll")
        assert scroll.suggested_step["verb"] == "scroll"

    def test_a_search_box_with_little_content_suggests_a_query(self):
        hints_ = rule_hints(_signals(search_inputs=1, links=4))
        assert any(h.kind == "needs_search" for h in hints_)

    def test_a_thin_unexplained_page_is_empty_not_content(self):
        hints_ = rule_hints(_signals(text_len=MIN_CONTENT_CHARS - 1, text_sample="", links=0))
        assert hints_[0].kind == "empty"

    def test_no_signals_at_all_is_unknown(self):
        assert rule_hints({})[0].kind == "unknown"

    def test_a_healthy_page_with_a_load_more_button_still_reads_as_content(self):
        """Regression: ranking the suggestion first bought a screenshot per page.

        lu.ma/discover has 51 event links *and* a next control. If
        needs_pagination (0.50) outranks content, every healthy paginated page
        falls below the escalation threshold and pays for a vision call.
        """
        hints_ = rule_hints(_signals(next_controls=1))
        assert hints_[0].kind == "content"
        assert hints_[0].confidence > VLM_ESCALATION_THRESHOLD
        assert [h.kind for h in hints_[1:]] == ["needs_pagination"], (
            "the suggestion survives, below"
        )

    def test_a_blocked_page_never_reads_as_content_however_full(self):
        hints_ = rule_hints(_signals(password_inputs=1, links=200, text_len=9000))
        assert all(h.kind != "content" for h in hints_)

    def test_hints_come_back_ranked(self):
        hints_ = rule_hints(_signals(
            password_inputs=1, search_inputs=1, links=2,
            scroll_height=9000, viewport_height=1000,
        ))
        confidences = [h.confidence for h in hints_]
        assert confidences == sorted(confidences, reverse=True)


class TestEscalation:
    """When the screenshot gets taken, and when it does not."""

    def test_auto_does_not_escalate_on_a_confident_rule(self):
        with patch.object(hints, "vlm_hint") as vlm:
            diag = diagnose(FakePage(_signals(password_inputs=1)), object(), use_vlm="auto")
        vlm.assert_not_called()
        assert diag.vlm_used is False
        assert diag.kind == "login_wall"

    def test_auto_escalates_when_the_rules_are_unsure(self):
        # A bare page with a search box: needs_search sits at 0.55, under the
        # threshold, and this is exactly the "I can't tell, look at it" case.
        reply = ("empty", "empty\nNothing rendered.", "/tmp/x.png")
        page = FakePage(_signals(search_inputs=1, links=4, text_len=500))
        with patch.object(hints, "vlm_hint", return_value=reply) as vlm:
            diag = diagnose(page, object(), use_vlm="auto")
        vlm.assert_called_once()
        assert diag.vlm_used is True
        assert diag.kind == "empty", "the picture outranks the DOM when they disagree"

    def test_explicit_no_never_escalates_even_when_unsure(self):
        page = FakePage(_signals(text_len=10, links=0, text_sample=""))
        with patch.object(hints, "vlm_hint") as vlm:
            diag = diagnose(page, object(), use_vlm=False)
        vlm.assert_not_called()
        assert diag.vlm_used is False

    def test_no_llm_config_means_no_escalation(self):
        with patch.object(hints, "vlm_hint") as vlm:
            diagnose(FakePage(_signals(text_len=10, links=0, text_sample="")), None, use_vlm=True)
        vlm.assert_not_called()

    def test_a_failed_vision_call_does_not_break_the_diagnosis(self):
        page = FakePage(_signals(text_len=10, links=0, text_sample=""))
        boom = RuntimeError("402 payment required")
        with patch.object(hints, "vision_completion", side_effect=boom):
            diag = diagnose(page, object(), use_vlm=True)
        assert diag.vlm_used is True
        assert "402" in diag.vlm_explanation
        assert diag.kind != "", "rules still produced a verdict"

    def test_the_rule_suggestion_survives_a_vlm_override(self):
        """The picture wins on *what* it is; the rules keep *what to do*."""
        reply = ("cookie_banner", "cookie_banner\nA consent modal covers the page.", None)
        page = FakePage(_signals(dialogs=1, links=4, search_inputs=1, text_len=500))
        with patch.object(hints, "vlm_hint", return_value=reply):
            diag = diagnose(page, object(), use_vlm=True)
        assert diag.kind == "cookie_banner"
        assert diag.top.suggested_step == {"verb": "dismiss"}


class TestDiagnosisShape:
    def test_summary_is_one_readable_line(self):
        diag = diagnose(FakePage(_signals(dialogs=1)), None, use_vlm=False)
        line = diag.summary()
        assert "cookie_banner" in line and "\n" not in line

    def test_to_dict_drops_the_text_sample(self):
        """The sample is up to 4k of page text; it is for rules, not for callers."""
        payload = diagnose(FakePage(_signals()), None, use_vlm=False).to_dict()
        assert "text_sample" not in payload["signals"]
        assert payload["signals"]["links"] == 40

    def test_hint_round_trips(self):
        h = Hint("needs_scroll", 0.7, "tall page", {"verb": "scroll", "seconds": 8})
        assert h.to_dict()["suggested_step"]["seconds"] == 8
