"""The funnel has to add up, and the check has to be able to fail.

`filtering` was split out of `analysis.extract_events_llm` on 2026-08-14 for two
reasons — the free stages were unreachable without paying for the LLM, and the
reject counts had stopped summing to the input without anything noticing.

Measured on job `…dcf5` (2026-08-13): 19,646 messages in, 775 reached the model,
244 events out, `llm_no_events` reported **0**. Roughly 530 messages attributed
to nothing. The run before it filed 814 unreachable messages as "the model read
these and found nothing", reported `ok` with 0 events, and overwrote a cache
holding 251.

So the tests that matter here are the ones that prove the check can *fail*. A
balance assertion that cannot be made to raise is the same class of object as
the counter that was never incremented: it reads as a measurement and is
structurally fixed. `TestTheCheckCanFail` is the point of this file.
"""

from __future__ import annotations

import logging

import pytest

from event_harvester.pipeline.filtering import (
    DROP_KINDS,
    FILTER_BUCKETS,
    REJECT_BUCKETS,
    FilterResult,
    FunnelImbalance,
    StageCount,
    _strict_default,
    empty_rejects,
    filter_for_extraction,
)


def _msgs(n: int) -> list[dict]:
    """Distinct dicts — the reject lists are identity-based, so shared objects
    would make two different messages indistinguishable to the arithmetic."""
    return [{"content": f"event {i} on friday at 8pm", "platform": "discord",
             "channel": "c", "timestamp": "2026-08-14T00:00:00Z"} for i in range(n)]


def _result(n_in: int, n_out: int, dropped: dict[str, int]) -> FilterResult:
    """A FilterResult with hand-set numbers, to exercise the arithmetic."""
    rejects = empty_rejects()
    for bucket, count in dropped.items():
        rejects[bucket] = _msgs(count)
    return FilterResult(
        messages=_msgs(n_out), rejects=rejects, n_in=n_in,
        stages=(StageCount("classifier", n_in, n_out, dict(dropped)),),
    )


class TestTheCheckCanFail:
    """If none of these can be made to raise, the check is decoration."""

    def test_an_unattributed_drop_raises_under_strict(self):
        """The shape of the live bug: items went in, fewer came out, and the
        difference is filed nowhere."""
        r = _result(n_in=5, n_out=1, dropped={})
        with pytest.raises(FunnelImbalance) as e:
            r.check(strict=True)
        assert "4 message(s) unaccounted for" in str(e.value)

    def test_the_batch_of_five_case_specifically(self):
        """5 messages to the model, 1 event, other 4 filed nowhere — this is
        incident #2 reduced to arithmetic."""
        stage = StageCount("extract", 5, 1, {})
        assert stage.unattributed == 4
        with pytest.raises(FunnelImbalance):
            stage.check(strict=True)

    def test_partial_attribution_still_raises(self):
        """Attributing *some* of the drop is not attributing the drop."""
        with pytest.raises(FunnelImbalance):
            _result(n_in=10, n_out=2, dropped={"classifier": 3}).check(strict=True)

    def test_over_attribution_raises_too(self):
        """A bucket claiming more than went missing is equally a bug, and the
        signed arithmetic catches it without a special case."""
        with pytest.raises(FunnelImbalance):
            _result(n_in=10, n_out=8, dropped={"classifier": 5}).check(strict=True)

    def test_a_balanced_funnel_is_silent(self):
        assert _result(n_in=10, n_out=4, dropped={"classifier": 6}).check(strict=True) == []


class TestItRecordsRatherThanRaisesInProduction:
    """Raising mid-harvest turns an accounting bug into a lost run, which is
    worse than the miscount. A 24-minute harvest is the unit at risk."""

    def test_non_strict_returns_the_problem_instead_of_raising(self):
        problems = _result(n_in=5, n_out=1, dropped={}).check(strict=False)
        assert len(problems) == 2          # the stage, then the section
        assert all("unaccounted for" in p for p in problems)

    def test_non_strict_logs_at_error(self, caplog):
        """It must be loud. The 429 storm was visible only as a stack trace per
        batch in a log nobody reads during a run."""
        with caplog.at_level(logging.ERROR, logger="event_harvester.pipeline.filtering"):
            _result(n_in=5, n_out=1, dropped={}).check(strict=False)
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    def test_strict_is_on_under_pytest_by_default(self):
        """Which is why the tests above did not have to ask for it."""
        assert _strict_default() is True

    def test_the_env_var_forces_it_off(self, monkeypatch):
        monkeypatch.setenv("EH_STRICT_FUNNEL", "0")
        assert _strict_default() is False

    def test_the_env_var_forces_it_on(self, monkeypatch):
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.setenv("EH_STRICT_FUNNEL", "1")
        assert _strict_default() is True

    def test_an_empty_env_var_means_unset(self, monkeypatch):
        """`EH_STRICT_FUNNEL=` should not read as "off" — same rule as
        `LLM_VERIFY_SSL`."""
        monkeypatch.setenv("EH_STRICT_FUNNEL", "   ")
        assert _strict_default() is True


