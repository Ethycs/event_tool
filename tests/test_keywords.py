"""Tests for keyword lists, the per-platform search plan, and the /admin API.

The design point these pin down is that **a keyword does not mean the same
thing on every platform** — Meetup can be typed into, Luma cannot (its search
endpoint answers "You are not signed in") and Partiful has no query surface at
all. `plan_for_source` is what reports that honestly, so most of these tests
are about it refusing to overstate what it can do.
"""

from __future__ import annotations

import json

import pytest

from event_harvester import keywords as kw
from event_harvester.config import CAP_MAX, load_caps_overlay, save_caps_overlay


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(kw, "KEYWORDS_FILE", tmp_path / "keywords.json")
    yield tmp_path


class TestNames:
    @pytest.mark.parametrize("name", ["ai", "queer-nightlife", "a1", "x" * 39])
    def test_good_names(self, name):
        assert kw.validate_name(name) == name

    @pytest.mark.parametrize("bad", ["", " ", "-leading", "has space", "UPPER!",
                                     "x" * 40, "../escape", "a/b"])
    def test_bad_names_are_rejected(self, bad):
        with pytest.raises(kw.KeywordError):
            kw.validate_name(bad)

    def test_case_and_padding_are_normalised(self):
        assert kw.validate_name("  AI  ") == "ai"


class TestTerms:
    def test_blanks_and_dupes_go(self):
        assert kw.normalise_terms(["AI", "", "  ", "ai", "ML"]) == ["AI", "ML"]

    def test_order_is_preserved(self):
        """It is the order a search-mode source types them."""
        assert kw.normalise_terms(["z", "a", "m"]) == ["z", "a", "m"]

    def test_whitespace_is_collapsed(self):
        assert kw.normalise_terms(["machine    learning"]) == ["machine learning"]

    def test_a_bare_string_is_accepted(self):
        assert kw.normalise_terms("AI") == ["AI"]

    def test_absurdly_long_terms_are_dropped(self):
        assert kw.normalise_terms(["x" * 81]) == []


class TestMatching:
    def test_short_keywords_do_not_fire_on_substrings(self):
        """The whole reason matching is not `in`: "AI" inside "chain"."""
        assert kw.match_terms("blockchain and said things", ["AI"]) == []

    def test_a_real_mention_matches(self):
        assert kw.match_terms("An AI meetup", ["AI"]) == ["AI"]

    def test_matching_is_case_insensitive(self):
        assert kw.match_terms("an ai meetup", ["AI"]) == ["AI"]

    def test_multi_word_terms_match(self):
        assert kw.match_terms("a machine learning talk", ["machine learning"]) \
            == ["machine learning"]

    def test_regex_metacharacters_are_literal(self):
        assert kw.match_terms("c++ workshop", ["c++"]) == ["c++"]

    def test_results_follow_term_order(self):
        assert kw.match_terms("ML and AI", ["AI", "ML"]) == ["AI", "ML"]


class TestTagging:
    def test_matches_are_tagged_and_nothing_is_dropped(self):
        msgs = [{"content": "AI night"}, {"content": "pottery"}]
        assert kw.tag_messages(msgs, ["AI"]) == 1
        assert msgs[0]["_keywords"] == ["AI"]
        assert "_keywords" not in msgs[1]
        assert len(msgs) == 2, "tagging must never filter"

    def test_no_terms_is_a_no_op(self):
        msgs = [{"content": "AI"}]
        assert kw.tag_messages(msgs, []) == 0
        assert "_keywords" not in msgs[0]


