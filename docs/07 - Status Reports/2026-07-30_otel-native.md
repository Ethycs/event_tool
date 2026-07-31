# Session Report — OTel native: metrics, span status, per-step spans

**Date**: 2026-07-30 · **Status**: ✅ LANDED
**Scope**: `observability/metrics.py` (new) · span status across the package · per-step route spans · roadmap 4.11
**Tests**: 636 passed, 0 failed (was 620) · **Lint**: 43, unchanged
**Follows**: [2026-07-30_routes-and-hints.md](2026-07-30_routes-and-hints.md)

## 1. What "instrumented" was missing

The 2026-07-30 instrumentation pass put a span on every meaningful boundary. Two things it did not do, and both were structural rather than a matter of coverage.

**No span was ever marked failed.** `traced` recorded exceptions and set `error.message`, then left the span's status `UNSET`. Every backend — error rates, alerting, the red highlighting in a trace viewer — keys off `Status`, not off the presence of an exception event. So a trace of a completely broken run rendered entirely green. Recording an exception and marking a span failed are different operations, and only the first was happening.

Worse for this codebase specifically: **most failures here do not raise.** A step that cannot resolve its target, a route that stops early, a token that will not refresh, a TickTick task that fails inside a caught loop — all of them return a value and keep going. Even a correct `traced` would never see them. Hence `fail_current_span(message, error_type=...)`, for marking a span from inside a handled failure.

**There were no metrics at all.** Spans answer *what happened in this run*; metrics answer *what is happening across runs*. Nearly every operational question this project actually has is the second kind — has the meetup route stopped yielding, are we spending more on vision than last week, which step verb fails most. Those numbers existed, on span attributes, where they cannot be summed without a backend query. `llm.total_tokens` is the sharpest example: the spend signal was recorded per call and never aggregable.

## 2. What landed

**`observability/metrics.py`** — same soft-dependency contract as `tracing.py`: no OTel, no problem, every instrument becomes a no-op. `count` / `record` / `add_to_gauge` / `shutdown_metrics`, with instruments created lazily and cached, so a module can call `count(...)` without import-order coupling to `init_metrics`.

The instrument that earns its keep most obviously is `explore.sessions.open`, an **up-down** counter. A plain counter can only say how many browser sessions were opened; the operational question is how many are open *right now*.

**Per-step spans in `replay`.** A route was one flat span, so the only observable fact was that it took nine seconds. Each step is now a child span carrying `step.index`, `step.verb`, `step.target_kind` (role vs. selector — selectors rot first, so that distinction is worth grouping on), `step.ok` and `step.detail`. A route is a waterfall and the slow or broken step names itself.

**Instrumented beyond the web path** — the three that answer real questions:

| Signal | Question it answers |
|---|---|
| `llm.tokens` / `llm.requests` | Spend. Previously unsummable |
| `ticktick.tasks` by outcome | Did the only external write work. Per-task failures are caught and printed inside the loop, so without this a run that failed everything looked like a quiet night |
| `pipeline.stage.duration` | Where the time goes. Added in `_stage`, the one place every stage passes through — the same argument its docstring already made about the reporter and the span |

Full instrument table in `03 - Architecture/04 - Observability.md`.

## 3. Two constraints found by building it

**A MeterProvider cannot be replaced once set.** The SDK logs `Overriding of current MeterProvider is not allowed`, keeps the original, and — because readers bind at provider construction — silently orphans the new readers. `init_metrics` now detects an existing provider and warns instead of pretending it attached. This surfaced as five failing tests whose per-test readers were reading from nothing; the fix was one provider per test module and accumulation-safe assertions.

**Metrics export on a timer, unlike spans.** A CLI that exits in seconds never reaches the first export interval, so the run's metrics are simply lost. `cli/dispatch.py` now calls `shutdown_metrics()` in a `finally`, which forced a small refactor of `main` into `main` + `_run_command`.

## 4. Verified, not assumed

Both signals were checked against in-memory exporters rather than eyeballed on a console:

- A three-step route emits `web.step.goto` / `web.step.dismiss` / `web.step.extract` as children of `web.route.replay`, with `web.verb.dismiss_modal` nested correctly beneath its step.
- A route whose `fill` cannot resolve its target produces `status=ERROR` on **both** `web.step.fill` and `web.route.replay`, carrying `error.type=step_fill` and `route.failed_verb=fill` — with no exception raised anywhere.
- `web.route.yield` records `sum=0` for the failed route and `sum=1` for the good one, which is the raw material for rot detection.
- Running the real `web list` command through the shipped `dispatch.main` produced the `cli.web` root span, the `cli.web.list` child, and a flushed `cli.invocations` metric — which is the part unit tests cannot check, since it proves `init_metrics` is wired and the shutdown flush fires.

## 5. State at handoff

| Fact | Value |
|---|---|
| Tests | 636 passed, 0 failed, ~12s (620 + 16) |
| Lint | 43, unchanged |
| Roadmap | 4.11 closed |
| Git | Branch `fix/tier0-tier1-and-feed-probe`; this is the 6th commit on `ed3a4ee` |

**First action next session**: `pixi run test` (expect 636). The outstanding work is unchanged and still the interesting part — **author a route through the MCP tools and replay it from config**, which remains the one seam nothing has exercised end to end.

## Verification

```powershell
pixi run test tests/test_metrics.py -v          # 16 passed
pixi run test                                    # 636 passed
pixi run event-harvester --debug-otel web list   # spans on the console
```

`unverified` — no OTLP collector was run. The OTLP exporter paths in `init_tracing`/`init_metrics` are guarded imports that fall back with a warning, and the in-memory exporters prove the instrumentation emits, but nothing here confirms the wire format against a real collector.

`unverified` — `llm.tokens` and `ticktick.tasks` are read-verified at their call sites and not exercised: no LLM call and no TickTick write was made in this pass. The `pipeline.stage.*` instruments are likewise unexercised, since no harvest was run.
