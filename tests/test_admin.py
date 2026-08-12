"""Tests for `/admin` — the caps equalizer, keyword lists and profile editor.

These routes had **no tests at all** until 2026-08-12, which is how a real
clamping defect survived: `api_profiles` normalised caps with `max(0, ...)`,
so a profile carrying `-1` (unlimited) would be rewritten as `0` (drop this
source entirely) the moment it was saved. Nothing surfaced that — it fails
hours later, at the next harvest, as a source that simply produced nothing.
`TestProfileCaps` is the regression.

**The two config stores are anchored differently, and it matters here.**
`data/profiles.json` and `data/settings.json` are opened CWD-relative, so
`monkeypatch.chdir` isolates them. `data/keywords.json` is **repo-anchored**
via `utils.data_path`, so chdir does not isolate it at all — the first draft of
this file posted to `/admin/api/keywords` and overwrote the operator's real
keyword lists, which are untracked and had no git copy to restore from. The
`isolated` fixture redirects `keywords.KEYWORDS_FILE` explicitly. Do not write
a test in this file that skips it.
"""

from __future__ import annotations

import json

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from event_harvester import keywords as keywords_mod
from event_harvester.servers.admin import (
    CAP_SOURCES,
    build_routes,
    current_caps,
    render_page,
    search_plan,
)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Point every config store at `tmp_path`. Autouse — see the module note."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    monkeypatch.setattr(keywords_mod, "KEYWORDS_FILE", tmp_path / "data" / "keywords.json")
    return tmp_path


@pytest.fixture
def client(isolated):
    return TestClient(Starlette(routes=build_routes()))


def _write_profiles(tmp_path, profiles):
    (tmp_path / "data" / "profiles.json").write_text(
        json.dumps({"profiles": profiles}), encoding="utf-8")


class TestCaps:
    def test_defaults_are_unlimited(self):
        """Axiom 8: discard at the view, not in the pipeline."""
        assert all(v == -1 for v in current_caps().values())

    def test_an_overlay_wins(self, client, isolated):
        r = client.post("/admin/api/caps", json={"caps": {"web": 40, "total": -1}})
        assert r.status_code == 200 and r.json()["ok"]
        assert json.loads((isolated / "data" / "settings.json")
                          .read_text())["caps"]["web"] == 40
        assert current_caps()["web"] == 40

    def test_a_negative_stays_unlimited_rather_than_becoming_zero(self, client):
        """-5 is a typo for "no limit", never for "drop this source"."""
        r = client.post("/admin/api/caps", json={"caps": {"web": -5}})
        assert r.json()["caps"]["web"] == -1

    def test_zero_survives_because_it_means_something_else(self, client):
        r = client.post("/admin/api/caps", json={"caps": {"gmail": 0}})
        assert r.json()["caps"]["gmail"] == 0

    def test_unknown_keys_are_dropped(self, client):
        r = client.post("/admin/api/caps", json={"caps": {"nonsense": 9}})
        assert "nonsense" not in r.json()["caps"]

    def test_bad_bodies_are_rejected_not_crashed(self, client):
        assert client.post("/admin/api/caps", content="{").status_code == 400
        assert client.post("/admin/api/caps", json=[1, 2]).status_code == 400


class TestProfileCaps:
    """The defect this file was written for."""

    def test_an_unlimited_cap_survives_a_round_trip(self, client, isolated):
        _write_profiles(isolated, [
            {"name": "wide", "web_sources": ["luma"], "caps": {"web": -1}},
        ])
        saved = client.get("/admin/api/state").json()["profiles"]
        r = client.post("/admin/api/profiles", json={"profiles": saved})
        assert r.status_code == 200
        assert r.json()["profiles"][0]["caps"]["web"] == -1, \
            "saving a profile turned 'unlimited' into 'drop this source'"

    def test_a_cap_is_still_bounded_above(self, client):
        r = client.post("/admin/api/profiles", json={"profiles": [
            {"name": "greedy", "caps": {"web": 10_000_000}}]})
        assert r.json()["profiles"][0]["caps"]["web"] == 5000

    def test_zero_still_means_drop_this_source(self, client):
        r = client.post("/admin/api/profiles", json={"profiles": [
            {"name": "noweb", "caps": {"web": 0}}]})
        assert r.json()["profiles"][0]["caps"]["web"] == 0


