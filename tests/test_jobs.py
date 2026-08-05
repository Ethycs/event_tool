"""Tests for the job queue.

The queue is shared by two OS processes, so the properties that matter are the
concurrency ones: a claim is a mutex, a publish is atomic, and a restart never
silently re-runs work that spends money. Those are tested directly rather than
inferred from the happy path.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from event_harvester import jobs


@pytest.fixture
def store(tmp_path):
    """Every test gets its own queue directory."""
    return tmp_path / "jobs"


def _req(**over):
    base = {"tags": {"any": ["venues"]}, "platforms": {"web": True}, "dry_run": False}
    base.update(over)
    return base


class TestSubmit:
    def test_a_submitted_job_is_queued(self, store):
        job = jobs.submit(_req(), submitted_by="site", jobs_dir=store)
        assert job.state == jobs.STATE_QUEUED
        assert jobs.load(job.id, store).id == job.id

    def test_ids_sort_chronologically(self, store):
        early = jobs.new_job_id(datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc))
        late = jobs.new_job_id(datetime(2026, 7, 31, 11, 0, tzinfo=timezone.utc))
        assert early < late

    def test_the_timer_gets_lower_priority_than_a_person(self, store):
        timer = jobs.submit(_req(a=1), submitted_by="timer", jobs_dir=store)
        human = jobs.submit(_req(a=2), submitted_by="site", jobs_dir=store)
        assert timer.priority < human.priority

    def test_a_full_queue_is_refused_not_accepted(self, store):
        for i in range(jobs.MAX_QUEUE_DEPTH):
            jobs.submit(_req(n=i), submitted_by="cli", jobs_dir=store)
        with pytest.raises(jobs.QueueFull, match="not draining"):
            jobs.submit(_req(n="over"), submitted_by="cli", jobs_dir=store)


class TestCoalescing:
    """Without this, one wedged job turns a nightly timer into a week of jobs."""

    def test_an_identical_queued_request_is_joined_not_duplicated(self, store):
        first = jobs.submit(_req(), submitted_by="timer", jobs_dir=store)
        second = jobs.submit(_req(), submitted_by="timer", jobs_dir=store)
        assert second.id == first.id
        assert len(jobs.list_jobs(bucket=jobs.QUEUED, jobs_dir=store)) == 1

    def test_the_join_is_counted_not_silently_dropped(self, store):
        jobs.submit(_req(), submitted_by="timer", jobs_dir=store)
        jobs.submit(_req(), submitted_by="timer", jobs_dir=store)
        job = jobs.submit(_req(), submitted_by="timer", jobs_dir=store)
        assert job.coalesced == 2

    def test_coalescing_does_not_delay_the_job(self, store):
        """Bumping submitted_at would push it later in FIFO every duplicate."""
        first = jobs.submit(_req(), submitted_by="timer", jobs_dir=store)
        original = first.submitted_at
        again = jobs.submit(_req(), submitted_by="timer", jobs_dir=store)
        assert again.submitted_at == original

    def test_an_interactive_duplicate_raises_the_priority(self, store):
        timer = jobs.submit(_req(), submitted_by="timer", jobs_dir=store)
        assert timer.priority == jobs.PRIORITY_SCHEDULED
        joined = jobs.submit(_req(), submitted_by="site", jobs_dir=store)
        assert joined.id == timer.id
        assert joined.priority == jobs.PRIORITY_INTERACTIVE

    def test_a_different_request_is_a_different_job(self, store):
        a = jobs.submit(_req(tags={"any": ["venues"]}), jobs_dir=store)
        b = jobs.submit(_req(tags={"any": ["tech"]}), jobs_dir=store)
        assert a.id != b.id

    def test_key_order_does_not_change_the_fingerprint(self, store):
        one = jobs.request_fingerprint({"a": 1, "b": 2})
        two = jobs.request_fingerprint({"b": 2, "a": 1})
        assert one == two, "dict ordering must not defeat coalescing"

    def test_a_running_job_is_never_coalesced_against(self, store):
        """Its inputs were read at start; a new request wants fresher data."""
        jobs.submit(_req(), submitted_by="timer", jobs_dir=store)
        jobs.claim_next(jobs_dir=store)
        again = jobs.submit(_req(), submitted_by="timer", jobs_dir=store)
        assert again.state == jobs.STATE_QUEUED
        assert len(jobs.list_jobs(bucket=jobs.QUEUED, jobs_dir=store)) == 1


class TestClaimIsAMutex:
    def test_claiming_moves_the_job_to_running(self, store):
        job = jobs.submit(_req(), jobs_dir=store)
        claimed = jobs.claim_next(jobs_dir=store)
        assert claimed.id == job.id
        assert claimed.state == jobs.STATE_RUNNING
        assert claimed.started_at is not None
        assert not (store / jobs.QUEUED / f"{job.id}.json").exists()
        assert (store / jobs.RUNNING / f"{job.id}.json").exists()

    def test_a_second_claim_cannot_take_the_same_job(self, store):
        """The whole point: os.rename is atomic and fails if it already moved."""
        jobs.submit(_req(), jobs_dir=store)
        first = jobs.claim_next(jobs_dir=store)
        second = jobs.claim_next(jobs_dir=store)
        assert first is not None
        assert second is None, "two runners both claimed one job"

    def test_an_empty_queue_claims_nothing(self, store):
        assert jobs.claim_next(jobs_dir=store) is None

    def test_priority_wins_then_fifo(self, store):
        low1 = jobs.submit(_req(n=1), submitted_by="timer", jobs_dir=store)
        low2 = jobs.submit(_req(n=2), submitted_by="timer", jobs_dir=store)
        high = jobs.submit(_req(n=3), submitted_by="site", jobs_dir=store)
        assert jobs.claim_next(jobs_dir=store).id == high.id
        assert jobs.claim_next(jobs_dir=store).id == low1.id
        assert jobs.claim_next(jobs_dir=store).id == low2.id


class TestCancelHasTwoMeanings:
    def test_a_queued_job_is_cancelled_instantly(self, store):
        job = jobs.submit(_req(), jobs_dir=store)
        out = jobs.request_cancel(job.id, jobs_dir=store)
        assert out.state == jobs.STATE_CANCELLED
        assert jobs.claim_next(jobs_dir=store) is None, "a cancelled job must not run"

    def test_a_running_job_is_only_asked_to_stop(self, store):
        jobs.submit(_req(), jobs_dir=store)
        running = jobs.claim_next(jobs_dir=store)
        out = jobs.request_cancel(running.id, jobs_dir=store)
        assert out.state == jobs.STATE_RUNNING, "cancel is cooperative, not instant"
        assert jobs.cancel_requested(running.id, jobs_dir=store) is True

    def test_finishing_clears_the_cancel_marker(self, store):
        jobs.submit(_req(), jobs_dir=store)
        running = jobs.claim_next(jobs_dir=store)
        jobs.request_cancel(running.id, jobs_dir=store)
        jobs.finish(running, jobs.STATE_CANCELLED, jobs_dir=store)
        assert jobs.cancel_requested(running.id, jobs_dir=store) is False

    def test_cancelling_an_unknown_job_is_not_an_error(self, store):
        assert jobs.request_cancel("j_nope", jobs_dir=store) is None


class TestFinish:
    def test_a_finished_job_leaves_running(self, store):
        jobs.submit(_req(), jobs_dir=store)
        job = jobs.claim_next(jobs_dir=store)
        jobs.finish(job, jobs.STATE_DONE, result={"events": 12}, jobs_dir=store)
        assert not (store / jobs.RUNNING / f"{job.id}.json").exists()
        loaded = jobs.load(job.id, store)
        assert loaded.state == jobs.STATE_DONE
        assert loaded.result["events"] == 12
        assert loaded.finished_at is not None

    def test_a_non_terminal_state_is_refused(self, store):
        jobs.submit(_req(), jobs_dir=store)
        job = jobs.claim_next(jobs_dir=store)
        with pytest.raises(jobs.JobError, match="not a terminal state"):
            jobs.finish(job, jobs.STATE_RUNNING, jobs_dir=store)


class TestRestartSafety:
    def test_a_job_left_running_becomes_interrupted(self, store):
        jobs.submit(_req(), jobs_dir=store)
        job = jobs.claim_next(jobs_dir=store)
        reaped = jobs.reap_interrupted(jobs_dir=store)
        assert [j.id for j in reaped] == [job.id]
        assert jobs.load(job.id, store).state == jobs.STATE_INTERRUPTED

    def test_an_interrupted_job_is_never_requeued(self, store):
        """Re-running silently re-opens browsers and re-spends tokens."""
        jobs.submit(_req(), jobs_dir=store)
        jobs.claim_next(jobs_dir=store)
        jobs.reap_interrupted(jobs_dir=store)
        assert jobs.claim_next(jobs_dir=store) is None

    def test_reaping_an_empty_queue_does_nothing(self, store):
        assert jobs.reap_interrupted(jobs_dir=store) == []


class TestDurability:
    def test_a_published_job_is_never_half_written(self, store):
        """Two processes read this directory; a torn read must be impossible."""
        job = jobs.submit(_req(), jobs_dir=store)
        path = store / jobs.QUEUED / f"{job.id}.json"
        assert json.loads(path.read_text(encoding="utf-8"))["id"] == job.id
        assert not list(store.glob("**/*.tmp")), "a temp file was left behind"

    def test_an_unreadable_job_file_is_skipped_not_fatal(self, store):
        jobs.submit(_req(), jobs_dir=store)
        (store / jobs.QUEUED / "j_20260731T000000_bad.json").write_text(
            "{not json", encoding="utf-8",
        )
        assert len(jobs.list_jobs(bucket=jobs.QUEUED, jobs_dir=store)) == 1


class TestPruning:
    def test_history_is_bounded_by_count(self, store):
        for i in range(12):
            jobs.submit(_req(n=i), jobs_dir=store)
            claimed = jobs.claim_next(jobs_dir=store)
            jobs.finish(claimed, jobs.STATE_DONE, jobs_dir=store)
        removed = jobs.prune(max_jobs=5, jobs_dir=store)
        assert removed == 7
        assert len(jobs.list_jobs(bucket=jobs.FINISHED, jobs_dir=store)) == 5

    def test_history_is_bounded_by_age(self, store):
        now = datetime(2026, 7, 31, tzinfo=timezone.utc)
        jobs.submit(_req(), now=now - timedelta(days=90), jobs_dir=store)
        claimed = jobs.claim_next(jobs_dir=store)
        jobs.finish(claimed, jobs.STATE_DONE, now=now - timedelta(days=90),
                    jobs_dir=store)
        assert jobs.prune(max_age_days=30, now=now, jobs_dir=store) == 1

    def test_pruning_keeps_the_newest(self, store):
        kept = None
        for i in range(6):
            jobs.submit(_req(n=i), jobs_dir=store)
            claimed = jobs.claim_next(jobs_dir=store)
            kept = jobs.finish(claimed, jobs.STATE_DONE, jobs_dir=store)
        jobs.prune(max_jobs=1, jobs_dir=store)
        remaining = jobs.list_jobs(bucket=jobs.FINISHED, jobs_dir=store)
        assert [j.id for j in remaining] == [kept.id]


class TestSummary:
    def test_the_summary_names_what_is_running_and_what_is_next(self, store):
        jobs.submit(_req(n=1), submitted_by="site", jobs_dir=store)
        running = jobs.claim_next(jobs_dir=store)
        running.progress = {"stage": "extract"}
        jobs.save(running, store)
        jobs.submit(_req(n=2), jobs_dir=store)

        summary = jobs.queue_summary(jobs_dir=store)
        assert summary["running"] == running.id
        assert summary["running_stage"] == "extract"
        assert summary["queued"] == 1
        assert len(summary["next"]) == 1


class TestLogBound:
    def test_the_log_tail_does_not_grow_without_limit(self):
        job = jobs.Job(id="j_x")
        for i in range(jobs.MAX_LOG_LINES + 50):
            job.append_log(f"line {i}")
        assert len(job.log_tail) == jobs.MAX_LOG_LINES
        assert job.log_tail[-1] == f"line {jobs.MAX_LOG_LINES + 49}"


class TestIdOrderingIsFifoOrdering:
    """Queue order *is* id order, so the id must carry sub-second precision.

    With second-resolution stamps, every job submitted inside the same second
    ordered by its random suffix — which is exactly the case that matters:
    two clicks of Run, or the site and MCP backend submitting together.
    """

    def test_ids_minted_in_the_same_second_still_order(self):
        base = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)
        ids = [
            jobs.new_job_id(base.replace(microsecond=us))
            for us in (1, 500, 900_000)
        ]
        assert ids == sorted(ids)

    def test_rapid_submissions_keep_their_order(self, store):
        submitted = [
            jobs.submit(_req(n=i), submitted_by="cli", jobs_dir=store).id
            for i in range(10)
        ]
        claimed = []
        while (job := jobs.claim_next(jobs_dir=store)) is not None:
            claimed.append(job.id)
            jobs.finish(job, jobs.STATE_DONE, jobs_dir=store)
        assert claimed == submitted, "FIFO broke for same-second submissions"
