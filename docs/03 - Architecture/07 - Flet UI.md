# Flet UI — Control Surface

**Status:** CURRENT · **Date:** 2026-07-29 · **Applies to:** `src/event_harvester/ui/app.py`, `src/event_harvester/ui/server.py`, `src/event_harvester/ui/display.py`, `src/event_harvester/ui/__init__.py`, `src/event_harvester/cli/commands/serve.py` · **Depends on:** `docs/03 - Architecture/01 - Pipeline.md` (unverified — file not present in the repo at time of writing; referenced per house style)

`EventsApp` (`ui/app.py`) is a single Python object that drives both the pipeline-progress screen and the events-review screen inside one Flet page. `ui/server.py` is a separate, older markdown+HTTP review path that does not use Flet at all. `ui/display.py` is unrelated ANSI terminal-printing used by the CLI harvest path, not by the Flet UI.

## Hard constraints

1. **One asyncio loop, one process.** `cli/commands/serve.py:serve_cmd` calls `ft.app_async(target=app.main, view=ft.AppView.WEB_BROWSER, port=8550)`. Flet's async web server and the pipeline both run as tasks on that one event loop — there is no worker thread and no process boundary between "the UI" and "the pipeline."
2. **`app` is constructed once, outside the per-session callback.** `serve.py:157` builds `EventsApp(cfg, pipeline_runner=run_pipeline)` a single time, then passes `app.main` as the Flet session target. Flet invokes `main(page)` once per browser tab/reconnect, but every invocation mutates the *same* `EventsApp` instance — `self._page` and every cached `ft.Control` (`_cards_column`, `_log_column`, `_action_bar`, `_stage_rows`, …) are instance attributes, not per-session state. See "Long-lived mutable state" below.
3. **Checkpointing is coarse and cooperative, and one stage is not actually cooperative.** `Pipeline.run()` (`pipeline/orchestrator.py`) awaits `reporter.checkpoint()` at exactly four points — before `harvest`, before `filter`, before `extract`, before `match` — plus one more `await reporter.checkpoint()` added by `serve.py:114` before `quality_check`. Between checkpoints, pause/stop requests are not observed.
4. **`extract_events_llm` runs on a worker thread.** *Fixed 2026-07-30.* It is a plain `def`, not `async def`, and used to be invoked directly from `Pipeline.run` with no `await` and no `asyncio.to_thread`. While it ran (classifier, cross-encoder, then N batched LLM network calls) it blocked the single Flet event loop: no `page.update()` could flush, the log panel appeared frozen, and Pause/Stop clicks were queued but not serviced until it returned — defeating the checkpoint architecture for exactly the stage most likely to take minutes. The orchestrator now does `await asyncio.to_thread(extract_events_llm, ...)`, so the loop stays live for the whole extract stage. This depended on first removing the module-level function-attribute side channels the stage used to communicate through (see the Pipeline doc) — moving that onto another thread while it wrote shared mutable state would have been worse than the freeze.

## `app.py`

| | |
|---|---|
| **Domain** | Pipeline progress display, live config editing, event review/approve/decline, reject/trace inspection. |
| **Runs at** | Flet's asyncio event loop. UI event handlers (`on_click`/`on_change`) run as normal coroutine callbacks; the pipeline itself runs as one `asyncio.Task` per run, created via `page.run_task(self._run_pipeline)`. |
| **Inputs** | `cfg: AppConfig` (mutated in place), optional `events`/`rejects`/`stats` (for the out-of-band, no-pipeline mode), `pipeline_runner: Callable[[ProgressReporter, EventsApp], Awaitable[None]]`. |
| **Outputs** | Mutates `cfg` from UI fields; writes fingerprints via `save_fingerprint()`; creates TickTick tasks via `create_ticktick_tasks()`; renders the Flet page. No return value — it's a long-lived UI driver. |
| **Internal structure** | `ProgressReporter` (pipeline→UI bridge), `_UILogHandler` (logging→UI bridge), `PipelineStateMachine` (`pipeline/state.py`) for Start/Pause/Resume/Stop legality, two full-page render functions (`_render_progress_view`, `_render_events_view`) that clear and rebuild `page.controls` from scratch on every transition. |
| **Failure modes** | Exceptions inside `pipeline_runner` are caught in `_run_pipeline`, logged via `reporter.error()`, and the state machine is force-reset to `IDLE` via `sm.reset()` — the user sees a red stage icon and an `ERROR:` log line but the app stays usable. `asyncio.CancelledError` from `Stop` is caught, logged, re-raised (so the task actually finishes cancelling), and also resets to `IDLE`. `_refresh_page()` swallows *all* exceptions from `page.update()` silently (`except Exception: pass`) — a broken/stale session's `page.update()` failing produces no diagnostic anywhere. |

