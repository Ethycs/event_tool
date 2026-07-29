"""Tests for ``event_harvester.web_primitives``.

Covers the pure-function bits (``Element.to_dict``, ``_bbox_from_pattern``,
``_arbitrate``, ``_pattern_to_element``) and each public verb against
duck-typed fake UC / page objects.
"""

from __future__ import annotations

from typing import Any

from event_harvester.web_primitives import (
    CONFIDENCE_THRESHOLD,
    Element,
    _arbitrate,
    _bbox_from_pattern,
    _pattern_to_element,
    click,
    dismiss_modal,
    extract_event,
    extract_text,
    fill_input,
    find_chat_input,
    find_feed_container,
    find_login_wall,
    find_modal,
    find_pagination,
    find_search_box,
    scroll,
    screenshot,
    wait_for_navigation,
)


# ── Fakes ─────────────────────────────────────────────────────────


class FakeUC:
    """Duck-typed UCBrowser stand-in."""

    def __init__(self, *, patterns: dict | None = None, inputs: list[dict] | None = None,
                 login_wall: bool = False, ml_chat: dict | None = None,
                 cookies_closed: bool = False, modal_closed: bool = False) -> None:
        self._patterns = patterns or {}
        self._inputs = inputs or []
        self._login_wall = login_wall
        self._ml_chat = ml_chat
        self._cookies_closed = cookies_closed
        self._modal_closed = modal_closed
        self.dismiss_cookies_calls = 0
        self.close_modal_calls = 0

    def get_patterns(self, page) -> dict:
        return self._patterns

    def find_inputs(self, page) -> list[dict]:
        return list(self._inputs)

    def has_login_wall(self, page) -> bool:
        return self._login_wall

    def ml_find_chat(self, page) -> dict | None:
        return self._ml_chat

    def dismiss_cookies(self, page) -> bool:
        self.dismiss_cookies_calls += 1
        return self._cookies_closed

    def close_modal(self, page) -> bool:
        self.close_modal_calls += 1
        return self._modal_closed


class FakePage:
    """Duck-typed Playwright page."""

    def __init__(self, *, evaluate_results: list[Any] | None = None,
                 evaluate_raises: bool = False) -> None:
        self._evaluate_results = list(evaluate_results or [])
        self._evaluate_raises = evaluate_raises
        self.evaluate_calls: list[tuple[str, tuple]] = []
        self.click_calls: list[str] = []
        self.fill_calls: list[tuple[str, str]] = []
        self.press_calls: list[tuple[str, str]] = []
        self.wait_calls: list[int] = []
        self.wait_state_calls: list[tuple[str, int]] = []
        self.screenshot_calls = 0

    def evaluate(self, script: str, *args: Any) -> Any:
        self.evaluate_calls.append((script, args))
        if self._evaluate_raises:
            raise RuntimeError("simulated evaluate failure")
        if self._evaluate_results:
            return self._evaluate_results.pop(0)
        return None

    def click(self, selector: str) -> None:
        self.click_calls.append(selector)

    def fill(self, selector: str, text: str) -> None:
        self.fill_calls.append((selector, text))

    def press(self, selector: str, key: str) -> None:
        self.press_calls.append((selector, key))

    def wait_for_timeout(self, ms: int) -> None:
        self.wait_calls.append(ms)

    def wait_for_load_state(self, state: str, timeout: int = 0) -> None:
        self.wait_state_calls.append((state, timeout))

    def screenshot(self) -> bytes:
        self.screenshot_calls += 1
        return b"png-bytes"


# ── Pure-function bits ────────────────────────────────────────────


class TestElement:
    def test_to_dict_round_trip(self) -> None:
        el = Element(selector="#foo", bbox=(1, 2, 3, 4), confidence=0.8,
                     label="search", meta={"x": 1})
        d = el.to_dict()
        assert d == {
            "selector": "#foo", "bbox": [1, 2, 3, 4], "confidence": 0.8,
            "label": "search", "meta": {"x": 1},
        }

    def test_to_dict_bbox_none(self) -> None:
        el = Element(selector="#x", bbox=None, confidence=1.0, label="login_wall")
        assert el.to_dict()["bbox"] is None


class TestBboxFromPattern:
    def test_rect_dict_xywh(self) -> None:
        assert _bbox_from_pattern({"rect": {"x": 1, "y": 2, "w": 3, "h": 4}}) == (1, 2, 3, 4)

    def test_rect_dict_left_top_width_height(self) -> None:
        assert _bbox_from_pattern(
            {"bbox": {"left": 10, "top": 20, "width": 30, "height": 40}}
        ) == (10, 20, 30, 40)

    def test_rect_tuple(self) -> None:
        assert _bbox_from_pattern({"bounds": (5, 6, 7, 8, "extra")}) == (5, 6, 7, 8)

    def test_no_rect(self) -> None:
        assert _bbox_from_pattern({"selector": "#x"}) is None


