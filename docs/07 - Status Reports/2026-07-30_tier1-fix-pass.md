# Session Report — Fix pass 2: all of Tier 1

**Date**: 2026-07-30 · **Status**: ✅ LANDED
**Scope**: Tier 1 items 2–11 + Tier 2.1 (their shared root cause) + Tier 4.3
**Tests**: 542 passed, 0 failed (was 511) · **Follows**: [2026-07-30_feed-probe-and-otel.md](2026-07-30_feed-probe-and-otel.md)

## 1. Verify before fixing

Six of the eleven Tier 1 items were `[R]` — reported by a documentation agent during the doc build and never independently re-verified. Three earlier agent claims had already proven false, so every one was re-read against the code first.

All six were real. **Two were worse than reported:**

- **1.4 (approve-on-failure).** The report said the approved fingerprint was written "after an exception is logged." In fact `_on_approve`'s `except` block *explicitly* set `_status = "approved"` **and** called `save_fingerprint(status="approved")` — not a fall-through, a deliberate-looking pair of statements. The consequence is real data loss: an event that failed to sync was permanently hidden from the review queue *and* absent from TickTick, with only a log line as evidence.
- **1.6 (`no-web` profile).** The report scoped this to the shipped profile's `__none__` sentinel. The actual defect is in the filter: when *any* name filter matched zero sources, `web_kw["sources"]` was never assigned, and `fetch_web_sources(sources=None)` means **load them all**. So `--web-source lumaa` (a typo) ran a full 29-source web harvest. The profile was one symptom.

## 2. The refactor at the centre (Tier 2.1)

Items 1.2, 1.3 and 1.5 were one problem wearing three hats: `analysis.py` returned `(summary, events)` and smuggled everything else out through **module-level function attributes**.

```python
extract_events_llm._last_rejects = rejects
_extract_events_cloud._last_llm_dropped = llm_dropped
_parse_event_ini._last_dropped = dropped
```

Two independent failures fell out of that:

1. **Wrong backend (1.5).** The orchestrator read `_extract_events_cloud`'s attributes *unconditionally*. On a `backend="local"` run that function never executes, so the reject panels showed a previous cloud run's leftovers — or nothing.
2. **Cross-thread read (1.3).** `_parse_event_ini` wrote `_last_dropped` and the caller read it on the **next statement**, while up to 5 `ThreadPoolExecutor` workers called it concurrently. One batch could read a sibling's dropped list.

Everything now returns its data: `ExtractionResult(summary, events, rejects)` from `extract_events_llm`, `(summary, events, llm_rejects)` from both backends, `(summary, events, dropped)` from `_parse_event_ini`. `_extract_events_local` produces reject buckets for the first time.

**This was the prerequisite for 1.2, not merely adjacent to it.** Moving the extract stage onto a worker thread with `asyncio.to_thread` — the fix for the frozen Flet UI — would have made the shared-mutable-state race strictly worse. Order mattered.

## 3. Everything fixed

| # | What | Test that pins it |
|---|---|---|
| 1.2 | Extract stage no longer blocks the Flet event loop (`asyncio.to_thread`) | existing pipeline tests still green through the new await |
| 1.3 | `_parse_event_ini` returns `dropped` | `TestParseEventIniIsThreadSafe` (20 calls / 5 threads) |
| 1.4 | Failed TickTick sync no longer records an approval; `create_ticktick_tasks` grew a `failed` bucket | `TestFailureReporting` |
| 1.5 | Reject buckets come from the backend that ran | `TestRejectsComeFromTheBackendThatRan` |
| 1.6 | Zero-match source filter selects zero, not all | `TestWebSourceFilter` |
| 1.7 | CLI TickTick sync saves `status="approved"`, not `"auto"` | `TestFailureReporting::test_successful_creation_saves_an_approved_fingerprint` |
| 1.8 | `chromium_cache` `NameError` (undefined `gzip_event_score`/`gzip_threshold`) | `tests/test_chromium_cache.py` |
| 1.9 | Data paths anchored to `utils.REPO_ROOT`, not the CWD | covered indirectly; `REPO_ROOT` verified against `pyproject.toml` |
| 1.10 | `RyzenSession._recv` bounded by `io_timeout_s`, wedged worker killed | `tests/test_interlock.py` |
| 1.11 | Feed mode emits `platform: "web"` per EH-STD-MSG-v0 | `test_extract_posts_from_instagram_edges_shape` (fixed — see §4) |
| 4.3 | `search` mode accepts multiple queries | `TestNormalizeQueries` |

