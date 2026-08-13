"""Tests for the extraction A/B harness.

The harness exists to answer "could a much weaker model do this?" with a
measurement instead of an opinion. That only works if the two models get
**identical input** and are judged by the **real parser** — so those are the
two properties pinned hardest here. A harness that sampled differently per
model, or that read replies more leniently than the pipeline does, would
produce a confident number that means nothing.
"""

from __future__ import annotations

import json

import pytest

from event_harvester.training.eval_extraction import (
    FIELDS,
    ModelResult,
    agreement,
    load_sample,
    resolve_key,
    run_model,
)


def _cache(tmp_path, n=40):
    """A message cache in the shape `pipeline/harvest.py` writes."""
    messages = [{
        "content": f"Party number {i} on Friday at 9pm, tickets at "
                   f"https://lu.ma/e{i}",
        "timestamp": "2026-08-12T10:00:00+00:00",
        "platform": "telegram", "channel": "sf-parties",
        "author": f"@person{i}", "id": str(i),
    } for i in range(n)]
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"messages": messages}), encoding="utf-8")
    return path


class TestTheSampleIsIdentical:
    """A difference between models must be the model, not the draw."""

    def test_it_is_deterministic(self, tmp_path):
        path = _cache(tmp_path)
        first = load_sample(3, 10, path)
        second = load_sample(3, 10, path)
        assert [[m["id"] for m in b] for b in first] == \
               [[m["id"] for m in b] for b in second]

    def test_it_batches_exactly_like_the_pipeline(self, tmp_path):
        sample = load_sample(3, 10, _cache(tmp_path))
        assert len(sample) == 3
        assert all(len(b) == 10 for b in sample)

    def test_it_takes_the_head_of_the_scored_queue(self, tmp_path):
        """`prioritize_with_caps` sends the highest-scoring messages, so an
        eval over a random draw would measure a different distribution than
        the one production actually sends."""
        from event_harvester.pipeline.analysis import score_message

        path = _cache(tmp_path, n=60)
        sample = load_sample(2, 10, path)
        taken = [m for b in sample for m in b]
        all_msgs = json.loads(path.read_text(encoding="utf-8"))["messages"]
        cutoff = min(score_message(m) for m in taken)
        assert all(score_message(m) <= cutoff
                   for m in all_msgs if m["id"] not in {t["id"] for t in taken})

    def test_an_empty_cache_says_so_rather_than_scraping(self, tmp_path):
        path = tmp_path / "empty.json"
        path.write_text(json.dumps({"messages": []}), encoding="utf-8")
        with pytest.raises(RuntimeError, match="no usable messages"):
            load_sample(2, 10, path)


class TestItJudgesWithTheRealParser:
    def test_an_unparseable_reply_counts_as_a_failure(self, tmp_path, monkeypatch):
        """A malformed reply costs all 10 messages in the batch silently, so it
        is the failure mode that matters most for a weak model."""
        from event_harvester.training import eval_extraction as mod

        monkeypatch.setattr(mod, "_call",
                            lambda *a, **k: ("I found some events for you!", None))
        result = run_model("weak/model", load_sample(2, 10, _cache(tmp_path)))
        assert result.parse_failures == 2
        assert result.events == []
        assert result.raw_samples, "the raw reply must be kept for diagnosis"

    def test_a_good_reply_is_parsed(self, tmp_path, monkeypatch):
        from event_harvester.training import eval_extraction as mod

        ini = ("[Event.1]\ntitle = Queer Night\ndate = 2026-08-20\n"
               "time = 21:00\nlocation = The Stud\nlink = https://lu.ma/x\n")
        monkeypatch.setattr(mod, "_call", lambda *a, **k: (ini, None))
        result = run_model("good/model", load_sample(2, 10, _cache(tmp_path)))
        assert len(result.events) == 2
        assert result.parse_failures == 0

    def test_a_percent_sign_does_not_eat_the_batch(self, tmp_path, monkeypatch):
        """The 2026-08-12 regression, at the layer that would hide it: a model
        judged by a lenient parser would score better here than in production."""
        from event_harvester.training import eval_extraction as mod

        ini = ("[Event.1]\ntitle = 50% off drinks\ndate = 2026-08-20\n"
               "link = https://x.test/a%20b\n")
        monkeypatch.setattr(mod, "_call", lambda *a, **k: (ini, None))
        result = run_model("m", load_sample(1, 10, _cache(tmp_path)))
        assert len(result.events) == 1

    def test_a_call_error_is_not_a_parse_failure(self, tmp_path, monkeypatch):
        """Same distinction the pipeline learned the hard way — an outage is
        not an empty answer."""
        from event_harvester.training import eval_extraction as mod

        def boom(*a, **k):
            raise RuntimeError("429")

        monkeypatch.setattr(mod, "_call", boom)
        result = run_model("m", load_sample(3, 10, _cache(tmp_path)))
        assert result.call_errors == 3
        assert result.parse_failures == 0


