"""The harvest job queue — many submitters, one runner.

Five things could start a harvest and every one of them was fire-and-forget:
the CLI, the Flet UI, MCP ``harvest_events``, the site's timer, and
``POST /refresh``. The site had a single-flight lock, so a second request was
**dropped on the floor** rather than queued. There was no id, no history, no
cancel, and no way to ask what was running.

This module is the queue they all feed.

Why a directory, not a file
---------------------------
The site (``:8800``) and the MCP backend (``:8765``) are **separate OS
processes**. A queue held in one process's memory does not bind the other, and
a single JSON file shared between them would tear — ``utils.save_json`` is a
plain ``write_text``, not an atomic replace.

So the store is one file per job, published with a temp-write plus
``os.replace``, laid out by state:

    data/.jobs/
      queued/    j_....json     submitted, waiting
      running/   j_....json     claimed by a runner
      finished/  j_....json     done | failed | cancelled | interrupted

**Claiming is ``os.rename`` from ``queued/`` to ``running/``**, which is atomic
and fails if the destination exists. That makes the claim a mutex for free:
if two runners ever race, exactly one wins and the loser moves on. We intend
one runner, but a queue whose correctness depends on that intention holding is
a queue that breaks the first time it does not.

Serial by design
----------------
One ``Pipeline`` runs at a time. This is not a throughput feature — it is a
*requests stop being dropped* feature. Three resources are effectively
singletons and concurrency would corrupt all three:

* native Chrome takes a fixed ``--remote-debugging-port=9222``
* ``data/.message_cache.json`` is one global file whose validity test is
  ``cached_sources == active_sources`` — two selections invalidate each other
  and race on the whole-file write
* the latest-events cache is indexed **by position** by ``approve_event``

See ``docs/01 - Design/03 - The Job Queue.md``.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from event_harvester.utils import data_path

logger = logging.getLogger("event_harvester.jobs")

JOBS_DIR = data_path(".jobs")

#: Sub-directories are the state machine's coarse states. The fine state
#: (done vs failed vs cancelled) lives in the record.
QUEUED = "queued"
RUNNING = "running"
FINISHED = "finished"
_BUCKETS = (QUEUED, RUNNING, FINISHED)

# ── states ────────────────────────────────────────────────────────────────
STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
STATE_INTERRUPTED = "interrupted"

TERMINAL_STATES = frozenset(
    {STATE_DONE, STATE_FAILED, STATE_CANCELLED, STATE_INTERRUPTED}
)

# ── priority ──────────────────────────────────────────────────────────────
#: A person pressing Run must not wait behind the nightly sweep. Two levels
#: are enough; resist a general priority field until something needs it.
PRIORITY_SCHEDULED = 0
PRIORITY_INTERACTIVE = 10

#: Reject rather than accept work that will never run.
MAX_QUEUE_DEPTH = 50

#: History bounds. `data/.web_traces` grew to 124 MB with no cleanup policy;
#: that is a mistake worth not repeating.
MAX_HISTORY = 200
MAX_HISTORY_DAYS = 30

#: Bounded, because the full log already goes to the service log.
MAX_LOG_LINES = 200


class JobError(RuntimeError):
    """The queue refused the request."""


class QueueFull(JobError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


@dataclass
class Job:
    """One harvest request and everything that happened to it."""

    id: str
    state: str = STATE_QUEUED
    priority: int = PRIORITY_INTERACTIVE
    request: dict[str, Any] = field(default_factory=dict)
    submitted_by: str = "unknown"
    submitted_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    #: How many identical requests arrived while this one waited. Surfacing
    #: it beats silently dropping them.
    coalesced: int = 0
    progress: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    log_tail: list[str] = field(default_factory=list)

    # ── derived ───────────────────────────────────────────────────────────
    @property
    def fingerprint(self) -> str:
        return request_fingerprint(self.request)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Job:
        known = {f: data.get(f) for f in cls.__dataclass_fields__ if f in data}
        return cls(**known)

    def append_log(self, line: str) -> None:
        self.log_tail.append(line)
        if len(self.log_tail) > MAX_LOG_LINES:
            del self.log_tail[: len(self.log_tail) - MAX_LOG_LINES]


#: Monotonic within a process, to break timestamp ties. See `new_job_id`.
_SEQUENCE = itertools.count()


def new_job_id(now: datetime | None = None) -> str:
    """Sortable by construction, so listing by name is listing by time.

    The id is ``j_<microsecond stamp>_<sequence>_<random>`` and each part
    earns its place:

    * **Microsecond stamp** — FIFO order *is* id order, so second resolution
      would order same-second submissions by their random suffix. That is
      precisely the case that matters: two clicks of Run.
    * **Sequence** — a stamp alone is not enough. `datetime.now()` on Windows
      has ~15 ms granularity, so a burst of submissions lands in the *same*
      microsecond and ties again. A per-process counter breaks that.
    * **Random** — breaks ties between processes.

    `known rough edge`: two processes submitting inside the same clock tick
    order arbitrarily, because nothing is shared between them to order by.
    That is a fairness question, not a correctness one — the claim is still a
    mutex, so no job runs twice or gets lost.
    """
    stamp = (now or _now()).strftime("%Y%m%dT%H%M%S%f")
    seq = next(_SEQUENCE) % 1_000_000
    return f"j_{stamp}_{seq:06d}_{uuid.uuid4().hex[:4]}"


def request_fingerprint(request: dict[str, Any]) -> str:
    """Stable hash of a request, for coalescing.

    Sorted keys so dict ordering cannot make two identical requests look
    different — which would defeat the whole point.
    """
    canonical = json.dumps(request or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ── storage ───────────────────────────────────────────────────────────────


def _root(jobs_dir: Path | None = None) -> Path:
    return Path(jobs_dir or JOBS_DIR)


def _bucket_dir(bucket: str, jobs_dir: Path | None = None) -> Path:
    path = _root(jobs_dir) / bucket
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """Publish a job file so no reader can ever see it half-written.

    `utils.save_json` is a plain `write_text`; with two processes reading this
    directory that is a torn read waiting to happen.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    os.replace(tmp, path)  # atomic on the same volume, Windows included