class TestPatternToElement:
    def test_promotes_pattern(self) -> None:
        el = _pattern_to_element({"selector": "#a", "score": 0.7, "extra": "x"}, "search")
        assert el is not None
        assert el.selector == "#a"
        assert el.confidence == 0.7
        assert el.label == "search"
        assert el.meta == {"extra": "x", "score": 0.7}

    def test_missing_selector_drops(self) -> None:
        assert _pattern_to_element({"score": 0.9}, "search") is None

    def test_default_confidence(self) -> None:
        el = _pattern_to_element({"selector": "#a"}, "search")
        assert el is not None and el.confidence == 1.0


class TestArbitrate:
    def test_picks_highest_confidence(self) -> None:
        a = Element("#a", None, 0.3, "search")
        b = Element("#b", None, 0.9, "search")
        c = Element("#c", None, 0.5, "search")
        assert _arbitrate(page=None, candidates=[a, b, c], goal="x") is b

    def test_empty_returns_none(self) -> None:
        assert _arbitrate(page=None, candidates=[], goal="x") is None

    def test_threshold_constant_sane(self) -> None:
        # Sanity check — threshold should sit somewhere in the middle, not 0 or 1.
        assert 0.0 < CONFIDENCE_THRESHOLD < 1.0


# ── Discovery verbs ───────────────────────────────────────────────


class TestFindSearchBox:
    def test_returns_best_from_patterns(self) -> None:
        uc = FakeUC(patterns={"search": [
            {"selector": "#low", "score": 0.3},
            {"selector": "#high", "score": 0.9},
        ]})
        el = find_search_box(uc, FakePage())
        assert el is not None
        assert el.selector == "#high"
        assert el.label == "search"

    def test_falls_back_to_find_inputs(self) -> None:
        uc = FakeUC(patterns={"search": []}, inputs=[{"selector": "input.q", "score": 0.5}])
        el = find_search_box(uc, FakePage())
        assert el is not None
        assert el.selector == "input.q"

    def test_no_candidates_returns_none(self) -> None:
        assert find_search_box(FakeUC(), FakePage()) is None


class TestFindPagination:
    def test_returns_element_from_evaluate(self) -> None:
        page = FakePage(evaluate_results=[{
            "selector": "a.next",
            "score": 0.8,
            "rect": {"x": 100, "y": 200, "w": 50, "h": 20},
        }])
        el = find_pagination(page)
        assert el is not None
        assert el.selector == "a.next"
        assert el.label == "pagination"
        assert el.bbox == (100, 200, 50, 20)

    def test_none_when_evaluate_returns_none(self) -> None:
        assert find_pagination(FakePage(evaluate_results=[None])) is None

    def test_none_when_evaluate_raises(self) -> None:
        assert find_pagination(FakePage(evaluate_raises=True)) is None


class TestFindLoginWall:
    def test_returns_signal_when_walled(self) -> None:
        el = find_login_wall(FakeUC(login_wall=True), FakePage())
        assert el is not None and el.label == "login_wall"
        assert el.meta == {"signal_only": True}

    def test_none_when_not_walled(self) -> None:
        assert find_login_wall(FakeUC(login_wall=False), FakePage()) is None


class TestFindModal:
    def test_picks_from_modal_cookie_or_dialog(self) -> None:
        uc = FakeUC(patterns={
            "modal": [{"selector": "#m", "score": 0.4}],
            "cookie": [{"selector": "#c", "score": 0.8}],
        })
        el = find_modal(uc, FakePage())
        assert el is not None and el.selector == "#c"

    def test_none_when_no_patterns(self) -> None:
        assert find_modal(FakeUC(), FakePage()) is None


class TestFindFeedContainer:
    def test_returns_best(self) -> None:
        uc = FakeUC(patterns={"feed": [{"selector": "div.feed", "score": 0.7}]})
        el = find_feed_container(uc, FakePage())
        assert el is not None and el.selector == "div.feed"

    def test_none_when_missing(self) -> None:
        assert find_feed_container(FakeUC(), FakePage()) is None


class TestFindChatInput:
    def test_prefers_ml_hit(self) -> None:
        uc = FakeUC(
            ml_chat={"selector": "#ml", "score": 0.9},
            patterns={"chat": [{"selector": "#pat", "score": 0.5}]},
        )
        el = find_chat_input(uc, FakePage())
        assert el is not None and el.selector == "#ml"

    def test_falls_back_to_pattern(self) -> None:
        uc = FakeUC(ml_chat=None, patterns={"chat": [{"selector": "#pat", "score": 0.5}]})
        el = find_chat_input(uc, FakePage())
        assert el is not None and el.selector == "#pat"


