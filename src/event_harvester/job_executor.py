"""What a harvest job actually does when the runner claims it.

Deliberately thin. It maps a job's `request` dict onto `run_event_pipeline`
and returns a summary — it does **not** re-implement any part of the pipeline.
That matters: the system's first design axiom is *one pipeline, three callers*,
and the job queue is now a fourth caller, not a fourth implementation.

Keeping `run_event_pipeline` as the single entry also means the event cache is
written in exactly one place, which is what `approve_event`'s positional
indexing depends on.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("event_harvester.job_executor")

#: Request keys that map straight onto `run_event_pipeline`. Anything not in
#: here is ignored rather than passed through, so a malformed request cannot
#: reach the pipeline as an unexpected keyword.
_PASSTHROUGH = (
    "days", "only", "skip", "include_known", "web_source",
    "no_cooldown", "skip_analysis", "no_cap", "total_cap",
)


def _normalise(value: Any) -> Any:
    """Make a value JSON-safe and order-stable.

    Two reasons, and the second is the subtle one:

    1. The CLI's `--only`/`--skip` produce a **set** (`parse_platform_csv`),
       which `json.dumps` refuses outright.
    2. Even once it serialises, an unordered collection breaks **coalescing** —
       the fingerprint is a hash of this dict, so `{"web", "gmail"}` and
       `{"gmail", "web"}` must not produce two different jobs.
    """
    if isinstance(value, (set, frozenset)):
        return sorted(str(v) for v in value)
    if isinstance(value, (list, tuple)):
        return sorted(str(v) for v in value)
    return value


def build_request(
    *,
    days: int | None = None,
    only: Any = None,
    skip: Any = None,
    web_source: str | None = None,
    no_cooldown: bool | None = None,
    no_cap: bool = False,
    total_cap: int | None = None,
    include_known: bool = False,
    skip_analysis: bool | None = None,
) -> dict[str, Any]:
    """Build a job request: JSON-safe, order-stable, unset keys dropped.

    Dropping unset keys matters for coalescing too — `{"days": 7}` and
    `{"days": 7, "only": None}` must not look like different requests.
    """
    candidate = {
        "days": days, "only": only, "skip": skip, "web_source": web_source,
        "no_cooldown": no_cooldown, "no_cap": no_cap or None,
        "total_cap": total_cap, "include_known": include_known or None,
        "skip_analysis": skip_analysis,
    }
    return {
        key: _normalise(value)
        for key, value in candidate.items()
        if value is not None
    }


async def execute_harvest_job(job, sink) -> dict[str, Any]:
    """Run one queued harvest. Returns the job's `result`.

    `sink` is both the progress reporter and the cancellation channel — it
    implements the pipeline's reporter protocol, so stages and Stop come from
    the pipeline's own boundaries rather than a parallel mechanism.
    """
    from event_harvester.servers.mcp_server import run_event_pipeline

    request = {k: v for k, v in (job.request or {}).items() if k in _PASSTHROUGH}
    sink.stage("harvest")
    sink.log(f"job {job.id}: starting ({request or 'defaults'})")

    outcome = await run_event_pipeline(reporter=sink, **request)

    stats = outcome.get("stats") or {}
    sink.stage("done")
    result = {
        "ok": bool(outcome.get("ok")),
        "events": stats.get("events", 0),
        "new_events": stats.get("new_events", 0),
        "total_messages": stats.get("total_messages", 0),
        "sources": stats.get("sources", []),
        "rejects": stats.get("rejects", {}),
        "warnings": outcome.get("warnings") or [],
        "error": None,
    }
    sink.log(f"job {job.id}: {result['events']} event(s)")
    return result
