"""Tests for ``event_harvester.sources.web_explore``.

No browser is opened anywhere in this file: sessions are constructed directly
around fakes and injected into the registry. `open_session` itself is the one
thing left unexercised, because exercising it means launching Chromium.

`WEB_SOURCES_PATH` is redirected to a tmp file in every test that writes. The
real `data/web_sources.json` is 29 hand-curated sources and a test that
appended to it would be a silent data change nobody would look for.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from event_harvester.sources import web_explore, web_steps
from event_harvester.sources.web_explore import ExploreSession
from event_harvester.web_primitives import Element


class FakePage:
    def __init__(self):
        self.closed = False
        self.waits: list[int] = []

    def wait_for_timeout(self, ms):
        self.waits.append(ms)

    def evaluate(self, *_a, **_kw):
        return {"url": "https://example.com", "title": "Ex", "text_len": 5000,
                "text_sample": "events tonight", "links": 40, "inputs": 0,
                "password_inputs": 0, "search_inputs": 0, "forms": 0, "iframes": 0,
                "dialogs": 0, "next_controls": 0, "scroll_height": 2000,
                "viewport_height": 1000}

    def click(self, selector):
        pass

    def close(self):
        self.closed = True


class FakeUC:
    def __init__(self):
        self.exited = False

    def dismiss_cookies(self, page):
        return True

    def close_modal(self, page):
        return False

    def __exit__(self, *_a):
        self.exited = True
        return False


@pytest.fixture(autouse=True)
def _clean_registry():
    """`_SESSIONS` is module-global; a leaked entry fails an unrelated test later."""
    web_explore._SESSIONS.clear()
    yield
    web_explore._SESSIONS.clear()


@pytest.fixture
def session():
    """A live-looking session in the registry, torn down afterwards."""
    s = ExploreSession(
        session_id="abc123", url="https://example.com",
        uc=FakeUC(), page=FakePage(),
        steps=[{"verb": "goto", "url": "https://example.com"}],
    )
    web_explore._SESSIONS["abc123"] = s
    yield s
    web_explore._SESSIONS.pop("abc123", None)


@pytest.fixture
def sources_file(tmp_path, monkeypatch):
    path = tmp_path / "web_sources.json"
    path.write_text(json.dumps([{"url": "https://old.example", "name": "existing", "mode": "raw"}]))
    monkeypatch.setattr(web_explore, "WEB_SOURCES_PATH", path)
    return path


class TestAct:
    def test_only_successful_steps_are_recorded(self):
        """Replay cannot tell an attempt from an event, so attempts are not kept."""
        s = ExploreSession("s1", "https://e", FakeUC(), FakePage(),
                           steps=[{"verb": "goto", "url": "https://e"}])
        web_explore._SESSIONS["s1"] = s
        try:
            with patch.object(web_steps.W, "find_search_box", return_value=None):
                out = web_explore.act("s1", {"verb": "fill", "target": "search", "text": "AI"},
                                      describe_after=False)
            assert out["ok"] is False
            assert out["recorded"] is False
            assert len(s.steps) == 1
            assert len(s.history) == 1, "it is still in the history"
        finally:
            web_explore._SESSIONS.pop("s1", None)

    def test_a_successful_step_lands_in_the_route(self, session):
        with patch.object(web_steps.W, "find_search_box",
                          return_value=Element("input#q", None, 0.9, "search")), \
             patch.object(web_steps.W, "fill_input", return_value=True):
            out = web_explore.act("abc123", {"verb": "fill", "target": "search", "text": "AI"},
                                  describe_after=False)
        assert out["ok"] is True and out["recorded"] is True
        assert session.steps[-1]["text"] == "AI"

    def test_an_invalid_step_is_rejected_before_it_touches_the_page(self, session):
        out = web_explore.act("abc123", {"verb": "teleport"}, describe_after=False)
        assert out["ok"] is False
        assert "teleport" in out["error"]
        assert len(session.steps) == 1

    def test_an_unknown_session_raises_with_the_open_ones_named(self):
        with pytest.raises(KeyError, match="no explore session"):
            web_explore.act("nope", {"verb": "dismiss"})


class TestDescribe:
    def test_describe_reports_targets_and_never_pays_for_vision_when_told_not_to(self, session):
        with patch.object(web_steps.W, "find_search_box",
                          return_value=Element("input#q", None, 0.87, "search")), \
             patch.object(web_steps.W, "find_pagination", return_value=None), \
             patch.object(web_steps.W, "find_modal", return_value=None), \
             patch.object(web_steps.W, "find_feed_container", return_value=None), \
             patch.object(web_steps.W, "find_chat_input", return_value=None), \
             patch.object(web_steps.W, "extract_links", return_value=[{"url": "https://e/1"}]), \
             patch("event_harvester.observability.hints.vlm_hint") as vlm:
            out = web_explore.describe("abc123", use_vlm=False)
        vlm.assert_not_called()
        assert out["targets"]["search"]["selector"] == "input#q"
        assert out["targets"]["pagination"] is None
        assert out["event_links_visible"] == 1
        assert "summary" in out

    def test_a_target_that_raises_reports_as_absent(self, session):
        with patch.object(web_steps.W, "find_search_box", side_effect=RuntimeError("uc died")), \
             patch.object(web_steps.W, "find_pagination", return_value=None), \
             patch.object(web_steps.W, "find_modal", return_value=None), \
             patch.object(web_steps.W, "find_feed_container", return_value=None), \
             patch.object(web_steps.W, "find_chat_input", return_value=None), \
             patch.object(web_steps.W, "extract_links", return_value=[]):
            out = web_explore.describe("abc123", use_vlm=False)
        assert out["targets"]["search"] is None


class TestCrystallize:
    def test_a_bare_walk_crystallizes_to_the_old_shape(self, session):
        """One goto and nothing else is exactly the erobay case."""
        built = web_explore.crystallize("abc123", "example")
        assert built["ok"] is True
        assert built["source"] == {"url": "https://example.com", "name": "example", "mode": "raw"}
        assert "steps" not in built["source"]

    def test_a_real_walk_crystallizes_to_a_route(self, session):
        session.steps.extend([
            {"verb": "dismiss"},
            {"verb": "fill", "target": "search", "text": "AI", "submit": True},
            {"verb": "extract", "what": "links"},
        ])
        built = web_explore.crystallize("abc123", "meetup-ai", category="tech")
        assert built["source"]["mode"] == "steps"
        assert built["source"]["category"] == "tech"
        assert len(built["source"]["steps"]) == 3
        assert built["source"]["steps"][0] == {"verb": "dismiss"}
        assert built["source"]["url"] == "https://example.com", "the opening goto becomes the url"


class TestSaveSource:
    def test_saving_appends(self, session, sources_file):
        session.steps.append({"verb": "extract", "what": "links"})
        out = web_explore.save_source("abc123", "newsite")
        assert out["ok"] is True and out["total_sources"] == 2
        written = json.loads(sources_file.read_text())
        assert written[-1]["name"] == "newsite"
        assert written[0]["name"] == "existing", "the existing source is untouched"

    def test_saving_over_an_existing_name_is_refused_by_default(self, session, sources_file):
        """Adding a source must never be able to silently remove one."""
        out = web_explore.save_source("abc123", "existing")
        assert out["ok"] is False
        assert "already exists" in out["error"]
        assert out["existing"]["url"] == "https://old.example"
        assert json.loads(sources_file.read_text()) == [
            {"url": "https://old.example", "name": "existing", "mode": "raw"}
        ]

    def test_replace_is_explicit_and_returns_what_it_overwrote(self, session, sources_file):
        out = web_explore.save_source("abc123", "existing", replace=True)
        assert out["ok"] is True
        assert out["replaced"]["url"] == "https://old.example"
        assert len(json.loads(sources_file.read_text())) == 1

    def test_expect_min_events_rides_along(self, session, sources_file):
        out = web_explore.save_source("abc123", "tripwired", expect_min_events=5)
        assert out["source"]["expect_min_events"] == 5


class TestSessionLifecycle:
    def test_the_session_cap_is_enforced_before_a_browser_is_launched(self, monkeypatch):
        monkeypatch.setattr(web_explore, "MAX_SESSIONS", 1)
        s = ExploreSession("full", "https://e", FakeUC(), FakePage())
        web_explore._SESSIONS["full"] = s
        try:
            with pytest.raises(RuntimeError, match="already open"):
                web_explore.open_session("https://another.example")
        finally:
            web_explore._SESSIONS.pop("full", None)

    def test_closing_tears_down_the_browser(self):
        page, uc = FakePage(), FakeUC()
        web_explore._SESSIONS["bye"] = ExploreSession("bye", "https://e", uc, page)
        out = web_explore.close_session("bye")
        assert out["ok"] is True
        assert page.closed is True and uc.exited is True
        assert "bye" not in web_explore._SESSIONS

    def test_closing_an_unknown_session_is_not_an_error(self):
        assert web_explore.close_session("ghost")["ok"] is False

    def test_idle_sessions_are_reaped(self):
        page, uc = FakePage(), FakeUC()
        stale = ExploreSession("stale", "https://e", uc, page)
        stale.last_used -= web_explore.IDLE_TIMEOUT_S + 1
        web_explore._SESSIONS["stale"] = stale
        assert web_explore.list_sessions() == []
        assert page.closed is True, "a leaked browser is a real process"

    def test_a_session_in_use_is_not_reaped(self):
        page, uc = FakePage(), FakeUC()
        web_explore._SESSIONS["fresh"] = ExploreSession("fresh", "https://e", uc, page)
        assert [s["session_id"] for s in web_explore.list_sessions()] == ["fresh"]
        assert page.closed is False

    def test_teardown_survives_a_page_that_is_already_gone(self):
        class Broken(FakePage):
            def close(self):
                raise RuntimeError("target closed")

        uc = FakeUC()
        web_explore._SESSIONS["broken"] = ExploreSession("broken", "https://e", uc, Broken())
        assert web_explore.close_session("broken")["ok"] is True
        assert uc.exited is True, "the browser is still exited after the page fails"

    def test_close_all_reports_how_many(self):
        for i in range(2):
            web_explore._SESSIONS[f"n{i}"] = ExploreSession(
                f"n{i}", "https://e", FakeUC(), FakePage(),
            )
        assert web_explore.close_all() == 2
        assert web_explore._SESSIONS == {}


class TestUndo:
    def test_undo_drops_the_last_step_and_says_the_page_did_not_move(self, session):
        session.steps.append({"verb": "dismiss"})
        out = web_explore.undo("abc123")
        assert out["ok"] is True
        assert out["dropped"] == {"verb": "dismiss"}
        assert "not rewound" in out["note"]

    def test_undo_will_not_drop_the_opening_goto(self, session):
        out = web_explore.undo("abc123")
        assert out["ok"] is False
        assert len(session.steps) == 1