## `server.py`

| | |
|---|---|
| **Domain** | A completely separate, Flet-free review flow: generate a Markdown report (`events_report.md`) with `Approve`/`Decline` links pointing at a local `http.server.HTTPServer` on port 8111 (`_PORT`), for viewing in VS Code/Obsidian/a browser instead of the Flet page. |
| **Runs at** | Blocking, synchronous `HTTPServer.serve_forever()` on the main thread of whatever process calls `serve_events()`. Not async, not Flet — unrelated event loop from `app.py`. |
| **Inputs** | `events: list[dict]`, `port`. |
| **Outputs** | `events_report.md` on disk; `data/.server_events.json` snapshot (`_save_events`/`_load_events`) written after every approve/decline click; TickTick tasks on approve; fingerprints via `save_fingerprint()` on decline. |
| **Internal structure** | `_Handler(SimpleHTTPRequestHandler)` with a **class-level** `events: list[dict] = []` attribute reassigned per-call in `serve_events()` (`_Handler.events = events`) — fine for a single-process, single-server-instance run, but note it's shared mutable class state, not instance state. |
| **Failure modes** | TickTick/fingerprint exceptions on `/approve/N` or `/decline/N` are caught and rendered as plain-text error bodies (`msg = f"... failed: {e}"`); the click still returns HTTP 200 with the error text, no propagation to a caller. Out-of-range index → 400/404 plain text. `dead code?`/`known rough edge`: this module has no caller found in `cli/dispatch.py` or `cli/commands/*` — `serve_cmd` (the actual `serve` subcommand) uses `ui/app.py`'s `EventsApp`, not this module. `generate_serve_report`/`serve_events` appear to be an earlier, now-unwired review path (the module docstring's `pixi run event-harvester --serve` usage example doesn't match the current `serve` subcommand argument style either).

## `display.py`

| | |
|---|---|
| **Domain** | Pure ANSI terminal formatting/printing (`print_message`, `print_links`, `print_validated_events`, `print_raw_events`, `print_recruiter_grades`) plus a `logger` for `event_harvester`. No Flet, no HTTP. |
| **Runs at** | Synchronous, called inline wherever imported — no event loop of its own. |
| **Inputs** | Plain dicts/lists (messages, links, events, recruiter grades). |
| **Outputs** | `stdout` writes only. |
| **Internal structure** | Module-level color constants + five `print_*` functions. |
| **Failure modes** | None of note — pure formatting over `dict.get()`/`.get()` with defaults, no I/O beyond stdout. |
| **Callers** | `cli/commands/harvest.py`, `cli/commands/web.py`, `cli/commands/recruiters.py`, `watch.py`, `pipeline/harvest.py`, `outputs/ticktick.py`, `training/eval_classifier.py` — i.e. the CLI/terminal paths, never `ui/app.py`. It's a sibling of the Flet UI, not part of it. |

## Start/Stop/Pause/Resume/Refresh state machine

`PipelineStateMachine` (`pipeline/state.py`) is the source of truth; `EventsApp._sm` holds one instance for the app's lifetime (see constraint 2 — this is also shared across sessions).