def _read(path: Path) -> Job | None:
    try:
        return Job.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError) as e:
        logger.warning("jobs: skipping unreadable %s (%s)", path.name, e)
        return None


def _path_for(job_id: str, bucket: str, jobs_dir: Path | None = None) -> Path:
    return _bucket_dir(bucket, jobs_dir) / f"{job_id}.json"


def _locate(job_id: str, jobs_dir: Path | None = None) -> tuple[Path, str] | None:
    for bucket in _BUCKETS:
        path = _path_for(job_id, bucket, jobs_dir)
        if path.exists():
            return path, bucket
    return None


def load(job_id: str, jobs_dir: Path | None = None) -> Job | None:
    found = _locate(job_id, jobs_dir)
    return _read(found[0]) if found else None


def list_jobs(
    *,
    bucket: str | None = None,
    state: str | None = None,
    limit: int | None = None,
    jobs_dir: Path | None = None,
) -> list[Job]:
    """Newest first. Ids sort chronologically, so this is a name sort."""
    buckets = [bucket] if bucket else list(_BUCKETS)
    jobs: list[Job] = []
    for b in buckets:
        for path in sorted(_bucket_dir(b, jobs_dir).glob("j_*.json"), reverse=True):
            job = _read(path)
            if job and (state is None or job.state == state):
                jobs.append(job)
    jobs.sort(key=lambda j: j.id, reverse=True)
    return jobs[:limit] if limit else jobs


def save(job: Job, jobs_dir: Path | None = None) -> Job:
    """Write a job in place, in whichever bucket currently holds it."""
    found = _locate(job.id, jobs_dir)
    bucket = found[1] if found else _bucket_for_state(job.state)
    _atomic_write(_path_for(job.id, bucket, jobs_dir), job.to_dict())
    return job