class TestFillRates:
    def test_tbd_does_not_count_as_filled(self):
        """An event with `date = TBD` never reaches the calendar page, so it
        must not score as a complete extraction."""
        r = ModelResult(model="m", events=[
            {"title": "a", "date": "2026-08-20", "time": "TBD",
             "location": "", "link": "https://x.test"},
        ])
        rates = r.fill_rates
        assert rates["date"] == 1.0
        assert rates["time"] == 0.0
        assert rates["location"] == 0.0
        assert rates["link"] == 1.0

    def test_every_reported_field_is_one_the_page_shows(self):
        assert set(FIELDS) == {"date", "time", "location", "link"}

    def test_no_events_is_zero_not_a_crash(self):
        assert ModelResult(model="m").fill_rates == {f: 0.0 for f in FIELDS}


class TestAgreement:
    def test_recall_is_measured_with_the_pipeline_matcher(self):
        """"The same event" must mean here what it means to the deduper."""
        base = [{"title": "Queer Night", "date": "2026-08-20"},
                {"title": "AI Meetup", "date": "2026-08-21"}]
        cand = [{"title": "Queer Night", "date": "2026-08-20"}]
        out = agreement(base, cand)
        assert out["matched"] == 1
        assert out["recall"] == 0.5
        assert "AI Meetup" in out["missed_titles"]

    def test_candidate_only_events_are_surfaced_not_scored(self):
        """They are either finds or inventions, and the harness cannot tell —
        so it counts them and says so rather than crediting them."""
        base = [{"title": "Queer Night", "date": "2026-08-20"}]
        cand = [{"title": "Queer Night", "date": "2026-08-20"},
                {"title": "Invented Gala", "date": "2026-08-22"}]
        out = agreement(base, cand)
        assert out["candidate_only"] == 1
        assert out["recall"] == 1.0

    def test_an_empty_baseline_gives_no_recall_rather_than_a_divide_by_zero(self):
        assert agreement([], [{"title": "x"}])["recall"] is None


class TestKeys:
    def test_the_flag_wins(self, monkeypatch):
        monkeypatch.setenv("EH_EVAL_API_KEY", "from-env")
        assert resolve_key("from-flag") == "from-flag"

    def test_the_environment_is_the_preferred_route(self, monkeypatch):
        monkeypatch.setenv("EH_EVAL_API_KEY", "from-env")
        assert resolve_key(None) == "from-env"

    def test_absent_is_none_not_empty_string(self, monkeypatch):
        monkeypatch.delenv("EH_EVAL_API_KEY", raising=False)
        assert resolve_key(None) is None


class TestCommandWiring:
    def test_the_command_is_registered(self):
        from event_harvester.cli.dispatch import _HANDLERS, KNOWN_COMMANDS

        assert "eval-extraction" in KNOWN_COMMANDS
        assert "eval-extraction" in _HANDLERS

    def test_it_parses(self):
        from event_harvester.cli.parser import build_parser

        args = build_parser().parse_args([
            "eval-extraction", "--baseline", "a/b", "--candidate", "c/d",
            "--batches", "5", "--api-base", "https://h:4000/v1",
        ])
        assert args.baseline == "a/b" and args.batches == 5

    def test_an_endpoint_without_a_key_is_refused_before_spending(self, monkeypatch):
        """A 401 mid-run reads like a broken eval rather than a missing flag."""
        import asyncio

        from event_harvester.cli.commands.eval_extraction import eval_extraction_cmd
        from event_harvester.cli.parser import build_parser

        monkeypatch.delenv("EH_EVAL_API_KEY", raising=False)
        args = build_parser().parse_args([
            "eval-extraction", "--baseline", "a/b", "--candidate", "c/d",
            "--api-base", "https://h:4000/v1",
        ])

        class Cfg:
            days_back = 7

            class llm:
                api_key = "a-provider-key"

        assert asyncio.run(eval_extraction_cmd(args, Cfg)) == 2