class TestTheRealFunnelBalances:
    def test_a_live_filter_run_accounts_for_every_message(self):
        """The end-to-end property, on the real classifier and reranker."""
        result = filter_for_extraction(_msgs(40), caps=None)
        assert result.n_in == 40
        assert result.n_in == result.n_out + sum(result.dropped.values())
        assert result.check(strict=True) == []

    def test_every_stage_reports_both_sides_of_its_boundary(self):
        result = filter_for_extraction(_msgs(20), caps=None)
        assert result.stages, "a run with no stages accounts for nothing"
        for stage in result.stages:
            assert stage.n_in >= stage.n_out
            assert stage.unattributed == 0, stage

    def test_the_stages_chain_end_to_end(self):
        """Each stage's output is the next one's input. A gap between two
        stages is a drop nobody owns."""
        stages = filter_for_extraction(_msgs(20), caps=None).stages
        for earlier, later in zip(stages, stages[1:]):
            assert earlier.n_out == later.n_in

    def test_empty_input_is_not_an_error(self):
        result = filter_for_extraction([], caps=None)
        assert result.n_in == 0 and result.n_out == 0
        assert result.check(strict=True) == []

    def test_nothing_is_dropped_when_caps_are_unlimited(self):
        """`caps: 0` on the service path is a true zero — the timer path leaves
        `caps.total` at the -1 sentinel and nothing is trimmed. Pinned because
        the number is indistinguishable from a policy that ran and kept
        everything; see §7 of the design doc."""
        result = filter_for_extraction(_msgs(30), caps=None)
        assert result.dropped["caps"] == 0


class TestTheDropVocabulary:
    def test_the_filter_buckets_are_a_subset_of_all_buckets(self):
        assert set(FILTER_BUCKETS) < set(REJECT_BUCKETS)

    def test_every_bucket_declares_a_kind(self):
        """A reason with no kind cannot answer "did anything look at this?",
        which is the question that decides whether a cache may be overwritten."""
        assert set(DROP_KINDS) == set(REJECT_BUCKETS)

    def test_only_the_outage_bucket_is_unreached(self):
        """`llm_errors` means the model never saw them. Every other bucket
        means something examined the message and decided. Collapsing the two
        cost 251 events on 2026-08-12."""
        unreached = {b for b, kind in DROP_KINDS.items() if kind == "unreached"}
        assert unreached == {"llm_errors"}

    def test_kinds_are_drawn_from_a_closed_set(self):
        assert set(DROP_KINDS.values()) == {"policy", "unreached"}


class TestTheSplitKeptItsCallers:
    """`analysis` re-exports the vocabulary because four modules and a training
    script import it from there. The split is not a reason to move them."""

    @pytest.mark.parametrize("name", [
        "REJECT_BUCKETS", "empty_rejects", "filter_for_extraction",
        "FilterResult", "StageCount", "FunnelImbalance", "DROP_KINDS",
    ])
    def test_it_is_still_importable_from_analysis(self, name):
        from event_harvester.pipeline import analysis
        assert hasattr(analysis, name)

    def test_the_orchestrator_still_gets_its_bucket_list(self):
        from event_harvester.pipeline.orchestrator import _empty_rejects
        assert set(_empty_rejects()) == set(REJECT_BUCKETS)

    def test_the_scoring_helpers_did_not_move(self):
        """`training/eval_classifier.py` imports `prioritize` from `analysis`."""
        from event_harvester.pipeline.analysis import (
            prioritize,
            prioritize_with_caps,
            score_message,
        )
        assert all(callable(f) for f in (prioritize, prioritize_with_caps, score_message))


class TestItRunsWithoutAnLLM:
    """The reason the split exists: source onboarding wants "does this page
    have events?" answered by the free stages. See
    `10 - Blueprints/01 - Adding a Web Source.md`."""

    def test_no_llm_config_is_needed(self):
        """The signature takes no LLMConfig at all — the strongest form of
        this guarantee is that there is nowhere to pass one."""
        import inspect
        params = inspect.signature(filter_for_extraction).parameters
        assert "cfg" not in params and "llm" not in params

    def test_it_makes_no_completion_call(self, monkeypatch):
        def _boom(*a, **k):  # pragma: no cover - the point is that it is not hit
            raise AssertionError("filtering called the LLM")

        monkeypatch.setattr("event_harvester.llm.chat_completion", _boom)
        result = filter_for_extraction(_msgs(12), caps=None)
        assert result.n_in == 12
