"""Tests for ``event_harvester.sources.web_steps``.

Validation is browser-free on purpose, so most of this file is too. The replay
tests use duck-typed fakes in the same style as ``test_web_primitives.py``.
"""

from __future__ import annotations

from unittest.mock import patch

from event_harvester.sources import web_steps
from event_harvester.sources.web_steps import (
    MAX_REPEAT,
    MAX_STEPS,
    ReplayResult,
    replay,
    resolve_target,
    validate_steps,
)
from event_harvester.web_primitives import Element


class FakePage:
    def __init__(self, *, links=None, text="", goto_raises=False):
        self._links = links or []
        self._text = text
        self._goto_raises = goto_raises
        self.gotos: list[str] = []
        self.waits: list[int] = []
        self.filled: list[tuple[str, str]] = []
        self.clicked: list[str] = []

    def goto(self, url, **_kw):
        if self._goto_raises:
            raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")
        self.gotos.append(url)

    def wait_for_timeout(self, ms):
        self.waits.append(ms)

    def fill(self, selector, text):
        self.filled.append((selector, text))

    def press(self, selector, key):
        pass

    def click(self, selector):
        self.clicked.append(selector)

    def evaluate(self, *_a, **_kw):
        return self._text


class FakeUC:
    def __init__(self, *, feed_items=None):
        self._feed_items = feed_items or []
        self.dismiss_calls = 0

    def dismiss_cookies(self, page):
        self.dismiss_calls += 1
        return True

    def close_modal(self, page):
        return False

    def get_feed_items(self, page):
        return list(self._feed_items)


class TestValidateSteps:
    def test_a_good_route_has_no_errors(self):
        assert validate_steps([
            {"verb": "goto", "url": "https://example.com"},
            {"verb": "dismiss"},
            {"verb": "fill", "target": "search", "text": "AI", "submit": True},
            {"verb": "scroll", "seconds": 8},
            {"verb": "extract", "what": "links"},
        ]) == []

    def test_not_a_list(self):
        assert validate_steps({"verb": "goto"}) == ["steps must be a list"]

    def test_unknown_verb_is_named(self):
        errors = validate_steps([{"verb": "teleport"}])
        assert any("teleport" in e for e in errors)

    def test_missing_required_key(self):
        errors = validate_steps([{"verb": "fill", "target": "search"}])
        assert any("'text'" in e for e in errors)

    def test_unexpected_key_is_caught(self):
        """A typo'd key would otherwise be silently ignored at replay."""
        errors = validate_steps([{"verb": "scroll", "second": 8}])
        assert any("second" in e for e in errors)

    def test_unknown_target_role_is_rejected(self):
        errors = validate_steps([{"verb": "click", "target": "the-big-button"}])
        assert any("neither a known role" in e for e in errors)

    def test_selector_escape_hatch_is_allowed(self):
        assert validate_steps([{"verb": "click", "target": "selector:.next-page"}]) == []

    def test_repeat_is_bounded(self):
        errors = validate_steps(
            [{"verb": "click", "target": "pagination", "repeat": MAX_REPEAT + 1}]
        )
        assert any("MAX_REPEAT" in e for e in errors)

    def test_route_length_is_bounded(self):
        errors = validate_steps([{"verb": "dismiss"}] * (MAX_STEPS + 1))
        assert any("too many steps" in e for e in errors)

    def test_bad_extract_kind(self):
        errors = validate_steps([{"verb": "extract", "what": "vibes"}])
        assert any("what must be one of" in e for e in errors)


class TestResolveTarget:
    def test_selector_escape_hatch_bypasses_discovery(self):
        el = resolve_target(FakeUC(), FakePage(), "selector:#go")
        assert el.selector == "#go"
        assert el.confidence == 1.0

    def test_a_role_goes_through_the_l1_verb(self):
        found = Element("input#q", None, 0.9, "search")
        with patch.object(web_steps.W, "find_search_box", return_value=found) as finder:
            el = resolve_target(FakeUC(), FakePage(), "search")
        finder.assert_called_once()
        assert el is found

    def test_no_target_resolves_to_none(self):
        assert resolve_target(FakeUC(), FakePage(), None) is None