```
                 start                          done
        ┌──────────────────────┐        ┌──────────────────────┐
        │                      ▼        │                      ▼
     ┌──────┐               ┌─────────┐            ┌──────┐
     │ IDLE │               │ RUNNING │───pause───▶ │PAUSED│
     └──────┘               └─────────┘◀──resume────└──────┘
        ▲   ▲                  │  │  │                 │  │
        │   │                  │  │  └──────done───────┼──┘  (finished while
        │   │                  │  │                     │      paused)
        │   └──────stop────────┘  │                     │
        │                         └───────stop──────────┘
        │                                                │
        │                         DONE ◀─────────────────┘
        │                          │
        └────────────start────────┘  (re-run from events view)
```

Transition table (`_TRANSITIONS` in `pipeline/state.py`):

| From | Legal targets |
|---|---|
| `IDLE` | `RUNNING` |
| `RUNNING` | `PAUSED`, `IDLE` (stop), `DONE` |
| `PAUSED` | `RUNNING` (resume), `IDLE` (stop), `DONE` (pipeline finished its last stage before the next checkpoint honored the pause) |
| `DONE` | `RUNNING` (re-run / Refresh) |

`transition()` raises `IllegalTransitionError` on anything not in this table (self-loops are silent no-ops); `reset()` bypasses the table entirely and forces `IDLE` — used only by the cancellation and exception handlers in `_run_pipeline`.

**Button → transition mapping** (`app.py`):

| Button | Handler | Guard | Effect |
|---|---|---|---|
| Start / Refresh / Re-run | `_start_clicked` (Refresh and Re-run both call this, `_rerun_clicked` is a thin alias) | no-op if `sm.is_active` or a prior task isn't `.done()` yet or `pipeline_runner is None` | reads UI fields into `cfg` (`_read_config_from_fields`), re-renders progress view, `sm.transition(RUNNING)`, spawns `page.run_task(self._run_pipeline)` |
| Pause | `_pause_clicked` | no-op unless `sm.is_running` | clears `_pause_event`; next `reporter.checkpoint()` call inside the pipeline will block on `ev.wait()` |
| Resume | `_resume_clicked` | no-op unless `sm.is_paused` | sets `_pause_event`; unblocks the waiting checkpoint; `sm.transition(RUNNING)` |
| Stop | `_stop_clicked` | none — legal from any state, hard reset | `task.cancel()`, also sets `_pause_event` (so a paused checkpoint wakes up into the cancellation instead of hanging forever), `sm.reset()` → `IDLE` unconditionally |

`race?` **Stop while in `extract_events_llm`:** `task.cancel()` only *schedules* delivery of `CancelledError` at the task's next `await`. This used to be acute — per the old hard-constraint 4 the extract stage awaited nothing for its full duration, so `Stop` flipped the UI to "Stopped"/`IDLE` while the run continued synchronously. With the stage now behind `asyncio.to_thread` there **is** an await point, so cancellation is delivered promptly. `known rough edge`: the underlying thread is not itself interruptible, so the LLM batch in flight still runs to completion in the background — the UI is honest about state again, but "Stopped" means "stopping" until that batch drains. `_start_clicked`'s `self._pipeline_task is not None and not self._pipeline_task.done()` guard prevents a *second* task from launching, so clicking Start again during this window is a silent no-op — but the button itself shows enabled, so the user sees no feedback explaining why nothing happened until the stale task finally reaches its next checkpoint and dies.

## `ProgressReporter` contract

`ProgressReporter` (`app.py:81`) is what `pipeline_runner` (here, `serve_cmd.run_pipeline` in `cli/commands/serve.py`) and `Pipeline.run()` are given instead of a direct UI reference; `pipeline/orchestrator.py` only depends on the `_ReporterProtocol` subset (`update_stage`, `log`, `checkpoint`) to avoid importing Flet.

