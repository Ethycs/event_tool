"""Both feeds: the site and the MCP server put work on the same queue.

The point of the queue is not scheduling — it is that **only one process runs
the pipeline**. So the tests that matter are the ones proving each surface
*submits* rather than executes, and that the shared contract (`harvest_events`'
return shape) did not change while that happened.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from event_harvester import job_executor, jobs
from event_harvester.servers import mcp_server
from event_harvester.servers import site as site_mod


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the whole queue at a temp directory for every test."""
    queue = tmp_path / "jobs"
    monkeypatch.setattr(jobs, "JOBS_DIR", queue)
    return queue


class TestRequestBuilding:
    def test_unset_keys_are_dropped(self):
        """Coalescing hashes this dict; None-vs-absent must not differ."""
        assert job_executor.build_request(days=7) == {"days": 7}

    def test_two_equivalent_requests_coalesce(self, store):
        a = job_executor.build_request(days=7)
        b = job_executor.build_request(days=7, only=None, web_source=None)
        assert jobs.request_fingerprint(a) == jobs.request_fingerprint(b)

    def test_a_real_difference_survives(self):
        a = job_executor.build_request(days=7)
        b = job_executor.build_request(days=1)
        assert jobs.request_fingerprint(a) != jobs.request_fingerprint(b)


class TestTheExecutorDoesNotReimplementThePipeline:
    async def test_it_calls_the_shared_entry_point(self, store):
        job = jobs.submit({"days": 3}, jobs_dir=store)
        sink = _sink(job, store)

        fake = AsyncMock(return_value={
            "ok": True,
            "stats": {"events": 4, "new_events": 2, "total_messages": 40},
            "warnings": [],
        })
        with patch.object(mcp_server, "run_event_pipeline", fake):
            result = await job_executor.execute_harvest_job(job, sink)

        assert fake.await_count == 1
        assert fake.await_args.kwargs["days"] == 3
        assert result["events"] == 4

    async def test_the_sink_is_handed_to_the_pipeline_as_its_reporter(self, store):
        """So progress and Stop come from the pipeline's own boundaries."""
        job = jobs.submit({}, jobs_dir=store)
        sink = _sink(job, store)

        fake = AsyncMock(return_value={"ok": True, "stats": {}})
        with patch.object(mcp_server, "run_event_pipeline", fake):
            await job_executor.execute_harvest_job(job, sink)
        assert fake.await_args.kwargs["reporter"] is sink

    async def test_unknown_request_keys_never_reach_the_pipeline(self, store):
        job = jobs.submit({"days": 1, "nonsense": "boom"}, jobs_dir=store)
        sink = _sink(job, store)

        fake = AsyncMock(return_value={"ok": True, "stats": {}})
        with patch.object(mcp_server, "run_event_pipeline", fake):
            await job_executor.execute_harvest_job(job, sink)
        assert "nonsense" not in fake.await_args.kwargs


class TestTheSinkIsARealPipelineReporter:
    """`Pipeline` takes a reporter with exactly three methods."""

    def test_it_has_the_protocol_surface(self, store):
        job = jobs.submit({}, jobs_dir=store)
        sink = _sink(job, store)
        for name in ("update_stage", "log", "checkpoint"):
            assert callable(getattr(sink, name)), name

    async def test_checkpoint_is_where_stop_lands(self, store):
        from event_harvester.job_runner import Cancelled

        job = jobs.submit({}, jobs_dir=store)
        jobs.claim_next(jobs_dir=store)
        sink = _sink(job, store)

        await sink.checkpoint()                       # quiet when not cancelled
        jobs.request_cancel(job.id, jobs_dir=store)
        with pytest.raises(Cancelled):
            await sink.checkpoint()

    def test_update_stage_records_the_stage(self, store):
        job = jobs.submit({}, jobs_dir=store)
        sink = _sink(job, store)
        sink.update_stage("extract", "Extract events (LLM)")
        assert jobs.load(job.id, store).progress["stage"] == "extract"


