"""Tests for the serial job runner.

The executor is injected, so none of this opens a browser or calls an LLM.
What is pinned here is the behaviour that costs money when it is wrong: one
job at a time, a failure never kills the runner, and nothing is ever run twice.
"""

from __future__ import annotations

import asyncio

import pytest

from event_harvester import job_runner, jobs
from event_harvester.job_runner import JobRunner, ProgressSink


@pytest.fixture
def store(tmp_path):
    return tmp_path / "jobs"


def _submit(store, **over):
    return jobs.submit({"n": 1, **over}, submitted_by="cli", jobs_dir=store)


class TestOneAtATime:
    async def test_a_job_runs_and_finishes(self, store):
        async def ok(job, sink):
            return {"events": 7}

        _submit(store)
        runner = JobRunner(ok, jobs_dir=store)
        assert await runner.run_once() is True

        done = jobs.list_jobs(bucket=jobs.FINISHED, jobs_dir=store)[0]
        assert done.state == jobs.STATE_DONE
        assert done.result["events"] == 7

    async def test_an_empty_queue_is_not_an_error(self, store):
        async def never(job, sink):
            raise AssertionError("ran with an empty queue")

        assert await JobRunner(never, jobs_dir=store).run_once() is False

    async def test_two_jobs_do_not_overlap(self, store):
        """Serial is the whole point — three singleton resources depend on it."""
        concurrent = 0
        peak = 0

        async def slow(job, sink):
            nonlocal concurrent, peak
            concurrent += 1
            peak = max(peak, concurrent)
            await asyncio.sleep(0.05)
            concurrent -= 1
            return {}

        _submit(store, n=1)
        _submit(store, n=2)
        runner = JobRunner(slow, jobs_dir=store)
        await runner.run_once()
        await runner.run_once()
        assert peak == 1
        assert runner.jobs_run == 2

    async def test_jobs_run_in_queue_order(self, store):
        seen = []

        async def note(job, sink):
            seen.append(job.request["n"])
            return {}

        # Scheduled work first, so the interactive job has something to jump.
        jobs.submit({"n": 1}, submitted_by="timer", jobs_dir=store)
        jobs.submit({"n": 2}, submitted_by="timer", jobs_dir=store)
        high = jobs.submit({"n": 3}, submitted_by="site", jobs_dir=store)
        assert high.priority == jobs.PRIORITY_INTERACTIVE

        runner = JobRunner(note, jobs_dir=store)
        for _ in range(3):
            await runner.run_once()
        assert seen == [3, 1, 2], "interactive first, then FIFO"


class TestFailureIsContained:
    async def test_a_raising_executor_fails_the_job_not_the_runner(self, store):
        async def boom(job, sink):
            raise ValueError("cloudflare said no")

        _submit(store)
        runner = JobRunner(boom, jobs_dir=store)
        await runner.run_once()  # must not raise

        done = jobs.list_jobs(bucket=jobs.FINISHED, jobs_dir=store)[0]
        assert done.state == jobs.STATE_FAILED
        assert "cloudflare said no" in done.result["error"]

    async def test_a_failure_leaves_running_empty(self, store):
        """A job stuck in running/ would be reaped as interrupted for ever."""
        async def boom(job, sink):
            raise RuntimeError("x")

        _submit(store)
        await JobRunner(boom, jobs_dir=store).run_once()
        assert jobs.list_jobs(bucket=jobs.RUNNING, jobs_dir=store) == []

    async def test_a_job_that_overruns_is_failed_not_left_hanging(self, store):
        async def forever(job, sink):
            await asyncio.sleep(10)

        _submit(store)
        runner = JobRunner(forever, jobs_dir=store, timeout_seconds=0.05)
        await runner.run_once()
        done = jobs.list_jobs(bucket=jobs.FINISHED, jobs_dir=store)[0]
        assert done.state == jobs.STATE_FAILED
        assert "wall clock" in done.result["error"]

    async def test_repeated_failure_stops_scheduled_work(self, store):
        """A dead credential must not produce a failed job every night for ever."""
        async def boom(job, sink):
            raise RuntimeError("bad token")

        runner = JobRunner(boom, jobs_dir=store)
        for i in range(job_runner.MAX_CONSECUTIVE_FAILURES):
            jobs.submit({"n": i}, submitted_by="timer", jobs_dir=store)
            await runner.run_once()
        assert runner.consecutive_failures == job_runner.MAX_CONSECUTIVE_FAILURES

        jobs.submit({"n": "another"}, submitted_by="timer", jobs_dir=store)
        assert await runner.run_once() is False, "kept spinning on a dead credential"

    async def test_a_person_can_still_run_after_repeated_failures(self, store):
        """Interactive work proceeds — someone is there to see it fail."""
        async def boom(job, sink):
            raise RuntimeError("bad token")

        runner = JobRunner(boom, jobs_dir=store)
        runner.consecutive_failures = job_runner.MAX_CONSECUTIVE_FAILURES
        jobs.submit({"n": "manual"}, submitted_by="site", jobs_dir=store)
        assert await runner.run_once() is True

    async def test_success_clears_the_failure_streak(self, store):
        async def ok(job, sink):
            return {}

        runner = JobRunner(ok, jobs_dir=store)
        runner.consecutive_failures = 2
        _submit(store)
        await runner.run_once()
        assert runner.consecutive_failures == 0