| Method | Called by | Effect |
|---|---|---|
| `update_stage(stage_id, label=None)` | `Pipeline.run()` at each of the 5 built-in stages; `serve.py` for `quality_check`/`done` | Marks all earlier stages `"done"`, the named stage `"active"`, everything after `"pending"` (`_mark_stage`); sets the big headline label; calls `_refresh_page()` |
| `log(line, color=None)` | Explicit calls in `Pipeline.run()`/`serve.py`, **and** automatically for every `event_harvester.*` log record ≥ INFO via `_UILogHandler`, installed/removed around each run in `_run_pipeline` | Appends a `ft.Text` row to `_log_column`, trims to `_LOG_LINE_LIMIT` (250) lines, refreshes |
| `error(message)` | `_run_pipeline`'s `except Exception` handler | Logs an `"ERROR: …"` red line and marks the current stage `"error"` |
| `checkpoint()` (async) | `Pipeline.run()` × 4, `serve.py` × 1 (see hard constraint 3) | If `_pause_event` is unset, sets the stage label to `"Paused — click Resume to continue"` and `await`s the event; a cleared-then-set event is how Resume unblocks it. If `_pause_event` is already set (not paused), returns immediately — this is the only mechanism by which Pause actually pauses anything. |

`_UILogHandler` reads `event_harvester.logging_tags.task_tag` (a `contextvars.ContextVar`) to prefix log lines with a task tag when set — `unverified` where that tag is populated from in this subsystem; it's read here, not written.

## Live controls → `AppConfig` fields

All controls below live in `_build_config_section()`, rebuilt fresh on every render (progress view and events view both call it). Except where noted, edits are **not** applied to `cfg` until `_read_config_from_fields()` runs, which happens only inside `_start_clicked` — i.e. typing a new cap value does nothing to the live config until Start/Refresh/Re-run is clicked.

| Control | Writes | Takes effect |
|---|---|---|
| Per-source cap fields (Discord/Telegram/Gmail/Signal/Web/Total) | `cfg.caps.<key>` (int; `-1` = unlimited sentinel, values `< -1` silently ignored) | On next Start/Refresh click |
| "No cap" checkbox | `cfg.caps.no_cap` (bool) | Field disabled-state and `"-1"` display flip immediately on toggle (`_on_no_cap_toggle`); `cfg.caps.no_cap` itself only updates on next Start |
| Days back | `cfg.days_back` (int, must parse `> 0` or the edit is dropped) | On next Start/Refresh click |
| Source checkboxes (Discord/Telegram/Gmail/Signal/Web) | `cfg.sources.<key>` (bool) | On next Start/Refresh click |
| "Skip web cooldown" | `cfg.web.no_cooldown` (bool) | On next Start/Refresh click |
| "Skip LLM analysis" | `cfg.skip_analyze` (bool) | On next Start/Refresh click |
| "Dry-run TickTick" | `cfg.dry_run` (bool) | On next Start/Refresh click |
| Profile dropdown | `cfg.web.selected_names` + merges `profile.caps` into `cfg.caps` via `apply_caps_to_config` (`profiles.py:apply_profile`) | **Immediately** on selection (`_on_profile_select`) — the only control that mutates `cfg` outside the Start path. It first calls `_read_config_from_fields()` to preserve any in-progress edits, then applies the profile on top, then re-renders the current view. |
| Refresh button (caps header) | none directly — same handler as Start (`_start_clicked`) | Immediate — applies current caps and re-runs |
| Start / Re-run (appbar refresh icon in events view) | none directly | Same as above |

`known rough edge`: `apply_profile` can set caps keys beyond the six shown in the UI (e.g. a `luma=25` sub-cap via `apply_caps_to_config`, which extends `CapsConfig` dynamically) — those extra per-source-within-web caps are applied to `cfg` but have no corresponding field in the caps row, so a profile pick can silently set config the user cannot see or edit in the panel.

## Event-card render and batch approve/decline

`_build_event_card(ev)` renders one `ft.Card` per event: status icon (`_status_icon`) + title + `date · time · location` subtitle, a 300-char notes excerpt, an italic source line, and an action row. If `link` is set, an "Open link" button is always shown. If the event is not yet acted on (`_status` not in `{"approved", "declined"}`), Approve/Decline buttons are shown; otherwise a colored "Approved"/"Declined" label replaces them (cards are not re-actionable once decided, short of clearing the fingerprint store out-of-band).

`_on_approve(ev)` / `_on_decline(ev)` close over the specific event dict and are rebuilt fresh on every `_rebuild_cards()` call (filter/search change, approve, decline). `_on_approve_all` / `_on_decline_all` iterate `self.events` and act only on `_status == "new"`.

