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


@pytest.fixture(autouse=True)
def no_ambient_keys(monkeypatch):
    """Clear **every** key `resolve_key` consults, for two reasons.

    A test that finds a real key stops testing the no-key path — and worse,
    `test_absent_is_none` then failed with the live proxy key embedded in the
    assertion message, printing a working credential into the test log. The
    same fallback also let `test_an_endpoint_without_a_key_is_refused` sail
    past the guard and make **real API calls** from a unit test.

    Autouse and unconditional: a credential must never be reachable from here
    by accident.
    """
    from event_harvester.training.eval_extraction import KEY_VARS

    for name in KEY_VARS:
        monkeypatch.delenv(name, raising=False)


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

    def test_it_spans_the_pool_rather_than_taking_its_head(self, tmp_path):
        """The head of the queue is the easy end — structured listings that
        already carry a date, a time and a link. A 3-batch run over it scored
        100% on every field for both models, which answers a question nobody
        asked. The sample is spaced across the whole pool instead."""
        path = _cache(tmp_path, n=100)
        sample = load_sample(2, 10, path, pool=100)
        taken = [int(m["id"]) for b in sample for m in b]
        assert len(taken) == 20
        assert len(set(taken)) == 20, "the stride must not repeat a message"
        # Spread, not clustered at the front: with a pool of 100 and 20 wanted,
        # the last pick comes from deep in the pool.
        assert max(taken) - min(taken) > 50

    def test_a_small_pool_falls_back_to_taking_everything(self, tmp_path):
        sample = load_sample(2, 10, _cache(tmp_path, n=20), pool=20)
        assert sum(len(b) for b in sample) == 20

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
    """Assertions here compare booleans, never the value.

    A failing `assert resolve_key(None) is None` puts whatever it found into
    the failure message — which is how a live proxy key ended up in a test log.
    """

    def test_the_flag_wins(self, monkeypatch):
        monkeypatch.setenv("EH_EVAL_API_KEY", "from-env")
        assert resolve_key("from-flag") == "from-flag"

    def test_the_environment_is_the_preferred_route(self, monkeypatch):
        monkeypatch.setenv("EH_EVAL_API_KEY", "from-env")
        assert resolve_key(None) == "from-env"

    def test_the_proxy_key_is_a_recognised_fallback(self, monkeypatch):
        """`LLM_ZT_KEY` lives in .env for the mesh proxy, so the CLI finds it
        without the operator re-exporting it."""
        monkeypatch.setenv("LLM_ZT_KEY", "from-dotenv")
        assert resolve_key(None) == "from-dotenv"

    def test_the_earlier_variable_wins(self, monkeypatch):
        monkeypatch.setenv("EH_EVAL_API_KEY", "explicit")
        monkeypatch.setenv("LLM_ZT_KEY", "fallback")
        assert resolve_key(None) == "explicit"

    def test_absent_is_none_not_empty_string(self):
        assert (resolve_key(None) is None) is True


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
        """A 401 mid-run reads like a broken eval rather than a missing flag.

        The `no_ambient_keys` fixture is load-bearing here: before it existed
        this test found the real proxy key, skipped the guard, and made live
        API calls.
        """
        import asyncio

        from event_harvester.cli.commands.eval_extraction import eval_extraction_cmd
        from event_harvester.cli.parser import build_parser

        # Belt and braces — a network call from a unit test is never acceptable.
        monkeypatch.setattr(
            "event_harvester.training.eval_extraction._call",
            lambda *a, **k: pytest.fail("the guard let a real call through"),
        )
        args = build_parser().parse_args([
            "eval-extraction", "--baseline", "a/b", "--candidate", "c/d",
            "--api-base", "https://h:4000/v1",
        ])

        class Cfg:
            days_back = 7

            class llm:
                api_key = "a-provider-key"

        assert asyncio.run(eval_extraction_cmd(args, Cfg)) == 2
