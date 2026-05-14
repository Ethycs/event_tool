"""Tests for the named pipeline profile loader and applier."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from event_harvester.config import load_config
from event_harvester.profiles import (
    Profile,
    ProfileError,
    apply_profile,
    apply_profile_by_name,
    find_profile,
    load_profiles,
)


def _write(tmp: Path, data: dict) -> Path:
    p = tmp / "profiles.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


class TestLoadProfiles:
    def test_missing_file_returns_empty_list(self, tmp_path):
        assert load_profiles(tmp_path / "nope.json") == []

    def test_basic(self, tmp_path):
        path = _write(tmp_path, {
            "profiles": [
                {"name": "a", "description": "A", "web_sources": ["luma"], "caps": {"web": 10}},
                {"name": "b", "web_sources": ["meetup"]},
            ],
        })
        profiles = load_profiles(path)
        assert [p.name for p in profiles] == ["a", "b"]
        assert profiles[0].caps == {"web": 10}
        assert profiles[0].description == "A"
        assert profiles[1].description == ""
        assert profiles[1].caps == {}

    def test_malformed_json_raises(self, tmp_path):
        p = tmp_path / "profiles.json"
        p.write_text("not json", encoding="utf-8")
        with pytest.raises(ProfileError):
            load_profiles(p)

    def test_missing_top_level_list_raises(self, tmp_path):
        path = _write(tmp_path, {"profile": []})
        with pytest.raises(ProfileError):
            load_profiles(path)

    def test_duplicate_names_raises(self, tmp_path):
        path = _write(tmp_path, {
            "profiles": [
                {"name": "x"},
                {"name": "x"},
            ],
        })
        with pytest.raises(ProfileError):
            load_profiles(path)

    def test_missing_name_raises(self, tmp_path):
        path = _write(tmp_path, {"profiles": [{"description": "no name"}]})
        with pytest.raises(ProfileError):
            load_profiles(path)


class TestFindProfile:
    def test_match(self):
        ps = [Profile(name="a"), Profile(name="b")]
        assert find_profile(ps, "a").name == "a"
        assert find_profile(ps, "b").name == "b"

    def test_no_match(self):
        ps = [Profile(name="a")]
        assert find_profile(ps, "nope") is None

    def test_empty(self):
        assert find_profile([], "anything") is None


class TestApplyProfile:
    def test_sets_web_sources_and_caps(self, monkeypatch):
        cfg = load_config()
        # Snapshot a few defaults to ensure they survive
        initial_discord = cfg.caps.discord
        initial_days = cfg.days_back

        profile = Profile(
            name="t",
            web_sources=["luma", "eventbrite"],
            caps={"web": 60, "total": 120, "luma": 25},
        )
        apply_profile(cfg, profile)

        assert cfg.web.selected_names == ["luma", "eventbrite"]
        assert cfg.caps.web == 60
        assert cfg.caps.total == 120
        # Dynamic per-sub-source cap via setattr is intentional.
        assert getattr(cfg.caps, "luma") == 25
        # Untouched fields keep their original values.
        assert cfg.caps.discord == initial_discord
        assert cfg.days_back == initial_days

    def test_does_not_touch_sources_or_behavior(self):
        cfg = load_config()
        cfg.sources.discord = False
        cfg.sources.telegram = True
        cfg.skip_analyze = True
        cfg.dry_run = False

        profile = Profile(name="t", web_sources=["luma"], caps={"web": 50})
        apply_profile(cfg, profile)

        assert cfg.sources.discord is False
        assert cfg.sources.telegram is True
        assert cfg.skip_analyze is True
        assert cfg.dry_run is False

    def test_empty_web_sources_means_all(self):
        cfg = load_config()
        cfg.web.selected_names = ["leftover"]
        apply_profile(cfg, Profile(name="t", web_sources=[], caps={}))
        # The profile's empty list is applied literally — selected_names
        # becomes empty, which downstream treats as "all sources".
        assert cfg.web.selected_names == []

    def test_no_cap_flag_via_caps(self):
        cfg = load_config()
        apply_profile(cfg, Profile(name="t", caps={"no_cap": True}))
        assert cfg.caps.no_cap is True


class TestApplyProfileByName:
    def test_success(self, tmp_path, monkeypatch):
        path = _write(tmp_path, {
            "profiles": [
                {"name": "t", "web_sources": ["luma"], "caps": {"web": 42}},
            ],
        })
        monkeypatch.setattr("event_harvester.profiles.PROFILES_FILE", path)
        cfg = load_config()
        assert apply_profile_by_name(cfg, "t") is True
        assert cfg.web.selected_names == ["luma"]
        assert cfg.caps.web == 42

    def test_missing_name_returns_false(self, tmp_path, monkeypatch):
        path = _write(tmp_path, {"profiles": [{"name": "x"}]})
        monkeypatch.setattr("event_harvester.profiles.PROFILES_FILE", path)
        cfg = load_config()
        assert apply_profile_by_name(cfg, "nope") is False

    def test_missing_file_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "event_harvester.profiles.PROFILES_FILE", tmp_path / "nope.json",
        )
        cfg = load_config()
        assert apply_profile_by_name(cfg, "anything") is False