# ── Action verbs ──────────────────────────────────────────────────


class TestClick:
    def test_clicks_selector(self) -> None:
        page = FakePage()
        el = Element("#go", None, 1.0, "x")
        assert click(page, el) is True
        assert page.click_calls == ["#go"]

    def test_empty_selector_returns_false(self) -> None:
        page = FakePage()
        assert click(page, Element("", None, 1.0, "x")) is False
        assert page.click_calls == []


class TestFillInput:
    def test_fills_and_optionally_submits(self) -> None:
        page = FakePage()
        el = Element("input.q", None, 1.0, "search")
        assert fill_input(page, el, "hello", submit=True) is True
        assert page.fill_calls == [("input.q", "hello")]
        assert page.press_calls == [("input.q", "Enter")]

    def test_no_submit_skips_press(self) -> None:
        page = FakePage()
        el = Element("input.q", None, 1.0, "search")
        fill_input(page, el, "x")
        assert page.press_calls == []


class TestScroll:
    def test_zero_seconds_no_ticks(self) -> None:
        assert scroll(FakePage(), None, 0) == 0

    def test_window_scroll_when_no_element(self, monkeypatch) -> None:
        page = FakePage()
        # Fast-forward time so the loop runs exactly once and exits.
        times = iter([0.0, 0.5, 99.0])
        import time as real_time
        monkeypatch.setattr(real_time, "time", lambda: next(times))
        ticks = scroll(page, None, seconds=1)
        assert ticks >= 1
        assert any("window.scrollBy" in script for script, _ in page.evaluate_calls)

    def test_container_scroll_uses_selector(self, monkeypatch) -> None:
        page = FakePage()
        times = iter([0.0, 0.5, 99.0])
        import time as real_time
        monkeypatch.setattr(real_time, "time", lambda: next(times))
        el = Element("div.feed", None, 1.0, "feed")
        scroll(page, el, seconds=1)
        assert any("div.feed" in str(args) for _, args in page.evaluate_calls)


class TestDismissModal:
    def test_returns_true_if_any_closes(self) -> None:
        uc = FakeUC(cookies_closed=True, modal_closed=False)
        assert dismiss_modal(uc, FakePage()) is True
        assert uc.dismiss_cookies_calls == 1
        assert uc.close_modal_calls == 1

    def test_returns_false_when_nothing_closes(self) -> None:
        uc = FakeUC()
        assert dismiss_modal(uc, FakePage()) is False


class TestWaitForNavigation:
    def test_calls_wait_for_load_state(self) -> None:
        page = FakePage()
        assert wait_for_navigation(page, timeout_ms=3000) is True
        assert page.wait_state_calls == [("networkidle", 3000)]

    def test_returns_false_on_timeout(self) -> None:
        class Boom(FakePage):
            def wait_for_load_state(self, state: str, timeout: int = 0) -> None:
                raise TimeoutError("nope")
        assert wait_for_navigation(Boom(), 1000) is False


# ── Extraction verbs ──────────────────────────────────────────────


class TestExtractText:
    def test_page_body_when_no_element(self) -> None:
        page = FakePage(evaluate_results=["body text"])
        assert extract_text(page) == "body text"

    def test_element_inner_text(self) -> None:
        page = FakePage(evaluate_results=["card text"])
        el = Element("div.card", None, 1.0, "card")
        assert extract_text(page, el) == "card text"

    def test_empty_string_on_failure(self) -> None:
        page = FakePage(evaluate_raises=True)
        assert extract_text(page) == ""


class TestExtractEvent:
    def test_pulls_from_meta(self) -> None:
        el = Element("a", None, 1.0, "event_card", meta={
            "title": "Demo", "date": "2026-06-01", "time": "18:00",
            "venue": "SF", "url": "https://x.test/e",
        })
        assert extract_event(el) == {
            "title": "Demo", "date": "2026-06-01", "time": "18:00",
            "location": "SF", "link": "https://x.test/e",
        }

    def test_missing_fields_become_none(self) -> None:
        el = Element("a", None, 1.0, "event_card", meta={})
        assert extract_event(el) == {
            "title": None, "date": None, "time": None, "location": None, "link": None,
        }


class TestScreenshot:
    def test_returns_bytes(self) -> None:
        assert screenshot(FakePage()) == b"png-bytes"

    def test_empty_on_failure(self) -> None:
        class Boom(FakePage):
            def screenshot(self) -> bytes:
                raise RuntimeError("nope")
        assert screenshot(Boom()) == b""