class TestCancellation:
    async def test_cancelling_before_the_claim_means_it_never_runs(self, store):
        ran = []

        async def note(job, sink):
            ran.append(job.id)
            return {}

        job = _submit(store)
        jobs.request_cancel(job.id, jobs_dir=store)
        await JobRunner(note, jobs_dir=store).run_once()
        assert ran == []

    async def test_an_executor_can_observe_the_cancel_flag(self, store):
        async def cooperative(job, sink):
            jobs.request_cancel(job.id, jobs_dir=store)   # someone hits Stop
            sink.raise_if_cancelled()
            raise AssertionError("should not get here")

        _submit(store)
        await JobRunner(cooperative, jobs_dir=store).run_once()
        done = jobs.list_jobs(bucket=jobs.FINISHED, jobs_dir=store)[0]
        assert done.state == jobs.STATE_CANCELLED

    async def test_raise_if_cancelled_is_quiet_when_it_is_not(self, store):
        _submit(store)
        job = jobs.claim_next(jobs_dir=store)
        ProgressSink(job, jobs_dir=store).raise_if_cancelled()   # must not raise


class TestProgress:
    async def test_stage_changes_are_visible_while_running(self, store):
        seen = {}

        async def staged(job, sink):
            sink.stage("harvest")
            seen["mid"] = jobs.load(job.id, store).progress.get("stage")
            sink.stage("extract")
            return {}

        _submit(store)
        await JobRunner(staged, jobs_dir=store).run_once()
        assert seen["mid"] == "harvest", "progress must be readable mid-run"

    async def test_log_writes_are_debounced(self, store):
        """A harvest logs per source; a disk write per line is pure waste."""
        writes = []
        original = jobs.save

        def counting_save(job, jobs_dir=None):
            writes.append(job.id)
            return original(job, jobs_dir)

        async def chatty(job, sink):
            for i in range(200):
                sink.log(f"line {i}")
            return {}

        _submit(store)
        import unittest.mock

        with unittest.mock.patch.object(jobs, "save", counting_save):
            await JobRunner(chatty, jobs_dir=store).run_once()
        assert len(writes) < 20, f"{len(writes)} writes for 200 log lines"


class TestStartup:
    async def test_the_runner_reaps_before_it_drains(self, store):
        """A job left running by a crash is interrupted, never re-run."""
        _submit(store)
        stranded = jobs.claim_next(jobs_dir=store)

        ran = []

        async def note(job, sink):
            ran.append(job.id)
            return {}

        runner = JobRunner(note, jobs_dir=store, poll_seconds=0.01)
        task = asyncio.create_task(runner.run_forever())
        await asyncio.sleep(0.05)
        runner.stop()
        await task

        assert jobs.load(stranded.id, store).state == jobs.STATE_INTERRUPTED
        assert ran == [], "an interrupted job was re-run"

    async def test_run_forever_stops_when_asked(self, store):
        async def ok(job, sink):
            return {}

        runner = JobRunner(ok, jobs_dir=store, poll_seconds=0.01)
        task = asyncio.create_task(runner.run_forever())
        await asyncio.sleep(0.03)
        runner.stop()
        await asyncio.wait_for(task, timeout=2)


class TestWaitFor:
    async def test_it_returns_once_the_job_is_terminal(self, store):
        job = _submit(store)

        async def finisher():
            await asyncio.sleep(0.02)
            claimed = jobs.claim_next(jobs_dir=store)
            jobs.finish(claimed, jobs.STATE_DONE, result={"events": 3},
                        jobs_dir=store)

        asyncio.create_task(finisher())
        out = await job_runner.wait_for(job.id, poll=0.01, timeout=3, jobs_dir=store)
        assert out.state == jobs.STATE_DONE
        assert out.result["events"] == 3

    async def test_an_unknown_job_returns_none(self, store):
        assert await job_runner.wait_for("j_nope", timeout=0.1, jobs_dir=store) is None

    async def test_it_gives_up_rather_than_blocking_for_ever(self, store):
        job = _submit(store)
        out = await job_runner.wait_for(job.id, timeout=0.05, poll=0.01,
                                        jobs_dir=store)
        assert out.state == jobs.STATE_QUEUED, "returned the job, still not terminal"