class TestReplay:
    def test_a_clean_route_runs_every_step(self):
        page, uc = FakePage(), FakeUC()
        with patch.object(web_steps.W, "extract_links", return_value=[{"url": "https://e/1"}]):
            out = replay(uc, page, [
                {"verb": "goto", "url": "https://example.com"},
                {"verb": "dismiss"},
                {"verb": "extract", "what": "links"},
            ])
        assert out.ok is True
        assert [r.verb for r in out.results] == ["goto", "dismiss", "extract"]
        assert out.links == [{"url": "https://e/1"}]

    def test_replay_refuses_an_invalid_route_before_touching_the_page(self):
        page = FakePage()
        out = replay(FakeUC(), page, [{"verb": "teleport"}])
        assert out.ok is False
        assert page.gotos == [], "nothing should have been navigated"

    def test_a_failed_step_stops_the_walk(self):
        """Continuing past a step that did not happen produces plausible lies."""
        page, uc = FakePage(), FakeUC()
        with patch.object(web_steps.W, "find_search_box", return_value=None), \
             patch.object(web_steps.W, "extract_links", return_value=[{"url": "https://e/1"}]):
            out = replay(uc, page, [
                {"verb": "fill", "target": "search", "text": "AI"},
                {"verb": "extract", "what": "links"},
            ], diagnose_on_failure=False)
        assert out.ok is False
        assert len(out.results) == 1, "the extract must not have run"
        assert out.failed_step.verb == "fill"

    def test_an_optional_step_may_fail_without_ending_the_route(self):
        page, uc = FakePage(), FakeUC()
        with patch.object(web_steps.W, "find_search_box", return_value=None), \
             patch.object(web_steps.W, "extract_links", return_value=[{"url": "https://e/1"}]):
            out = replay(uc, page, [
                {"verb": "fill", "target": "search", "text": "AI", "optional": True},
                {"verb": "extract", "what": "links"},
            ], diagnose_on_failure=False)
        assert out.ok is True
        assert out.links == [{"url": "https://e/1"}]

    def test_an_exception_in_a_step_is_a_failed_step_not_a_crash(self):
        out = replay(FakeUC(), FakePage(goto_raises=True),
                     [{"verb": "goto", "url": "https://nope.invalid"}],
                     diagnose_on_failure=False)
        assert out.ok is False
        assert "ERR_NAME_NOT_RESOLVED" in out.failed_step.detail

    def test_failure_attaches_a_diagnosis(self):
        """'0 events' and '0 events because a consent dialog was up' differ."""
        fake_diag = type("D", (), {"to_dict": lambda self: {"kind": "cookie_banner"}})()
        with patch.object(web_steps.W, "find_search_box", return_value=None), \
             patch("event_harvester.observability.hints.diagnose", return_value=fake_diag):
            out = replay(FakeUC(), FakePage(),
                         [{"verb": "fill", "target": "search", "text": "AI"}])
        assert out.diagnosis == {"kind": "cookie_banner"}

    def test_extract_dedupes_across_repeated_extracts(self):
        page, uc = FakePage(), FakeUC()
        with patch.object(web_steps.W, "extract_links",
                          return_value=[{"url": "https://e/1"}, {"url": "https://e/2"}]):
            out = replay(uc, page, [
                {"verb": "extract", "what": "links"},
                {"verb": "extract", "what": "links"},
            ])
        assert len(out.links) == 2, "the second pass found nothing new"
        assert out.results[1].detail.startswith("0 new")

    def test_dismissing_nothing_is_success(self):
        """Routes keep a dismiss step because the banner only shows sometimes."""
        uc = FakeUC()
        with patch.object(web_steps.W, "dismiss_modal", return_value=False):
            out = replay(uc, FakePage(), [{"verb": "dismiss"}])
        assert out.ok is True
        assert "nothing to dismiss" in out.results[0].detail

    def test_click_repeat_stops_when_the_control_disappears(self):
        """Pagination runs out; that is the end of the list, not an error."""
        elements = [Element("a.next", None, 0.8, "pagination"), None]
        with patch.object(web_steps.W, "find_pagination", side_effect=elements), \
             patch.object(web_steps.W, "click", return_value=True):
            out = replay(FakeUC(), FakePage(),
                         [{"verb": "click", "target": "pagination", "repeat": 5}])
        assert out.ok is True
        assert out.results[0].detail == "clicked 1/5"

    def test_feed_items_land_in_texts(self):
        uc = FakeUC(feed_items=["one post", "another post"])
        out = replay(uc, FakePage(), [{"verb": "extract", "what": "feed_items"}])
        assert out.texts == ["one post", "another post"]

    def test_to_dict_reports_where_it_broke(self):
        with patch.object(web_steps.W, "find_search_box", return_value=None):
            out = replay(FakeUC(), FakePage(),
                         [{"verb": "fill", "target": "search", "text": "x"}],
                         diagnose_on_failure=False)
        payload = out.to_dict()
        assert payload["failed_step"]["index"] == 0
        assert payload["ok"] is False

    def test_empty_result_starts_clean(self):
        out = ReplayResult(ok=True)
        assert out.failed_step is None and out.links == []