def _bucket_for_state(state: str) -> str:
    if state == STATE_QUEUED:
        return QUEUED
    if state == STATE_RUNNING:
        return RUNNING
    return FINISHED


# ── submit ────────────────────────────────────────────────────────────────


def submit(
    request: dict[str, Any],
    *,
    submitted_by: str = "unknown",
    priority: int | None = None,
    now: datetime | None = None,
    jobs_dir: Path | None = None,
) -> Job:
    """Enqueue a harvest request, or join an identical one already waiting.

    Coalescing matters more than it looks: without it, one wedged job turns a
    nightly timer into a week of identical stacked jobs, and clearing the jam
    runs all seven.

    A **running** job is never coalesced against — its inputs were read when it
    started, so a request arriving mid-run legitimately wants fresher data.
    """
    now = now or _now()
    if priority is None:
        priority = (
            PRIORITY_SCHEDULED if submitted_by == "timer" else PRIORITY_INTERACTIVE
        )

    fingerprint = request_fingerprint(request)
    for existing in list_jobs(bucket=QUEUED, jobs_dir=jobs_dir):
        if existing.fingerprint != fingerprint:
            continue
        existing.coalesced += 1
        # Deliberately NOT touching submitted_at. Bumping it would push this
        # job *later* in FIFO order every time a duplicate arrived, which
        # starves exactly the request that keeps being asked for.
        if priority > existing.priority:
            existing.priority = priority
        save(existing, jobs_dir)
        logger.info("jobs: coalesced into %s (now %d)", existing.id, existing.coalesced)
        return existing

    depth = len(list_jobs(bucket=QUEUED, jobs_dir=jobs_dir))
    if depth >= MAX_QUEUE_DEPTH:
        raise QueueFull(
            f"{depth} jobs already queued (max {MAX_QUEUE_DEPTH}). "
            "Something is not draining — check the runner."
        )

    job = Job(
        id=new_job_id(now),
        state=STATE_QUEUED,
        priority=priority,
        request=dict(request or {}),
        submitted_by=submitted_by,
        submitted_at=_iso(now),
    )
    _atomic_write(_path_for(job.id, QUEUED, jobs_dir), job.to_dict())
    logger.info("jobs: queued %s by %s (priority %d)", job.id, submitted_by, priority)
    return job


# ── claim / finish ────────────────────────────────────────────────────────


def _queue_order(jobs: list[Job]) -> list[Job]:
    """Priority descending, then FIFO by submission."""
    return sorted(jobs, key=lambda j: (-j.priority, j.id))


def claim_next(
    *, now: datetime | None = None, jobs_dir: Path | None = None
) -> Job | None:
    """Atomically take the next job, or return None.

    The claim is `os.rename` from `queued/` to `running/`: atomic, and it
    fails if the destination exists. Two racing runners cannot both win.
    """
    now = now or _now()
    for job in _queue_order(list_jobs(bucket=QUEUED, jobs_dir=jobs_dir)):
        src = _path_for(job.id, QUEUED, jobs_dir)
        dst = _path_for(job.id, RUNNING, jobs_dir)
        try:
            os.rename(src, dst)
        except OSError:
            # Another runner claimed it, or it was cancelled out from under
            # us. Both are fine; try the next one.
            continue
        job.state = STATE_RUNNING
        job.started_at = _iso(now)
        _atomic_write(dst, job.to_dict())
        logger.info("jobs: claimed %s", job.id)
        return job
    return None


def finish(
    job: Job,
    state: str,
    *,
    result: dict[str, Any] | None = None,
    now: datetime | None = None,
    jobs_dir: Path | None = None,
) -> Job:
    """Move a job out of `running/` into `finished/` with a terminal state."""
    if state not in TERMINAL_STATES:
        raise JobError(f"{state!r} is not a terminal state")
    job.state = state
    job.finished_at = _iso(now or _now())
    if result is not None:
        job.result = result
    _atomic_write(_path_for(job.id, FINISHED, jobs_dir), job.to_dict())
    for bucket in (QUEUED, RUNNING):
        stale = _path_for(job.id, bucket, jobs_dir)
        if stale.exists():
            try:
                stale.unlink()
            except OSError:  # pragma: no cover - racing cleanup
                pass
    _clear_cancel(job.id, jobs_dir)
    logger.info("jobs: %s -> %s", job.id, state)
    return job


