# The job queue — many requests, one runner

**Status:** ✅ **IMPLEMENTED 2026-08-01** (steps 1–4 and 7's data layer; see §10) · **Date:** 2026-07-31, updated 2026-08-01 · **Applies to:** `src/event_harvester/jobs.py`, `src/event_harvester/job_runner.py`, `src/event_harvester/job_executor.py`, `src/event_harvester/cli/commands/jobs.py`, `src/event_harvester/servers/site.py` (`SiteState`, `refresh`), `src/event_harvester/servers/mcp_server.py` (`harvest_events`, `run_event_pipeline`), `src/event_harvester/pipeline/orchestrator.py`, `src/event_harvester/pipeline/harvest.py` (`CACHE_FILE`) · **Related:** `01 - Design/02 - The Control UI.md` §4.5 / §6.2, roadmap 2.2

## 0. What is wrong now

Five things can start a harvest — the CLI, the Flet UI, MCP `harvest_events`, the site's 24 h timer, and `POST /refresh` — and **every one of them is fire-and-forget**. There is no id, no history, no cancel, and no way to ask what is running.

The site does have a guard: `SiteState` holds `refreshing: bool` and an `asyncio.Lock` (`site.py:61,65`). It is single-flight, which is correct as far as it goes, but a second request is **dropped on the floor**, not queued. Press Run twice and the second press does nothing, silently.

What is wanted: submit several requests and have them all get done.

## 1. Decision — serial execution, FIFO with priority

**One `Pipeline` runs at a time. Everything else waits in an ordered queue.**

This is deliberately not a throughput feature. It is a *"requests stop being dropped"* feature, and the reason is that three resources are effectively singletons:

| Resource | Evidence | What concurrency does to it |
|---|---|---|
| **Native Chrome** | `--remote-debugging-port=9222`, a fixed port (`uc_browser/browser.py:614`) | Second launch collides with the first |
| **`data/.message_cache.json`** | One global file; a hit requires `cached_sources == active_sources` (`harvest.py:98`) | Two jobs with different selections each invalidate the other, *and* race on the whole-file write |
| **The latest-events cache** | `approve_event` / `decline_event` index **by position** (`03 - Architecture/08 - MCP Server.md`) | Regenerating it under a pending review silently re-points every index |

Running jobs in parallel means fixing all three first. Serial execution needs none of them fixed, closes the dropped-request problem today, and leaves the parallel door open later.

*Rejected:* stage pipelining (job B harvests while A extracts) — genuinely ~2× on mixed loads, but needs per-stage leases and a per-job message cache. *Rejected:* full concurrency with resource pools — also requires making the event cache addressable rather than positional, which reaches into the review flow.

## 2. Where the queue lives — the decision that actually matters

A queue inside one process does not bind another process. **The site (`:8800`) and the MCP backend (`:8765`) are separate services and both can harvest right now** — the site's lock is invisible to the backend and vice versa. Adding an in-process queue to the site would leave that exactly as broken, while *looking* solved.

So: **the MCP backend owns the queue and is the only process that runs a `Pipeline`.** Everything else submits.

Why the backend rather than the site:

- It is already described as the warm pipeline process, holding `Pipeline`, the classifier and the reranker, deliberately kept alive so there is no per-session cold start.
- Service dependency already flows toward it — `EventHarvesterGateway` declares `DependOnService EventHarvesterMCP`. Adding the site to that is consistent rather than a new shape.
- The site is a presentation layer. Its 24 h timer becomes *submit a job*, which is one line and removes pipeline execution from the process that serves the public page.

**The cost, stated plainly:** the site can no longer harvest if the backend is down. It must then serve the last good page and say so on `/healthz` — which is exactly what it already does when a refresh fails. It must **not** fall back to running the pipeline itself, because that reintroduces the two-runners bug this decision exists to kill.

`known rough edge` — this is enforced by convention, not by a lock. A `--dry-run` CLI harvest still runs in its own process. A file lock at `data/.harvest.lock` would make it structural; recommended, not required for v1.

## 3. The job record

```jsonc
{
  "id": "j_2026-07-31T20-14-03_a4f1",   // sortable, no clock ambiguity
  "state": "queued",                     // queued|running|done|failed|cancelled|interrupted
  "priority": 10,                         // higher first; ties break FIFO
  "request": {
    "tags":      {"any": ["venues", "queer"]},
    "sources":   [],                      // explicit names, unioned
    "profile":   null,
    "platforms": {"discord": true, "signal": false},
    "caps":      {"web": 60, "total": 120},
    "dry_run":   false,
    "no_cooldown": false
  },
  "submitted_by": "site|mcp|cli|timer",
  "submitted_at": "...", "started_at": null, "finished_at": null,
  "progress": {"stage": null, "source_counts": {}, "messages": 0},
  "result":   {"events": 0, "rejects": {}, "error": null},
  "log_tail": []
}
```

`request` is the resolved selection from the run page (`02 - The Control UI.md` §4.4), stored as submitted so a job can be **re-run** verbatim later. That is the cheapest useful feature the queue unlocks.

### States

```
queued ──▶ running ──▶ done
   │          │──────▶ failed
   │          └──────▶ cancelled        (cooperative, at a stage boundary)
   ├──▶ cancelled                       (instant — it never started)
   └──▶ (process restart) ─────────────▶ interrupted
```

**Cancel is honest about its two meanings.** A queued job is dropped immediately. A running job sets a flag that `Pipeline` observes at `await self._checkpoint()` (`orchestrator.py:150,190,219,239`) — so it stops at the *next stage boundary*, which during a long LLM extract can be a minute away. The UI must say "stopping…" and not pretend otherwise.

**`interrupted` is a distinct state, not a retry.** If the service restarts mid-job, the job is marked `interrupted` and left alone. Auto-resuming would silently re-open browsers and re-spend LLM tokens on a schedule nobody watched.

## 4. Coalescing — the thing that stops the queue eating itself

If the 24 h timer fires while an identical request is already queued, **do not enqueue a second**. Return the existing id and count the join.

> **Corrected during implementation.** This section originally said to *bump* the existing job's `submitted_at`. That is wrong: queue order is submission order, so bumping pushes the job **later** every time a duplicate arrives — the request that keeps being asked for is the one that keeps getting starved. `submit()` leaves `submitted_at` alone and increments `coalesced` instead, which is also more useful: `+3` on a row says three identical requests are waiting on this one. Pinned by `test_coalescing_does_not_delay_the_job`.

Without this, one wedged job turns into a week of identical nightly jobs stacked behind it, and clearing the jam runs all seven. Coalesce on a fingerprint of `request` — the same normalise-and-hash the profile resolution already needs.

Never coalesce against a **running** job: its inputs were read when it started, so a request arriving mid-run legitimately wants fresher data.

## 5. Priority — one rule

**Interactive beats scheduled.** A person pressing Run should not wait behind the nightly sweep. Two levels are enough: `submitted_by: timer` → 0, everything else → 10. Resist a general priority field until something needs it.

## 6. Persistence

On disk under `data/.jobs/`, one file per job, so a restart does not lose the queue.

- History bounded — keep 200 jobs or 30 days, whichever is smaller, pruned on write. The traces directory already grows unbounded (`00 - System Overview.md` §4) and that is a mistake worth not repeating.
- `log_tail` capped at ~200 lines in the record; the full log stays in the service log.
- The queue file is written by one process (§2), so no locking protocol is needed — which is another reason single-ownership is worth the constraint.

## 7. Interface

| | |
|---|---|
| `POST /jobs` | submit → `202` + id (or the coalesced id) |
| `GET /jobs` | list, filterable by state |
| `GET /jobs/<id>` | one job |
| `DELETE /jobs/<id>` | cancel |
| `GET /jobs/<id>/stream` | SSE progress; poll `GET /jobs/<id>` is the fallback |

MCP gains `submit_job`, `job_status`, `list_jobs`, `cancel_job`.

**`harvest_events` stays, and stays blocking.** It is the tool every existing MCP client calls, and changing it to return an id would break them all. Reimplement it as *submit + wait for completion*, so there is exactly one execution path underneath. Its docstring should say it may now wait behind other work.

## 8. What this fixes, and what it does not

**Fixes:** the silently-dropped second request · no visibility into what is running · no history · no cancel · the timer racing a manual run · no way to re-run a past selection.

**Does not fix:**

- **Throughput.** Serial is serial. Six queued jobs take six jobs' worth of time.
- **A CLI harvest run outside the service.** Convention only until there is a file lock (§2).
- **The positional event cache.** Still the hazard it is today; the queue merely stops *making it worse* by never running two harvests at once.

## 9. Failure modes to design for

| Failure | Response |
|---|---|
| Job wedges (browser never returns) | Per-job wall-clock ceiling. Precedent: `DEFAULT_IO_TIMEOUT_S = 600` in `interlock.py`, chosen to turn "hangs forever" into "fails after a bounded wait". A full harvest measured **320 s** on 2026-07-31, so a 30 min ceiling is generous without being infinite |
| Service dies mid-job | `interrupted` on restart (§3). Never auto-resume |
| Queue grows unbounded | Cap depth (~50) and reject with a clear error, rather than accepting work that will never run |
| Every job fails identically (dead credential) | Consecutive-failure counter; after 3, pause the timer and surface it. Otherwise a dead token produces a failed job every night forever |

## 10. Sequencing

| # | Step | State | Notes |
|---|---|---|---|
| 1 | Job record + state machine + on-disk store | ✅ **done** | `jobs.py`, 35 tests |
| 2 | In-process serial runner in the MCP backend | ✅ **done** | `job_runner.py` + `job_executor.py`; `harvest_events` reimplemented on top, return shape unchanged |
| 3 | HTTP + MCP interface | 🟡 **partial** | CLI `jobs {list,show,cancel,submit,prune}` landed; `/jobs` HTTP routes and the `submit_job`/`job_status` MCP tools have not |
| 4 | Site's timer and `POST /refresh` submit instead of running | ✅ **done** | **The step that actually fixes the two-process bug.** `site._collect` no longer constructs a `Pipeline` at all |
| 5 | Coalescing, priority, bounded history | ✅ **done** | Landed with step 1 — cheaper together than bolted on |
| 6 | `data/.harvest.lock` so a stray CLI run cannot overlap | 🔲 open | Converts §2 from convention to structure. Still the largest gap |
| 7 | `/admin/jobs` UI | 🔲 open | `02 - The Control UI.md` §4.5. `/healthz` now carries `queue_summary()`, which is the data it needs |

## Open questions

1. **Does the CLI submit or run directly?** Submitting makes `event-harvester --days 7` fail when the service is down, which would be a regression for the primary interface. Running directly keeps the second-runner hole open. Leaning: CLI runs directly but takes the file lock (step 6), so it is exclusive without being dependent.
2. **SSE or polling for the job stream?** The site already polls `/healthz` every 60 s and on `visibilitychange`. Polling is proven here and simpler; SSE is nicer for a live log.
3. **Per-source jobs?** A "re-test just this one source" job is a different, much shorter shape than a full harvest, and might not want to queue behind one.

## 11. What implementation changed about the design

Four things the design did not anticipate, all found by building or running it.

**The store is a directory, and the claim is `os.rename`.** The design said "on disk under `data/.jobs/`". Building it made the claim protocol the interesting part: `queued/` → `running/` via `os.rename` is atomic and fails if the destination exists, so it *is* a mutex. Two racing runners cannot both win, which means correctness no longer depends on the single-runner convention holding — only throughput does.

**Job ids needed sub-second precision *and* a counter.** Queue order is id order. Second-resolution stamps ordered same-second submissions by their random suffix; microseconds were still not enough, because `datetime.now()` on Windows has ~15 ms granularity and a burst lands in one microsecond. The id is now `j_<µs stamp>_<per-process sequence>_<random>`. Found by a test, and it would have shown up as "two clicks of Run ran in the wrong order".

**`--only` produces a `set`, which `json.dumps` refuses.** Found by running `jobs submit --only web`. The deeper issue was not serialisation: an unordered collection also breaks coalescing, since the fingerprint hashes the request. `build_request` now normalises collections to sorted lists.

**Waiting needed two deadlines, not one.** The design had a single wall-clock ceiling. With only that, a caller submitting while the MCP service is stopped waits the full 30 minutes — and the *site* would hold its refresh lock for all of it. `wait_for` now also has a `pickup_timeout` (30 s): if nothing has claimed the job and nothing else is running, there is no live runner, so return and let the caller say so. The site's message names the likely cause.

## Verification

`verified 2026-08-01` — end to end, on the real queue with a real service:

```
event-harvester jobs submit --days 1 --only web --web-source the-bitter-end
  → Queued j_20260804T032904622990_000000_3bc9
event-harvester jobs submit   (identical, again)
  → Joined an identical queued job         # coalesced, +1, one job not two
event-harvester mcp --transport streamable-http --port 8770
  → mcp: job runner started — this process owns the queue
  → jobs: claimed j_2026...
event-harvester jobs list     (from a different process, mid-run)
  → running j_2026... (extract)            # real pipeline stage, cross-process
event-harvester jobs show j_2026...
  → done · 1 event · started 03:29:34 · finished 03:30:58
```

Tests: **906 passed** (was 826), lint **43**, unchanged.

`unverified` — the site's submit path has not been exercised against a live runner end to end; it is covered by tests and by the shared `wait_for`, but no scheduled site refresh has yet been drained by the MCP service on this machine.

`known rough edge` — stdio MCP still runs `harvest_events` inline rather than queueing, because a per-client subprocess must not own a queue. Step 6 (`data/.harvest.lock`) is what would close that; until then a stdio client and the service can still overlap.

Current-state claims below were read from source; numbers measured 2026-07-31:

```powershell
curl.exe http://127.0.0.1:8800/healthz   # {"refreshing": ..., "refreshes": N} — the whole
                                          # of today's job visibility
```

Full harvest wall-clock **320 s**. Checkpoint sites `orchestrator.py:150,190,219,239`. Message-cache validity test `harvest.py:98`. Fixed debug port `uc_browser/browser.py:614`.
