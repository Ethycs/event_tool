"""`/sources` — three kinds, a form that proposes, and a log of attempts.

**Every test redirects `ATTEMPTS_FILE`.** `data_path` anchors to the repo root,
not the working directory, so a `monkeypatch.chdir` is not isolation — that
mistake overwrote a real `data/keywords.json` on 2026-08-12 and it is exactly
the shape a page with a write path invites.

Nothing here reaches the network. Exploring an already-configured URL
short-circuits before the first fetch, which is both the cheap way to test the
route and a property worth pinning on its own.
"""

from __future__ import annotations

import json

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from event_harvester.servers import sources_page
from event_harvester.sources.onboard import SOURCE_KINDS, classify_kind


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """No test may touch the real attempt log."""
    monkeypatch.setattr(sources_page, "ATTEMPTS_FILE", tmp_path / "attempts.json")


@pytest.fixture
def client():
    return TestClient(Starlette(routes=sources_page.routes()))


class TestTheThreeKinds:
    def test_a_bot_challenge_outranks_everything_else(self):
        """erobay answers 403 to a plain request and produced 43 of the last
        harvest's 244 events. How it must be *run* outranks how it is parsed,
        so a feed on the same host would not make it `direct`."""
        assert classify_kind(mode="calendar", native_chrome=True) == "special"

    def test_a_platform_with_its_own_mode_is_special(self):
        for mode in ("luma", "partiful", "instagram", "feed"):
            assert classify_kind(mode=mode) == "special", mode

    def test_a_source_that_must_be_asked_is_search(self):
        assert classify_kind(mode="search") == "search"
        assert classify_kind(mode="calendar", has_query=True) == "search"

    def test_a_plain_calendar_or_feed_is_direct(self):
        for mode in ("ics", "rss", "calendar", "raw"):
            assert classify_kind(mode=mode) == "direct", mode

    def test_every_kind_is_explained_on_the_page(self):
        """A grouping with no stated rule is a grouping people argue with."""
        assert set(SOURCE_KINDS) == {"direct", "search", "special"}
        assert all(len(v) > 40 for v in SOURCE_KINDS.values())


class TestThePage:
    def test_it_serves_html_with_the_shared_nav(self, client):
        r = client.get("/sources")
        assert r.status_code == 200
        assert "<title>Sources</title>" in r.text
        assert 'class="sitenav"' in r.text

    def test_the_nav_marks_this_page_current(self, client):
        assert 'aria-current="page"' in client.get("/sources").text

    def test_its_fetches_are_relative_so_a_subpath_mount_works(self, client):
        """The site is reached at `central-dev.zt/harvester` through a proxy.
        A root-relative fetch would resolve against the proxy's root."""
        body = client.get("/sources").text
        assert "fetch('sources/api/state')" in body
        assert 'fetch("/sources' not in body and "fetch('/sources" not in body


class TestState:
    def test_it_groups_every_configured_source(self, client):
        r = client.get("/sources/api/state").json()
        assert r["ok"] and r["sources"]
        assert set(r["counts"]) == set(SOURCE_KINDS)
        assert sum(r["counts"].values()) == len(r["sources"])

    def test_every_source_carries_its_kind(self, client):
        for s in client.get("/sources/api/state").json()["sources"]:
            assert s["kind"] in SOURCE_KINDS

    def test_special_sources_say_why(self, client):
        """"special" with no reason is a bucket, not a classification."""
        specials = [s for s in client.get("/sources/api/state").json()["sources"]
                    if s["kind"] == "special"]
        assert specials, "the real config has special sources"
        assert any(s["why"] for s in specials)


class TestExamining:
    def test_an_empty_url_is_rejected(self, client):
        assert client.post("/sources/api/explore", json={}).status_code == 400

    def test_an_absurd_url_is_rejected_before_any_fetch(self, client):
        r = client.post("/sources/api/explore", json={"url": "http://x/" + "a" * 600})
        assert r.status_code == 400

    def test_an_already_configured_url_short_circuits(self, client):
        """No network: `explore` returns before the first fetch."""
        from event_harvester.sources.web_utils import _load_web_sources
        url = (_load_web_sources() or [{}])[0].get("url")
        assert url, "the real config has sources"
        r = client.post("/sources/api/explore", json={"url": url}).json()
        assert r["ok"] and r["outcome"] == "already-configured"
        assert r["already_configured"]


class TestAttempts:
    def test_an_examination_is_recorded(self, client):
        from event_harvester.sources.web_utils import _load_web_sources
        url = (_load_web_sources() or [{}])[0].get("url")
        client.post("/sources/api/explore", json={"url": url})
        attempts = client.get("/sources/api/attempts").json()["attempts"]
        assert attempts and attempts[0]["url"] == url
        assert attempts[0]["outcome"] == "already-configured"

    def test_newest_first(self, client, tmp_path):
        sources_page.record_attempt({"at": "2026-01-01T00:00:00Z", "url": "a"})
        sources_page.record_attempt({"at": "2026-01-02T00:00:00Z", "url": "b"})
        assert [a["url"] for a in sources_page.load_attempts()] == ["b", "a"]

    def test_the_log_is_bounded(self, monkeypatch):
        """An unbounded JSON file read on every page load is a slow leak."""
        monkeypatch.setattr(sources_page, "MAX_ATTEMPTS", 3)
        for i in range(6):
            sources_page.record_attempt({"at": f"t{i}", "url": str(i)})
        assert len(sources_page.load_attempts()) == 3

    def test_a_rejected_source_is_kept_too(self):
        """One you tried and rejected is information the next person needs more
        than one you added — and until now it lived in a terminal scrollback."""
        sources_page.record_attempt(
            {"at": "t", "url": "u", "outcome": "needs-agent"})
        assert sources_page.load_attempts()[0]["outcome"] == "needs-agent"

    def test_an_empty_log_is_not_an_error(self):
        assert sources_page.load_attempts() == []


class TestItProposesButNeverConfigures:
    def test_no_route_writes_the_source_config(self):
        """A page that could add a source on the strength of a scoring function
        would be a page that adds sources nobody proved. Blueprint step 7 — a
        real --dry-run producing real events — is not something a page does."""
        import inspect
        src = inspect.getsource(sources_page)
        assert "web_sources.json" not in src.replace(
            "Nothing here writes `web_sources.json`", "").replace(
            "it never writes `web_sources.json`", "")

    def test_the_only_write_target_is_the_attempt_log(self):
        import inspect
        src = inspect.getsource(sources_page)
        assert src.count("save_json(") == 1

    def test_the_page_says_so_to_the_reader(self):
        assert "does not configure anything" in sources_page.render_page()


class TestTheAgentOfframpReachesThePage:
    def test_a_stuck_report_carries_a_prompt(self, monkeypatch, client):
        """The brief has to arrive as text the operator can hand over, not as
        a flag saying it was needed."""
        from event_harvester.sources.onboard import OnboardReport, PageEvidence

        blank = PageEvidence(url="https://v.test/a", ok=True, chars=800)
        stuck = OnboardReport(
            given_url="https://v.test", final_url="https://v.test",
            candidates=(blank,), best=blank, needs_browser=True, mode="raw",
        )
        monkeypatch.setattr(
            "event_harvester.sources.onboard.explore", lambda url, **kw: stuck)
        r = client.post("/sources/api/explore", json={"url": "https://v.test"}).json()
        assert r["outcome"] == "needs-agent"
        assert "MULTIPLE dated" in r["agent_prompt"]
        assert json.dumps(r["agent_brief"])