# ── cancel ────────────────────────────────────────────────────────────────
#
# Cancel means two different things and the UI must not pretend otherwise.
# A queued job is dropped instantly. A running job gets a marker file, which
# the runner observes at a stage boundary — during a long LLM extract that
# can be a minute away.


def _cancel_marker(job_id: str, jobs_dir: Path | None = None) -> Path:
    return _bucket_dir(RUNNING, jobs_dir) / f"{job_id}.cancel"


def request_cancel(job_id: str, *, jobs_dir: Path | None = None) -> Job | None:
    found = _locate(job_id, jobs_dir)
    if not found:
        return None
    path, bucket = found
    job = _read(path)
    if job is None or job.is_terminal:
        return job
    if bucket == QUEUED:
        return finish(job, STATE_CANCELLED, jobs_dir=jobs_dir)
    _cancel_marker(job_id, jobs_dir).write_text("1", encoding="utf-8")
    logger.info("jobs: cancel requested for running %s", job_id)
    return job


def cancel_requested(job_id: str, *, jobs_dir: Path | None = None) -> bool:
    return _cancel_marker(job_id, jobs_dir).exists()


def _clear_cancel(job_id: str, jobs_dir: Path | None = None) -> None:
    marker = _cancel_marker(job_id, jobs_dir)
    if marker.exists():
        try:
            marker.unlink()
        except OSError:  # pragma: no cover
            pass


# ── maintenance ───────────────────────────────────────────────────────────


def reap_interrupted(*, jobs_dir: Path | None = None) -> list[Job]:
    """Mark anything left in `running/` as interrupted. Never re-run it.

    Called at runner startup. Auto-resuming would silently re-open browsers
    and re-spend LLM tokens on a schedule nobody was watching.
    """
    reaped = []
    for job in list_jobs(bucket=RUNNING, jobs_dir=jobs_dir):
        job.result = {**(job.result or {}), "error": "interrupted by restart"}
        reaped.append(finish(job, STATE_INTERRUPTED, jobs_dir=jobs_dir))
    if reaped:
        logger.warning("jobs: reaped %d interrupted job(s)", len(reaped))
    return reaped


def prune(
    *,
    max_jobs: int = MAX_HISTORY,
    max_age_days: int = MAX_HISTORY_DAYS,
    now: datetime | None = None,
    jobs_dir: Path | None = None,
) -> int:
    """Drop old finished jobs. Returns how many were removed."""
    now = now or _now()
    cutoff = now - timedelta(days=max_age_days)
    finished = list_jobs(bucket=FINISHED, jobs_dir=jobs_dir)
    removed = 0
    for index, job in enumerate(finished):
        too_many = index >= max_jobs
        too_old = False
        if job.finished_at:
            try:
                too_old = datetime.fromisoformat(job.finished_at) < cutoff
            except ValueError:
                too_old = False
        if not (too_many or too_old):
            continue
        try:
            _path_for(job.id, FINISHED, jobs_dir).unlink()
            removed += 1
        except OSError:  # pragma: no cover
            pass
    return removed


def queue_summary(*, jobs_dir: Path | None = None) -> dict[str, Any]:
    """What `/healthz` and the CLI both want to say in one line."""
    running = list_jobs(bucket=RUNNING, jobs_dir=jobs_dir)
    queued = _queue_order(list_jobs(bucket=QUEUED, jobs_dir=jobs_dir))
    return {
        "queued": len(queued),
        "running": running[0].id if running else None,
        "running_stage": (running[0].progress or {}).get("stage") if running else None,
        "next": [j.id for j in queued[:5]],
    }