class TestStore:
    def test_round_trip(self):
        kw.save_keyword_lists([kw.KeywordList("ai", ["AI", "LLM"], "desc")])
        got = kw.load_keyword_lists()
        assert [k.name for k in got] == ["ai"]
        assert got[0].terms == ["AI", "LLM"]

    def test_a_missing_file_is_empty_not_an_error(self, tmp_path):
        assert kw.load_keyword_lists(tmp_path / "nope.json") == []

    def test_corrupt_json_is_empty_not_an_error(self, tmp_path):
        p = tmp_path / "k.json"
        p.write_text("{not json", encoding="utf-8")
        assert kw.load_keyword_lists(p) == []

    def test_an_invalid_entry_is_skipped_not_fatal(self, tmp_path):
        p = tmp_path / "k.json"
        p.write_text(json.dumps({"lists": [
            {"name": "BAD NAME", "terms": ["x"]},
            {"name": "ok", "terms": ["y"]},
        ]}), encoding="utf-8")
        assert [k.name for k in kw.load_keyword_lists(p)] == ["ok"]

    def test_terms_for_flattens_and_dedupes(self):
        kw.save_keyword_lists([
            kw.KeywordList("a", ["AI", "ML"]), kw.KeywordList("b", ["ML", "VC"]),
        ])
        assert kw.terms_for(["a", "b"]) == ["AI", "ML", "VC"]

    def test_an_unknown_list_name_is_ignored(self):
        kw.save_keyword_lists([kw.KeywordList("a", ["AI"])])
        assert kw.terms_for(["a", "nope"]) == ["AI"]


class TestSearchPlan:
    """The honest-degradation report — the point of the whole feature."""

    def test_search_mode_is_native(self):
        p = kw.plan_for_source({"name": "meetup", "mode": "search"}, ["AI", "queer"])
        assert p.strength == kw.NATIVE
        assert p.terms == ["AI", "queer"]

    def test_luma_maps_a_matching_keyword_to_a_category(self):
        p = kw.plan_for_source({"name": "luma", "mode": "luma"}, ["AI"])
        assert p.strength == kw.CATEGORY
        assert p.categories == ["cat-ai"]

    def test_luma_falls_back_to_local_for_an_unmappable_keyword(self):
        """Luma has 5 public categories; "queer" is not one of them."""
        p = kw.plan_for_source({"name": "luma", "mode": "luma"}, ["queer"])
        assert p.strength == kw.LOCAL
        assert "no public free-text search" in p.note

    def test_luma_says_which_terms_it_could_not_map(self):
        p = kw.plan_for_source({"name": "luma", "mode": "luma"}, ["AI", "queer"])
        assert p.strength == kw.CATEGORY
        assert "queer" in p.note

    def test_a_source_with_no_query_surface_is_local(self):
        for mode in ("partiful", "rss", "ics", "calendar", "raw"):
            assert kw.plan_for_source({"name": "x", "mode": mode}, ["AI"]).strength \
                == kw.LOCAL

    def test_no_keywords_says_so(self):
        p = kw.plan_for_source({"name": "meetup", "mode": "search"}, [])
        assert "no keywords" in p.note

    def test_the_plan_is_json_serialisable(self):
        p = kw.plan_for_source({"name": "luma", "mode": "luma"}, ["AI"])
        json.dumps(p.to_dict())


class TestCapsOverlay:
    def test_round_trip(self, tmp_path):
        p = tmp_path / "settings.json"
        save_caps_overlay({"web": 90, "total": 400}, p)
        assert load_caps_overlay(p) == {"web": 90, "total": 400}

    def test_a_huge_value_is_clamped(self, tmp_path):
        p = tmp_path / "s.json"
        assert save_caps_overlay({"total": 10 ** 9}, p) == {"total": CAP_MAX}

    def test_any_negative_normalises_to_unlimited_not_to_zero(self, tmp_path):
        """`prioritize_with_caps` tests `cap < 0`, so -5 already meant
        unlimited. Clamping it to 0 would turn an operator's typo into "drop
        every message from this source" — the worst available reading."""
        p = tmp_path / "s.json"
        assert save_caps_overlay({"web": -5}, p) == {"web": -1}

    def test_zero_still_means_drop_this_source(self, tmp_path):
        """Distinct from unlimited, and still useful."""
        p = tmp_path / "s.json"
        assert save_caps_overlay({"web": 0}, p) == {"web": 0}

    def test_unknown_keys_are_dropped(self, tmp_path):
        p = tmp_path / "s.json"
        save_caps_overlay({"web": 10, "evil": 1, "__proto__": 2}, p)
        assert set(load_caps_overlay(p)) == {"web"}

    def test_a_missing_file_is_no_overlay(self, tmp_path):
        assert load_caps_overlay(tmp_path / "nope.json") == {}

    def test_corrupt_json_is_no_overlay(self, tmp_path):
        p = tmp_path / "s.json"
        p.write_text("{oops", encoding="utf-8")
        assert load_caps_overlay(p) == {}

    def test_it_preserves_other_settings_keys(self, tmp_path):
        p = tmp_path / "s.json"
        p.write_text(json.dumps({"other": "keep me"}), encoding="utf-8")
        save_caps_overlay({"web": 5}, p)
        assert json.loads(p.read_text(encoding="utf-8"))["other"] == "keep me"

    def test_the_overlay_reaches_the_config(self, tmp_path):
        from event_harvester.config import AppConfig, apply_caps_overlay

        p = tmp_path / "s.json"
        save_caps_overlay({"web": 77}, p)
        cfg = AppConfig()
        apply_caps_overlay(cfg, p)
        assert cfg.caps.web == 77