class TestMcpFeedsTheQueue:
    async def test_it_queues_when_this_process_owns_the_runner(self, store):
        submitted = {}

        async def fake_wait(job_id, **kw):
            submitted["id"] = job_id
            job = jobs.load(job_id)
            claimed = jobs.claim_next()
            return jobs.finish(claimed, jobs.STATE_DONE,
                               result={"warnings": []}) if claimed else job

        with patch.object(mcp_server, "runner_active", return_value=True), \
             patch("event_harvester.job_runner.wait_for", fake_wait), \
             patch.object(mcp_server, "_load_latest_events",
                          return_value={"events": [{"title": "X"}], "stats": {}}):
            out = await mcp_server._queue_harvest({"days": 5})

        assert submitted["id"].startswith("j_")
        assert out["ok"] is True
        assert out["job_id"] == submitted["id"]

    async def test_the_return_shape_is_unchanged(self, store):
        """Every existing MCP client depends on these keys."""
        async def fake_wait(job_id, **kw):
            claimed = jobs.claim_next()
            return jobs.finish(claimed, jobs.STATE_DONE, result={"warnings": []})

        with patch("event_harvester.job_runner.wait_for", fake_wait), \
             patch.object(mcp_server, "_load_latest_events",
                          return_value={"events": [], "stats": {"events": 0}}):
            out = await mcp_server._queue_harvest({"days": 1})

        for key in ("ok", "warnings", "stats", "events"):
            assert key in out, key

    async def test_a_failed_job_is_reported_not_raised(self, store):
        async def fake_wait(job_id, **kw):
            claimed = jobs.claim_next()
            return jobs.finish(claimed, jobs.STATE_FAILED,
                               result={"error": "cloudflare said no"})

        with patch("event_harvester.job_runner.wait_for", fake_wait):
            out = await mcp_server._queue_harvest({})
        assert out["ok"] is False
        assert "cloudflare" in out["error"]

    async def test_a_job_nobody_drained_says_so(self, store):
        async def fake_wait(job_id, **kw):
            return jobs.load(job_id)          # still queued

        with patch("event_harvester.job_runner.wait_for", fake_wait):
            out = await mcp_server._queue_harvest({})
        assert out["ok"] is False


class TestTheSiteFeedsTheQueue:
    async def test_the_site_submits_and_never_runs_a_pipeline_itself(self, store):
        """The whole point: two processes must not both harvest."""
        from event_harvester.config import load_config

        async def fake_wait(job_id, **kw):
            claimed = jobs.claim_next()
            return jobs.finish(claimed, jobs.STATE_DONE, result={})

        with patch("event_harvester.job_runner.wait_for", fake_wait), \
             patch("event_harvester.pipeline.Pipeline") as Pipeline, \
             patch.object(site_mod, "_read_cached_events",
                          return_value=([], {}, 0)):
            await site_mod._collect(load_config(), harvest=True)

        Pipeline.assert_not_called()

    async def test_a_job_nobody_picked_up_names_the_likely_cause(self, store):
        from event_harvester.config import load_config

        async def fake_wait(job_id, **kw):
            return jobs.load(job_id)          # never drained

        with patch("event_harvester.job_runner.wait_for", fake_wait):
            with pytest.raises(RuntimeError, match="EventHarvesterMCP"):
                await site_mod._collect(load_config(), harvest=True)

    async def test_not_harvesting_reads_the_cache_without_queueing(self, store):
        from event_harvester.config import load_config

        with patch.object(site_mod, "_read_cached_events",
                          return_value=([{"title": "cached"}], {}, 1)) as read:
            events, _, _ = await site_mod._collect(load_config(), harvest=False)

        assert read.call_count == 1
        assert events[0]["title"] == "cached"
        assert jobs.list_jobs(bucket=jobs.QUEUED, jobs_dir=store) == []


def _sink(job, store):
    from event_harvester.job_runner import ProgressSink

    return ProgressSink(job, jobs_dir=store, min_interval=0.0)


class TestTheExecutorCallIsValid:
    """The signature check that used to guard `site._collect`.

    The site shipped for a day calling `Pipeline.run()` without its required
    `no_kwargs`, because every test patched the seam. The seam moved here —
    `execute_harvest_job` is now the only place a queued harvest enters the
    pipeline — so the guard moves with it.
    """

    def test_the_executor_only_passes_arguments_run_event_pipeline_accepts(self):
        import inspect

        signature = inspect.signature(mcp_server.run_event_pipeline)
        for key in job_executor._PASSTHROUGH:
            assert key in signature.parameters, f"{key} is not a real parameter"
        assert "reporter" in signature.parameters

    async def test_a_full_request_binds_against_the_real_signature(self, store):
        import inspect

        request = job_executor.build_request(
            days=7, only={"web"}, skip=None, web_source="stud",
            no_cooldown=True, no_cap=True, total_cap=50, include_known=True,
        )
        job = jobs.submit(request, jobs_dir=store)
        sink = _sink(job, store)

        captured = {}

        async def capture(**kwargs):
            captured.update(kwargs)
            return {"ok": True, "stats": {}}

        with patch.object(mcp_server, "run_event_pipeline", capture):
            await job_executor.execute_harvest_job(job, sink)

        inspect.signature(mcp_server.run_event_pipeline).bind(**captured)

    def test_a_request_is_json_serialisable(self):
        """`--only` yields a set, which json.dumps refuses outright."""
        import json

        request = job_executor.build_request(days=1, only={"web", "gmail"})
        json.dumps(request)

    def test_an_unordered_selection_still_coalesces(self):
        """A set's iteration order must not create two jobs for one request."""
        a = job_executor.build_request(only={"web", "gmail"})
        b = job_executor.build_request(only={"gmail", "web"})
        assert jobs.request_fingerprint(a) == jobs.request_fingerprint(b)
