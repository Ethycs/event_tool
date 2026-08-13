"""The single serial runner that drains the job queue.

`jobs.py` is the queue; this is the thing that empties it. Exactly one of
these should exist across the whole machine, and it lives in the MCP backend
because that is already the warm pipeline process — it holds `Pipeline`, the
classifier and the reranker, kept alive so there is no per-session cold start.

Everything else *submits*. The site's timer and `POST /refresh` submit. MCP
`harvest_events` submits and waits. That is what makes the two-processes-both
-harvesting bug go away: not the queue, but the fact that only one process
owns execution.

The executor is injected rather than imported so this module is testable
without a browser, an LLM, or a five-minute wall clock.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from event_harvester import jobs
from event_harvester.observability.metrics import count, record
from event_harvester.observability.tracing import fail_current_span, get_tracer

logger = logging.getLogger("event_harvester.job_runner")
tracer = get_tracer("event_harvester.job_runner")

#: How often to look for work when the queue is empty. A harvest takes
#: minutes, so a couple of seconds of latency on pickup is free.
POLL_SECONDS = 2.0

#: Wall-clock ceiling per job. The precedent is
#: `interlock.DEFAULT_IO_TIMEOUT_S`, chosen to turn "hangs forever" into
#: "fails after a bounded wait".
#:
#: **Raised from 30 minutes to 90 on 2026-08-13, after it killed a healthy
#: run 13 batches from the finish.** The old value was set against a harvest
#: measured at 320 s on 2026-07-31, and every term in that measurement has
#: since changed:
#:
#: * the message cache can be cold, so the run re-scrapes 33 web sources
#:   with browsers before extraction starts;
#: * `batch_size` went 10 → 5 for the local model's context window, which
#:   doubled the call count to 161;
#: * extraction moved to a **local** model that answers in ~10 s and, on a
#:   single-slot server, serialises — so 161 calls is ~27 minutes on its own.
#:
#: Harvest plus extract came to ~32 minutes and was cut off at 30. The lesson
#: is that this constant is a function of the deployment, not of the code, so
#: it is now overridable without an edit.
JOB_TIMEOUT_SECONDS = float(os.getenv("EH_JOB_TIMEOUT_SECONDS", 90 * 60))

#: A dead credential would otherwise produce a failed job every night for
#: ever. After this many consecutive failures the runner stops taking
#: scheduled work and says so.
MAX_CONSECUTIVE_FAILURES = 3


class Cancelled(Exception):
    """Raised inside an executor when the job has been cancelled."""


#: An executor takes (job, report) and returns the job's result dict.
#: `report` lets it publish progress without knowing about the store.
Executor = Callable[[jobs.Job, "ProgressSink"], Awaitable[dict[str, Any]]]


class ProgressSink:
    """How a running job publishes progress, and how it learns to stop.

    Writes are debounced: a harvest logs per source per run, and rewriting the
    job file on every line would turn a 320-second job into a few thousand
    disk writes for no benefit.
    """

    def __init__(self, job: jobs.Job, *, jobs_dir: Path | None = None,
                 min_interval: float = 1.0):
        self.job = job
        self._jobs_dir = jobs_dir
        self._min_interval = min_interval
        self._last_write = 0.0

    def stage(self, stage: str, **extra: Any) -> None:
        self.job.progress = {**(self.job.progress or {}), "stage": stage, **extra}
        self._flush(force=True)          # stage changes are always worth a write

    def update(self, **fields: Any) -> None:
        self.job.progress = {**(self.job.progress or {}), **fields}
        self._flush()

    # ── the Pipeline reporter protocol ────────────────────────────────────
    #
    # `Pipeline` takes a reporter with exactly these three methods
    # (`orchestrator.py:_ReporterProtocol`). Implementing it here rather than
    # inventing a parallel progress channel means job progress comes from the
    # real stage transitions, and Stop lands on the real `await
    # self._checkpoint()` boundaries the pipeline already has.

    def update_stage(self, stage_id: str, label: str | None = None) -> None:
        self.stage(stage_id, **({"label": label} if label else {}))

    def log(self, line: str, color: Any = None) -> None:
        self.job.append_log(line)
        self._flush()

    async def checkpoint(self) -> None:
        self.raise_if_cancelled()

    def _flush(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_write) < self._min_interval:
            return
        self._last_write = now
        jobs.save(self.job, self._jobs_dir)

    # ── cancellation ──────────────────────────────────────────────────────
    @property
    def cancelled(self) -> bool:
        return jobs.cancel_requested(self.job.id, jobs_dir=self._jobs_dir)

    def raise_if_cancelled(self) -> None:
        """Call at a stage boundary. Cancel is cooperative, by design."""
        if self.cancelled:
            raise Cancelled(self.job.id)


class JobRunner:
    """Drains the queue, one job at a time, for ever."""

    def __init__(
        self,
        executor: Executor,
        *,
        jobs_dir: Path | None = None,
        poll_seconds: float = POLL_SECONDS,
        timeout_seconds: float = JOB_TIMEOUT_SECONDS,
    ):
        self._executor = executor
        self._jobs_dir = jobs_dir
        self._poll = poll_seconds
        self._timeout = timeout_seconds
        self._stopping = asyncio.Event()
        self.consecutive_failures = 0
        self.jobs_run = 0

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def run_forever(self) -> None:
        """Reap, then drain until stopped."""
        jobs.reap_interrupted(jobs_dir=self._jobs_dir)
        logger.info("job runner: started")
        try:
            while not self._stopping.is_set():
                ran = await self.run_once()
                if ran:
                    continue
                # Nothing to do — wait, but wake immediately on stop.
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=self._poll)
                except asyncio.TimeoutError:
                    pass
        finally:
            logger.info("job runner: stopped after %d job(s)", self.jobs_run)

    def stop(self) -> None:
        self._stopping.set()

    # ── one job ───────────────────────────────────────────────────────────

    async def run_once(self) -> bool:
        """Claim and run one job. Returns False when the queue was empty."""
        if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            # Do not spin on a broken credential. Interactive work still runs,
            # because a person is there to see it fail.
            if not self._has_interactive_work():
                return False

        job = jobs.claim_next(jobs_dir=self._jobs_dir)
        if job is None:
            return False

        await self._execute(job)
        self.jobs_run += 1
        return True

    def _has_interactive_work(self) -> bool:
        return any(
            j.priority > jobs.PRIORITY_SCHEDULED
            for j in jobs.list_jobs(bucket=jobs.QUEUED, jobs_dir=self._jobs_dir)
        )

    async def _execute(self, job: jobs.Job) -> jobs.Job:
        sink = ProgressSink(job, jobs_dir=self._jobs_dir)
        started = time.monotonic()

        with tracer.start_as_current_span("job.run") as span:
            span.set_attribute("job.id", job.id)
            span.set_attribute("job.submitted_by", job.submitted_by)
            span.set_attribute("job.priority", job.priority)
            span.set_attribute("job.coalesced", job.coalesced)

            # A job cancelled between submit and claim must not run.
            if sink.cancelled:
                return self._finish(job, jobs.STATE_CANCELLED,
                                    {"error": "cancelled before start"}, span)

            try:
                result = await asyncio.wait_for(
                    self._executor(job, sink), timeout=self._timeout
                )
            except Cancelled:
                return self._finish(job, jobs.STATE_CANCELLED,
                                    {"error": "cancelled"}, span)
            except asyncio.TimeoutError:
                self.consecutive_failures += 1
                return self._finish(
                    job, jobs.STATE_FAILED,
                    {"error": f"exceeded {self._timeout:.0f}s wall clock"}, span,
                )
            except Exception as e:  # noqa: BLE001 - a job must not kill the runner
                logger.exception("job runner: %s failed", job.id)
                self.consecutive_failures += 1
                return self._finish(
                    job, jobs.STATE_FAILED,
                    {"error": f"{type(e).__name__}: {e}"}, span,
                )

            self.consecutive_failures = 0
            record("job.duration", time.monotonic() - started, unit="s")
            return self._finish(job, jobs.STATE_DONE, result or {}, span)

    def _finish(self, job: jobs.Job, state: str, result: dict, span) -> jobs.Job:
        span.set_attribute("job.state", state)
        if state in (jobs.STATE_FAILED,):
            fail_current_span(str(result.get("error"))[:200], error_type="job_failed")
        count("job.completions", 1, state=state, submitted_by=job.submitted_by)
        return jobs.finish(
            job, state, result=result,
            now=datetime.now(timezone.utc), jobs_dir=self._jobs_dir,
        )


# ── waiting, for the callers that still need to block ─────────────────────


#: How long to wait for *someone* to claim a job before concluding that no
#: runner is alive. Much shorter than the completion timeout on purpose: a
#: caller blocked on a queue nobody drains should find out in seconds, not
#: after the full wall-clock ceiling.
PICKUP_TIMEOUT_SECONDS = 30.0


async def wait_for(
    job_id: str,
    *,
    timeout: float = JOB_TIMEOUT_SECONDS,
    pickup_timeout: float | None = PICKUP_TIMEOUT_SECONDS,
    poll: float = 0.5,
    jobs_dir: Path | None = None,
) -> jobs.Job | None:
    """Block until a job reaches a terminal state.

    `harvest_events` is the reason this exists: it is the tool every existing
    MCP client calls, and changing it to return an id would break all of them.
    So it submits and waits, and there is still exactly one execution path
    underneath.

    Two deadlines, not one. If nothing claims the job within `pickup_timeout`
    **and** nothing else is running, there is no live runner — return the job
    still queued so the caller can say so. Otherwise a caller with the service
    stopped would block for the full 30-minute ceiling, and in the site's case
    would hold its refresh lock for all of it.
    """
    deadline = time.monotonic() + timeout
    pickup_deadline = (
        time.monotonic() + pickup_timeout if pickup_timeout else None
    )
    while time.monotonic() < deadline:
        job = jobs.load(job_id, jobs_dir)
        if job is None:
            return None
        if job.is_terminal:
            return job
        if job.state != jobs.STATE_QUEUED:
            pickup_deadline = None            # claimed; only the long clock now
        elif pickup_deadline and time.monotonic() > pickup_deadline:
            if not jobs.list_jobs(bucket=jobs.RUNNING, jobs_dir=jobs_dir):
                logger.warning(
                    "jobs: %s unclaimed after %.0fs and nothing is running — "
                    "no runner appears to be alive", job_id, pickup_timeout,
                )
                return job
            pickup_deadline = None            # busy, not dead; keep waiting
        await asyncio.sleep(poll)
    return jobs.load(job_id, jobs_dir)