~~`BUG:` **TickTick failures are swallowed and the event is marked approved anyway.**~~ **Fixed 2026-07-30.** In both `_on_approve` and `_on_approve_all`, a TickTick API exception was logged but the `except` branch (single-approve) or the code after the `try` (batch) still ran `save_fingerprint(status="approved")` and set `ev["_status"] = "approved"`. The card showed "Approved" in green whether or not a task was created, and the approved fingerprint meant the event was **never offered again** — lost from the review queue and absent from TickTick, with only a log line in a panel the user may not have open.

Both handlers now route through `_sync_one_to_ticktick(ev) -> (ok, reason)`. On failure the event stays `_status == "new"` and a red line naming the event and the reason is appended to the log. Two subtleties encoded there:

- A **dry run** and **TickTick not being configured at all** both count as success. There is nothing to sync to, and the user's click is still their decision — only an actual attempt that failed blocks the fingerprint.
- `create_ticktick_tasks` catches per-task exceptions internally so one bad event cannot abort a batch, which means *returning* was never evidence of success. The helper checks the new `failed` bucket, not just the absence of an exception.

Both approve paths call `self._get_tt()`, which lazily constructs and caches a single TickTick client (`self._tt`) for the app's lifetime — combined with hard constraint 2, this client is shared across all sessions too.

`race?`: `_on_approve`/`_on_decline`/batch handlers run TickTick and fingerprint I/O synchronously inside a Flet `on_click` callback on the shared event loop — a slow TickTick call blocks the whole UI (all sessions, per constraint 2) for its duration, same class of issue as hard constraint 4 but triggered by user action instead of the pipeline.

`dead code?`: `_status_icon` has a grey `"known"` icon, the status filter dropdown offers `"Known"`, and both `_rebuild_cards()` and `_render_events_view()` compute an `n_known` count — but `_status_for` (`app.py:42`) and its pipeline-side twin `Pipeline._status_for` (`orchestrator.py:254`) only ever return a fingerprint's status via `find_acted_fingerprint()`, which by construction (`event_match.py:267-283`, `_ACTED_STATUSES = {"approved", "declined"}`) never returns a fingerprint with status `"known"`. No code path in this subsystem appears to set `ev["_status"] = "known"`, so the grey icon, the `"Known"` filter option, and the `n_known` counters look permanently dead against the current fingerprint semantics.

## Per-stage reject panels

`_build_reject_panels()` renders one `ft.ExpansionPanel` per non-empty stage in `self.rejects`, in fixed order: `classifier`, `reranker`, `caps`, `llm_past`, `llm_no_events`. `llm_past` items are event-shaped (`title — date (reason)`); the other four stages' items are message-shaped (`author: content[:100]`). `self.rejects` is populated once per pipeline run by `Pipeline.run()` (`pipeline/orchestrator.py:216-226`), which reads it off **module-level function attributes** on `extract_events_llm`/`_extract_events_cloud` (`pipeline/analysis.py`) set as a side effect of the just-finished LLM call. `race?` this is safe under the Flet UI's own single-active-task guard, but it is genuinely global mutable state on a function object — if this process ever runs a second concurrent extraction (e.g. a future gateway process hosting both `serve` and `mcp` in one interpreter), the panel could show another call's rejects. `unverified`: no such combined process exists in the repo today (`serve` and `mcp` are separate CLI subcommands per `cli/dispatch.py`).

## Traces panel

`_build_traces_panel()` returns `None` (no panel, no message, no log line) whenever: `cfg.web.debug_screenshots` is falsy, `data/.web_traces/` doesn't exist, it has no subdirectories, or none of those subdirectories has a `*.jsonl` file. There is nothing in the UI to tell the user *why* the panel is absent — `known rough edge`.

`cfg.web.debug_screenshots` is set only from the `--debug-web-screenshots` CLI flag in `serve_cmd` (`cli/commands/serve.py:65-67`, which also forces `debug_trace = True`); there is no live checkbox for it in `_build_config_section`, so it cannot be turned on from a running UI session.