**1.9 got a small consolidation rather than two point fixes.** `FINGERPRINT_FILE` and `mcp_server.DATA_DIR` were bare `data/` paths — the stdio MCP server, launched by whatever client spawns it, used a *different* fingerprint store and event cache than the CLI. Both now anchor to one `utils.REPO_ROOT` / `data_path()`, which also replaces the hand-counted `.parents[N]` derivations scattered across four modules. That off-by-one had already shipped twice (the classifier directory, then the message cache landing in `src/data/`); it is now computed once, with the reasoning written down where it is computed.

## 4. Two tests that were lying

- **`test_extract_posts_from_instagram_edges_shape` passed `"web"` as the third argument** — the value production *should* have been sending. So it asserted `platform == "web"` and passed, while the real call site passed the source name and produced `platform: "instagram"`. A test can pin the right shape and still miss the bug when it supplies the input the caller gets wrong. It now passes a real source name.
- **`test_unlimited_caps_passes_none_to_extract`** (from fix pass 1) asserted that `None` was *passed*, not what happened next. Same failure mode, already recorded.

Both are instances of the pattern the roadmap flags: the suite pinned calls, not effects. The tests added across these three passes are the first that measure effects.

## 5. Mistake made in this session

**I introduced a hang while fixing a hang.** The first `_recv` timeout used `concurrent.futures.ThreadPoolExecutor`. Pool threads are **non-daemon** and joined by an `atexit` hook, so abandoning a blocked `readline` on one hangs *interpreter shutdown* instead of the call — strictly worse than the bug being fixed. The unit tests passed; the full suite hung at exit, which is how it surfaced. Now an explicit daemon `threading.Thread`, with the reasoning in the docstring so it does not get "simplified" back.

## 6. State at handoff

| Fact | Value |
|---|---|
| Tests | 542 passed, 0 failed, ~12s |
| Modules | 69/69 import cleanly |
| Lint | 43 ruff errors in `src/`, all pre-existing style (E501/E402/E741); down from 102 at the start of the day. Three genuinely dead locals removed |
| Roadmap | Tier 0 closed except 0.9 (upstream submodule); **Tier 1 fully closed**; Tier 2.1 closed |
| Git | **Everything below is uncommitted**, on top of `ed3a4ee "Docs Pass"`. 54 modified files under `src/` + `tests/`, 20 modified docs, `README.md`, `.gitignore`, and 7 new untracked files (3 status reports, `sources/feed_probe.py`, `tests/test_feed_probe.py`, `tests/test_interlock.py`, `tests/test_chromium_cache.py`) |
| Not mine | `.dvc/`, `.dvcignore`, six `*.dvc` files still **untracked** — do not commit without asking. Three long-running `event-harvester` python processes (started 7/28 and 7/29) are the user's and were left alone |

### What landed across the three passes, in one place

| Pass | Date | Headline |
|---|---|---|
| Docs tree build | 07-29 | 39-doc numbered tree; found the defect list everything since has been working through |
| Fix pass 1 | 07-29 | Tier 0 (README → landing page, `.token-oauth`, 5 messaging/doc defects) + Tier 1.1 (`--only`'s phantom 150 cap) |
| Feed probe + OTel | 07-30 | `sources/feed_probe.py` + `web probe`; package-wide OTel via a `traced` decorator (LLM token counts and TickTick failures went from unobservable to observable) |
| Tier 1 fix pass | 07-30 | Tier 1 items 2–11 + Tier 2.1 (the function-attribute side channels) + Tier 4.3 (multi-query search) |

**First action next session**: `pixi run test` (expect 542). Then Tier 4.7 — spike the Eventbrite/Meetup APIs before building `steps[]`, since an API path could delete the hardest third of that work. Tier 4.3 landed here, so Meetup can already be given several queries in `data/web_sources.json` today.

## Verification

```powershell
pixi run test                            # 542 passed
pixi run python scripts/mcp_probe.py     # 20 tools, 2 resources
pixi run event-harvester web probe       # 1/27 (sf-eagle)
```

`unverified` — the Flet approve/decline paths have no automated coverage (`ui/app.py` remains untested, Tier 2.2), so 1.4's UI half is read-verified only: `_sync_one_to_ticktick` is exercised by nothing. `TestFailureReporting` pins the `create_ticktick_tasks` contract it depends on, which is the part that was silently lying, but the handler wiring itself has not been run.