class TestProfiles:
    def test_saving_writes_the_file(self, client, isolated):
        r = client.post("/admin/api/profiles", json={"profiles": [
            {"name": "sf-local", "description": "just SF",
             "web_sources": ["erobay"], "keywords": ["queer"]}]})
        assert r.status_code == 200
        on_disk = json.loads((isolated / "data" / "profiles.json").read_text())
        assert on_disk["profiles"][0]["name"] == "sf-local"
        assert on_disk["profiles"][0]["keywords"] == ["queer"]

    def test_a_bad_name_is_a_400_not_a_written_file(self, client, isolated):
        r = client.post("/admin/api/profiles",
                        json={"profiles": [{"name": "Not A Slug!"}]})
        assert r.status_code == 400
        assert not (isolated / "data" / "profiles.json").exists()

    def test_non_dict_entries_are_skipped(self, client):
        r = client.post("/admin/api/profiles",
                        json={"profiles": ["nope", {"name": "ok"}]})
        assert [p["name"] for p in r.json()["profiles"]] == ["ok"]

    def test_the_wrong_shape_is_rejected(self, client):
        assert client.post("/admin/api/profiles", json={"profiles": {}}).status_code == 400


class TestKeywords:
    def test_terms_are_normalised_on_save(self, client, isolated):
        r = client.post("/admin/api/keywords", json={"keywords": [
            {"name": "ai", "terms": [" AI ", "AI", "", "machine learning"]}]})
        assert r.status_code == 200
        terms = r.json()["keywords"][0]["terms"]
        assert "AI" in terms and "" not in terms
        assert len(terms) == len(set(t.lower() for t in terms)), "duplicates kept"
        # Written to the redirected store, never the repo's own.
        assert (isolated / "data" / "keywords.json").exists()

    def test_a_bad_name_is_a_400(self, client):
        assert client.post("/admin/api/keywords",
                           json={"keywords": [{"name": ""}]}).status_code == 400


class TestSearchPlan:
    def test_none_means_none(self):
        """`__none__` and an empty list are different answers: "skip web
        entirely" versus "all of it"."""
        assert search_plan({"web_sources": ["__none__"]}) == []

    def test_naming_sources_narrows_to_them(self):
        """`data/web_sources.json` is repo-anchored (System Overview axiom 5),
        so this reads the real source list on purpose — the plan is a view of
        what is configured, and pinning it to a fixture would test the
        fixture."""
        everything = search_plan()
        assert everything, "no web sources configured — this test needs some"
        one = search_plan({"web_sources": [everything[0]["source"]]})
        assert [p["source"] for p in one] == [everything[0]["source"]]

    def test_every_plan_declares_its_strength(self):
        """The point of the panel: the platforms are not equivalent, and a UI
        that implied they were would be lying."""
        for plan in search_plan():
            assert plan["strength"] in ("native", "category", "local")


class TestPage:
    def test_it_renders_with_every_panel(self, client):
        body = client.get("/admin").text
        assert client.get("/admin").status_code == 200
        for heading in ("Caps", "Keywords", "Profiles", "Searches"):
            assert f"<h2>{heading}</h2>" in body

    def test_it_carries_the_shared_nav(self, client):
        """Relative hrefs — this origin is also reached through a proxy at
        `/harvester`. See `tests/test_subpath_mount.py`."""
        body = client.get("/admin").text
        assert 'href="calendar"' in body and 'href="."' in body
        assert 'href="admin" title="Caps, keyword lists and profiles" '\
               'aria-current="page"' in body

    def test_the_nav_style_is_substituted_not_left_as_a_marker(self):
        page = render_page()
        assert "/*NAV-STYLE*/" not in page and "<!--NAV-->" not in page
        assert ".sitenav" in page

    def test_the_equalizer_is_built_from_the_api_not_the_markup(self, client):
        """The sliders are rendered client-side from `cap_sources`, so the
        source names are absent from the HTML and present in the state."""
        assert "cap-${name}" in client.get("/admin").text
        assert client.get("/admin/api/state").json()["cap_sources"] == list(CAP_SOURCES)

    def test_unlimited_is_a_toggle_not_a_slider_position(self, client):
        """-1 is a quantity a slider cannot express, and it is the default."""
        body = client.get("/admin").text
        assert 'class="inf"' in body and "&#8734;" in body

    def test_it_is_never_cached(self, client):
        assert client.get("/admin").headers["cache-control"] == "no-store"

    def test_state_reports_everything_the_page_needs(self, client):
        state = client.get("/admin/api/state").json()
        for key in ("caps", "cap_sources", "cap_max", "keywords", "profiles",
                    "sources", "plan"):
            assert key in state