class TestCapDefaults:
    """Unlimited everywhere except an interactive CLI run — 2026-08-11.

    Capping was losing whole sources silently: every web sub-source shares one
    `web` bucket, so luma's 60 messages took all 30 slots and **erobay fetched
    121 messages every run for weeks and produced no events**, with nothing in
    any log saying so.
    """

    def test_a_default_config_has_no_ceilings(self):
        from event_harvester.config import AppConfig

        caps = AppConfig().caps
        assert all(getattr(caps, k) == -1
                   for k in ("discord", "telegram", "gmail", "signal", "web", "total"))

    def test_the_cli_restores_the_old_ceilings(self):
        from event_harvester.config import CLI_DEFAULT_CAPS, AppConfig

        caps = AppConfig().caps
        caps.apply_cli_defaults()
        assert {k: getattr(caps, k) for k in CLI_DEFAULT_CAPS} == CLI_DEFAULT_CAPS

    def test_the_cli_does_not_overwrite_a_deliberate_value(self):
        """A profile, a CAP_* env var or an /admin overlay must still win."""
        from event_harvester.config import AppConfig

        caps = AppConfig().caps
        caps.web = 200
        caps.apply_cli_defaults()
        assert caps.web == 200
        assert caps.gmail == 30, "untouched fields still get the CLI default"

    def test_unlimited_keeps_every_message_in_a_bucket(self):
        from event_harvester.config import AppConfig
        from event_harvester.pipeline.analysis import prioritize_with_caps

        msgs = [{"platform": "web", "content": f"Event on 9/{i} at 7pm"}
                for i in range(1, 29)]
        assert len(prioritize_with_caps(msgs, AppConfig().caps)) == 28

    def test_a_zero_cap_still_drops_a_source(self):
        from event_harvester.config import AppConfig
        from event_harvester.pipeline.analysis import prioritize_with_caps

        cfg = AppConfig()
        cfg.caps.web = 0
        msgs = [{"platform": "web", "content": "x"},
                {"platform": "gmail", "content": "y"}]
        assert [m["platform"] for m in prioritize_with_caps(msgs, cfg.caps)] == ["gmail"]


class TestScoring:
    def test_a_keyword_hit_outranks_the_builtin_signals(self):
        from event_harvester.pipeline.analysis import score_message

        plain = score_message({"content": "Party on Friday at 7pm https://x.com/a"})
        tagged = score_message({"content": "Party on Friday at 7pm https://x.com/a",
                                "_keywords": ["queer"]})
        assert tagged > plain

    def test_the_bonus_survives_a_low_base_score(self):
        """A keyword hit on an otherwise dull message must still place."""
        from event_harvester.pipeline.analysis import score_message

        assert score_message({"content": "quiet night", "_keywords": ["queer"]}) >= 4


class TestSearchModeUsesKeywordLists:
    def test_a_named_list_becomes_queries(self, monkeypatch):
        from event_harvester.sources.web_modes.search import _normalize_queries

        kw.save_keyword_lists([kw.KeywordList("ai", ["AI", "LLM"])])
        got = _normalize_queries({"name": "meetup", "keywords": ["ai"]})
        assert got == ["AI", "LLM"]

    def test_inline_queries_still_work(self):
        from event_harvester.sources.web_modes.search import _normalize_queries

        assert _normalize_queries({"queries": ["a", "b"]}) == ["a", "b"]

    def test_a_list_and_inline_queries_combine_without_dupes(self):
        from event_harvester.sources.web_modes.search import _normalize_queries

        kw.save_keyword_lists([kw.KeywordList("ai", ["AI"])])
        got = _normalize_queries({"keywords": ["ai"], "queries": ["ai", "startup"]})
        assert got == ["AI", "startup"]