When present, for each source directory it takes the lexicographically-latest `*.jsonl` (run id = filename stem), parses it line-by-line as JSON (malformed lines are logged and skipped, not fatal), and looks for a sidecar `<source>/<run_id>/quality.json` written by `NavQualityScorer.score_source` (`observability/nav_quality.py:433`) — confirmed the write path (`run_dir / "quality.json"`) matches the read path exactly. If present, it renders a colored badge (green=`content`, amber=`empty`/`cookie_banner`/`unknown`, red=anything else) with `nav_score` and `vlm_explanation`, plus an emoji-annotated `ExpansionTile` title. Each trace record's `image` field (relative path) is resolved against `traces_root` and rendered inline as an `ft.Image` with `error_content` fallback text if the file is missing (e.g. pruned/cleaned up on disk after the trace was recorded).

## Long-lived mutable UI state — where the races are

`EventsApp` is a **process-lifetime singleton**, not a per-session object (hard constraint 2). Everything below is an instance attribute mutated by both pipeline-task code and UI click handlers on the one shared event loop:

| State | Written by pipeline task | Written by UI handlers | Risk |
|---|---|---|---|
| `self._page` | — | `main()`, once per new session/tab connect | A second browser tab/reconnect overwrites `self._page`; all subsequent `_refresh_page()` calls target only the most-recently-connected session. `race?`/`BUG:` the first tab silently stops receiving updates (or the second tab's page gets updated with control trees never attached to its own client) — `_refresh_page()`'s bare `except Exception: pass` means this fails silently either way. |
| `self.events` | `set_events()` reassigns the whole list at pipeline completion | `_on_approve`/`_on_decline`/batch handlers mutate `ev["_status"]` in place on the *same* dicts | Not a data race (single loop), but a re-run in progress does not clear `self.events` until `set_events()` is called — the events view keeps showing the *previous* run's cards, with live Approve/Decline buttons, while a new pipeline run is already active underneath (Start/Refresh only clears `_pipeline_done` and swaps to the progress view; it does not touch `self.events`). |
| `self._sm`, `self._pause_event`, `self._pipeline_task` | `_run_pipeline` creates/awaits/clears these | `_pause_clicked`/`_resume_clicked`/`_stop_clicked` mutate the same objects | See the Stop/`extract_events_llm` `race?` above — the state machine can report `IDLE` while a stale task is still executing synchronously. |
| `self._tt` (TickTick client) | — | Lazily created by `_get_tt()`, called from approve handlers | Shared across all sessions/events once created; never invalidated if `cfg.ticktick` changes at runtime (it can't, currently, since TickTick config isn't UI-editable — `unverified` whether that's intentional). |
| `self._cards_column`, `_log_column`, `_action_bar`, `_stage_rows`, `_cap_fields`, etc. | `ProgressReporter`/`_run_pipeline` mutate rows/log via helper methods | Full-page renders (`_render_progress_view`/`_render_events_view`) *replace* these with brand-new `ft.Control` objects on every Start/Refresh | Any handler holding a stale closure over an old `ft.Control` (e.g. an `_on_approve(ev)` closure built before a re-render) would be updating a control no longer attached to the page tree; in practice `_rebuild_cards()` regenerates the whole `_cards_column.controls` list on every action, but this is enforced by convention, not by the type system. |

## Verification

```
pixi run event-harvester serve
```

Expect: a browser tab opens at `http://localhost:8550`. Initially the progress view — headline stage label, a `ft.ProgressBar`, the "Pipeline config" card (profile dropdown, days/sources/behavior toggles, per-source cap fields), a 6-row stage checklist (harvest → filter → extract → match → quality_check → done), and a live-scrolling monospace log panel — with the pipeline already running (per `main()`'s auto-start-on-first-session behavior) and Start disabled / Pause+Stop enabled. When extraction finishes, the view swaps to the events list: header counts (`N events · N new · N known · …`), status/source/search filter row, one card per event with Approve/Decline, and (if any rejects/traces exist) collapsible panels below the card list. `unverified`: the exact end-to-end behavior wasn't executed as part of writing this document — reading + static analysis only, per the task's "Do NOT run the app" instruction.

