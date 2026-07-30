# Observability

**Status:** CURRENT · **Date:** 2026-07-30 (OTel coverage extended package-wide) · **Applies to:** `src/event_harvester/observability/*`, `src/event_harvester/logging_tags.py`, and the `@traced` call sites throughout the package · **Depends on:** `docs/03 - Architecture/03 - Sources — Web.md`

Three independent, opt-in debug flags instrument the web-fetch path: `--debug-web-trace` records a JSONL log of every page navigation, `--debug-web-screenshots` adds per-step PNGs and scroll-sequence GIFs, and `--debug-otel` emits OpenTelemetry spans. Screenshots imply trace; OTel is orthogonal to both. A fourth, always-on module (`tracing.py`) wraps OTel as a soft dependency so the whole subsystem is a no-op when `opentelemetry` isn't installed, and `logging_tags.py` is unrelated per-task log labeling that lives in this package for lack of a better home.

**Changed 2026-07-30:** OTel spans used to cover the web-fetch path plus a handful of pipeline stages. They now cover every meaningful operation boundary in the package — see [OTel spans in the codebase](#otel-spans-in-the-codebase). Two things that were previously impossible to observe are now on spans: **per-request LLM token counts** and **TickTick task-creation failures**.

## Hard constraints

| Constraint | Value | Enforced in |
|---|---|---|
| Screenshot cap per `ScreenshotCapture` instance | 25 snaps (`DEFAULT_MAX_SNAPS`), then all further `.snap()` calls return `None` | `screenshot_capture.py:38,65-72` |
| Screenshot circuit breaker | 3 consecutive failures (`CONSECUTIVE_FAILURE_LIMIT`) disables the instance for the rest of the run | `screenshot_capture.py:39,81-88` |
| Screenshot per-call timeout | 5000ms (`DEFAULT_TIMEOUT_MS`), overridable via constructor | `screenshot_capture.py:37,46` |
| VLM image budget per pipeline run | `VLM_MAX_IMAGES_PER_RUN` env / `cfg.llm.vision_max_images_per_run`, default **10** | `config.py:49,252-261`, `nav_quality.py:367,374-385` |
| Key frames sent to VLM per source/run | 3 (`pick_key_frames(..., limit=3)` call site) | `nav_quality.py:359` |
| Flag implication chain | `--debug-web-screenshots` ⟹ `--debug-web-trace`; `--debug-otel` implies nothing and nothing implies it | `config.py:308-309` (env), `harvest.py:45-49`, `serve.py:63-67`, `watch.py:18-21`, `web_fetch.py:111-114` (re-applied for direct callers) |
| Chrome-extension URLs | Never traced or screenshotted (filtered in `record`, `record_with_screenshot`, and the `attach()` listeners — defense in depth) | `navigation_trace.py:81-83,178-180,222-236` |

**No cleanup policy exists** for anything under `data/.web_traces/` — JSONL files, PNGs, GIFs, and `quality.json` accumulate indefinitely; `NavigationTracer.close()` and `ScreenshotCapture.close()` are both documented no-ops "reserved for future cleanup" (`navigation_trace.py:205-207`, `screenshot_capture.py:143-146`). unverified whether any external process (cron, CI) prunes this directory — nothing in this package does.

## The three debug flags

| Flag | Env equivalent | Config field | Implies |
|---|---|---|---|
| `--debug-web-trace` | `WEB_DEBUG_TRACE=1` | `cfg.web.debug_trace` | — |
| `--debug-web-screenshots` | `WEB_DEBUG_SCREENSHOTS=1` | `cfg.web.debug_screenshots` | `--debug-web-trace` |
| `--debug-otel` | `OTEL_EXPORTER_OTLP_ENDPOINT` (endpoint implies OTel is wired regardless of the flag) | n/a — passed straight to `init_tracing(debug=...)` | nothing |

Flags are registered once in `_add_debug_flags()` (`src/event_harvester/cli/parser.py:59-72`) and shared by the `harvest`, `watch`, and `serve` subcommands. `env` values are read in `config.load_config()` (`src/event_harvester/config.py:308-309`); CLI flags override the config afterward in each command's entry point (`harvest.py:45-49`, `serve.py:63-67`, `watch.py:18-21`), each re-asserting `debug_screenshots ⟹ debug_trace`. `fetch_web_sources()` re-applies the same implication defensively for callers that construct kwargs directly, e.g. tests (`src/event_harvester/sources/web_fetch.py:111-114`).

`--debug-otel` (or a bare `OTEL_EXPORTER_OTLP_ENDPOINT` env var with no flag) is read once at CLI startup in `dispatch.main()`, **before** any harvest/serve code runs, so root spans are captured (`src/event_harvester/cli/dispatch.py:98-110`):

```python
init_tracing(
    debug=getattr(args, "debug_otel", False),
    otlp_endpoint=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"),
)
```

## Module: `navigation_trace.py`

**Domain:** Pure JSONL event recorder for one `(source, run-id)` Playwright session. No screenshots, no LLM calls — a dependency-free data sink.

**Runs at:** Web-fetch time, inside `fetch_web_sources()`'s per-source loop (`web_fetch.py:135-168,180`), and inside each `web_modes/*.py` mode handler (calendar, feed, search, chat, ics, raw) that receives `tracer=` / `capture=` kwargs.

**Inputs:** `source_name: str`, `run_id: str` (constructed as `strftime("%Y%m%dT%H%M%S") + "_" + secrets.token_hex(2)`, e.g. `20260429T183211_a3f2`), `out_dir` (default `data/.web_traces`), and per-event `kind`/`url`/`parent`/`label`/`image` from Playwright page listeners or mode handlers.

**Outputs:** `data/.web_traces/<source>/<run-id>.jsonl` — one append-only JSONL file per run, touched into existence at construction time even before any record is written (`navigation_trace.py:56-58`).

**JSONL schema** (one JSON object per line):

| Field | Type | Present | Meaning |
|---|---|---|---|
| `ts` | string | always | UTC ISO-8601 with milliseconds, `Z` suffix (`_now_iso()`) |
| `step` | int | always | Monotonic counter, starts at 1, incremented per record (chrome-extension URLs don't consume a step) |
| `kind` | string | always | One of `open` \| `framenavigated` \| `popup` \| `xhr` \| `close` \| `error` \| `feed-gif` (feed-gif is added by `web_modes/feed.py`, not documented in the module docstring but written the same way) |
| `url` | string \| null | always (key present) | Page/frame URL; `null` for `close` |
| `parent` | int \| null | always (key present) | `step` id of the record that triggered this one; `null` for the initial `open` |
| `label` | string | only if not None | Free-form tag, e.g. `"feed-scroll"` (xhr from feed dwell-loop), `"scroll-sequence"` (the assembled GIF row) |
| `image` | string | only if a screenshot/GIF was captured | Path relative to `out_dir`, e.g. `"luma/20260429T183211_a3f2/3.png"` or `.../feed.gif` |

Every successful write also best-effort emits an OTel span event via `current_span_event(kind, {"url", "step", "parent"})` (`navigation_trace.py:141-153`) — additive only, the JSONL row is written regardless of whether OTel is active.

**Internal structure:**
- `record()` — plain JSONL write, filters `chrome-extension://` URLs by returning the current step without incrementing.
- `record_with_screenshot()` — claims a step, calls `capture.snap(page, step)` (duck-typed, only needs `.snap()`), resolves the image path relative to `out_dir`, then writes the row. Falls back to `record()` if `capture is None`; still writes the row (no `image` field) if `snap()` returns `None`.
- `attach(tracer, page, capture=None)` — registers Playwright `framenavigated` / `popup` / `close` listeners on a page. `popup` snaps the **popup page itself**, not the parent. `close` never snaps (page is already gone).
- Writes are open-append-fsync-close per record so a crash mid-run leaves a valid partial JSONL (`navigation_trace.py:117-129`); `fsync` failure is swallowed since the flush already reached the OS buffer.

**Failure modes:** `OSError` on write → logged at WARNING, record silently dropped (no exception propagates). `capture.snap()` raising → logged at WARNING, row still written without `image`. Tracing-module import failure inside `_write_record` → swallowed (`except Exception: pass`), so `navigation_trace.py` stays importable even if `tracing.py` breaks.

## Module: `screenshot_capture.py`

**Domain:** Per-step PNG capture + GIF assembly for feed scroll sequences. Duck-typed against `page` (only `page.screenshot(path=..., timeout=...)` and optionally `page.wait_for_timeout(int)`), so it never imports Playwright directly.

**Runs at:** Constructed per-source when `--debug-web-screenshots` is on (`web_fetch.py:156-167`), writing into `nav_tracer.out_dir / source_name / run_id`. Also driven directly by `web_modes/feed.py`'s dwell loop for periodic GIF frames (every 5.0s, `snap_interval_s = 5.0`, `web_modes/feed.py:90,111`).

**Inputs:** `out_dir: Path` (auto-created), `max_snaps` (default 25), `timeout_ms` (default 5000). `.snap(page, step, settle_ms=200)` — settles 200ms (via `page.wait_for_timeout` if available, else `time.sleep`) before snapping.

**Outputs:**
- `<out_dir>/<step>.png` — one PNG per snapped step.
- `<out_dir>/feed.gif` — assembled via `gif_from_pngs()` from the feed dwell-loop's collected PNGs, at the end of `_do_feed` (`web_modes/feed.py:181-196`); `fps=2` default → 500ms/frame, `loop=0` (infinite).

Given `debug_screenshots=True`, the concrete on-disk layout for one run is:

```
data/.web_traces/<source>/<run-id>.jsonl
data/.web_traces/<source>/<run-id>/1.png
data/.web_traces/<source>/<run-id>/2.png
data/.web_traces/<source>/<run-id>/feed.gif        # feed mode only
data/.web_traces/<source>/<run-id>/quality.json    # only if quality_check ran (serve command)
```

**Internal structure:** `snap()` enforces the cap (`_snap_count >= max_snaps` → disables and returns `None`) and the consecutive-failure circuit breaker (3 failures → disables) before ever raising; both checks short-circuit to `None` on every subsequent call once tripped (`_disabled` flag), so a chatty SPA cannot stack dozens of 30s timeouts. `gif_from_pngs()` lazy-imports Pillow; if missing, logs a WARNING with the exact `pixi add --pypi pillow` remediation and returns `None` — GIF capture degrades gracefully rather than failing the run.

**Failure modes:** any `page.screenshot()` exception → caught, logged at WARNING, counted toward the circuit breaker, returns `None`. GIF encode failure (bad PNG, IO error) → caught, logged at WARNING, returns `None`; the `feed-gif` JSONL row is simply not written (`web_modes/feed.py:181-196` only records if `gif_path is not None`).

## Module: `nav_quality.py`

**Domain:** Pure scoring/classification layer plus the one stateful orchestrator (`NavQualityScorer`) that calls a vision LLM. No `litellm` import at module load — `vision_completion` is a lazy shim rebound at call time (`nav_quality.py:451-453`) so importing this module costs nothing when screenshots/quality-check are off.

**Runs at:** Only from `serve_cmd`'s post-`match` `quality_check` stage (`src/event_harvester/cli/commands/serve.py:105-140`), gated on **both** `cfg.web.debug_screenshots` and an LLM being configured (`cfg.llm.api_key` or `cfg.llm.vision_model` truthy). Not wired into `harvest` or `watch` — **known rough edge**: those commands can produce screenshots but never get a `quality.json` unless the user separately runs `serve`.

**Inputs:** `source: str`, `run_id: str`, `cfg: AppConfig` (needs `cfg.llm`), `traces_root` (default `data/.web_traces`). Reads the trace JSONL at `<traces_root>/<source>/<run_id>.jsonl`; returns `None` immediately if that file doesn't exist.

**Outputs:** `<traces_root>/<source>/<run_id>/quality.json` — a `SourceQuality` dataclass serialized via `json.dumps(asdict(result), indent=2, sort_keys=True)`. Fields: `source`, `run_id`, `classification`, `vlm_explanation`, `structural` (dict), `nav_score` (int), `frames_used` (list of str paths), `vlm_model` (str), `duration_ms` (int).

### Verdict vocabulary

`CLASSIFICATIONS` (`nav_quality.py:50-58`), in declaration/matching order:

| Label | Base score delta | Meaning |
|---|---|---|
| `content` | +50 | Actual events/posts/listings/messages visible |
| `empty` | +0 | Page loaded but no content (empty search, rate-limited feed) |
| `login_wall` | −30 | Sign-in / authentication gate |
| `cloudflare_challenge` | −50 | Bot-check page (Cloudflare, hCaptcha, etc.) |
| `cookie_banner` | −10 | Modal/banner blocking main content |
| `error` | −50 | 404/500/DNS/connection error |
| `unknown` | +0 | None of the above, or a short-circuit (no frames / budget exceeded / LLM error) |

### Scoring formula (`compute_score`, pure, no I/O)

```
score = 50 + base_delta(classification)
           + min(event_links, 30)
           + min(feed_items, 20)
           + (20 if search_hits else 0)
clamped to [0, 100]
```
`event_links` = count of `framenavigated` trace records; `feed_items` = count of `xhr` records labelled `feed-scroll`; `search_hits` = any `framenavigated` URL containing `"search"` (case-insensitive) — all derived by `_structural_signals_from_records()` (`nav_quality.py:267-292`), not by the VLM.

### Key-frame selection (`pick_key_frames`, deterministic, no LLM)

Walks the trace JSONL once and collects, in this priority/order:
1. **Every** `feed-gif` record's image (there's normally at most one).
2. The image of the record immediately preceding any `close` or `error` record ("pre-terminal frame").
3. The **most recent** `framenavigated` record's image (appended last, so it survives trimming).

Then: dedupe preserving first-seen order → drop any path that doesn't exist on disk → if still over `limit` (3 at the call site), **drop oldest first** so the most-recent/most-terminal frames win. Image paths in the JSONL are relative to `data/.web_traces/`; resolved via `trace_jsonl_path.parent.parent / image`.

### VLM model selection

`_resolve_vlm_model_name()` (`nav_quality.py:295-306`) — a **duplicated** copy of `llm._resolve_vision_model`, kept in sync by convention only (comment says "keep in sync", no shared code, no test asserting parity — **BUG: drift risk**, the two functions can silently diverge):
```python
explicit = cfg.llm.vision_model or ""
if explicit:
    return explicit
if cfg.llm.api_key.startswith("sk-ant-"):
    return "claude-sonnet-4-7"
return "gpt-4o-mini"
```
Override via `VLM_MODEL` env → `cfg.llm.vision_model` (`config.py:290`).

### `VLM_MAX_IMAGES_PER_RUN` circuit breaker

`NavQualityScorer._images_sent_this_run` is a **class-level** (process-wide, not per-instance) counter. `reset_counter()` must be called once per pipeline run — `serve.py:120` does this immediately before scoring the batch. Budget check (`nav_quality.py:367,374-385`):
```python
budget = cfg.llm.vision_max_images_per_run  # default 10, env VLM_MAX_IMAGES_PER_RUN
if frames and (sent + len(frames) > budget):
    classification = "unknown"
    explanation = f"image budget exceeded for this run (sent {sent}, limit {budget})"
    # no LLM call
```
Only a **successful** VLM call increments the counter (`cls._images_sent_this_run += len(frames)`, `nav_quality.py:413`) — a failed call doesn't burn budget, so a flaky LLM endpoint can be retried across sources without exhausting the cap. cost-bearing: each `score_source` call that clears the budget makes one real vision-LLM request per source/run with up to 3 images attached.

**Failure modes:** No trace file → `None` (caller skips). No key frames → `unknown` / "no key frames available", no LLM call. Budget exceeded → `unknown` / explicit message, no LLM call. LLM exception → caught broadly, `unknown` / `str(e)`, `nav_score` still computed from structural signals alone — the quality-check stage is designed to never fail the pipeline (`serve.py:131-136` also wraps the call in its own try/except). `quality.json` write failure → logged at WARNING, `SourceQuality` still returned to caller (UI badge just won't appear).

## Module: `tracing.py`

**Domain:** OpenTelemetry soft-dependency shim. If `opentelemetry` isn't importable, `_OTEL_OK = False` and every public function degrades to a no-op (`_NoOpSpan` / `_NoOpTracer`) — the pipeline runs identically either way.

**Runs at:** `init_tracing()` called once at CLI startup (`dispatch.py`, before any handler). `get_tracer(name)` is called at module scope, or implicitly by the `@traced` decorator, in most modules of the package.

**Public API:** `init_tracing`, `get_tracer`, `current_span_event`, and — added 2026-07-30 — **`traced`** and **`set_span_attrs`**.

`traced` is a decorator that runs the wrapped callable inside a span, handling sync, `async def`, and generator functions. It exists because the manual
`tracer.start_as_current_span(...)` / `set_attribute` / `record_exception` block is ~8 lines of identical boilerplate per function, and the codebase had already drifted into two spellings of it. `set_span_attrs(**attrs)` annotates whichever span is current, so a function body can add computed attributes without threading the span object through. `None` values are skipped, so optional fields can be passed unconditionally.

```python
@traced("llm.chat", tracer_name="event_harvester.llm")
def chat_completion(messages, cfg):
    set_span_attrs(**{"llm.model": cfg.model})
```

Three properties `traced` guarantees, each pinned by `tests/test_tracing.py::TestTracedDecorator`:

1. **It returns what the function returns** — including awaiting coroutines rather than handing back the coroutine object.
2. **It never swallows an exception.** The error is recorded on the span as `error.message` plus `record_exception`, then re-raised. Instrumentation must not change control flow.
3. **It preserves signature metadata** (`functools.wraps` plus an intact `inspect.signature`). This is load-bearing rather than cosmetic: FastMCP builds each MCP tool's JSON schema by introspecting the decorated function, so a decorator that collapsed the signature to `(*args, **kwargs)` would silently strip every tool's parameters. Verified live — `scripts/mcp_probe.py` still reports full parameter lists for all six harvest/review tools.

**Inputs:** `init_tracing(debug: bool = False, otlp_endpoint: str | None = None)`. `debug=True` (from `--debug-otel`) attaches a `ConsoleSpanExporter` on a `SimpleSpanProcessor` (prints spans to stderr synchronously). `otlp_endpoint` (from `OTEL_EXPORTER_OTLP_ENDPOINT`) attaches an `OTLPSpanExporter` (gRPC, insecure) on a `BatchSpanProcessor`.

**Outputs:** No files — spans go to stderr (console exporter) and/or an OTLP collector over gRPC. `Resource` is tagged `service.name=event_harvester` (`tracing.py:93`).

**Idempotency:** first call installs the global `TracerProvider`; later calls reuse `trace.get_tracer_provider()` and only attach exporter types not already in `_ATTACHED_EXPORTER_TYPES` (`{"console", "otlp"}`), so calling `init_tracing()` repeatedly (e.g. once per test, or if a future caller re-invokes it) never double-attaches processors.

### OTel spans in the codebase

Coverage was extended on 2026-07-30 from "the web-fetch path and a few pipeline stages" to **every meaningful operation boundary** — a stage, an I/O call, an LLM request, a page interaction, a CLI or MCP entry point.

Deliberately *not* instrumented: per-element helpers called inside tight loops (`score_message`, `_pattern_to_element`, `event_signature`). A span per message would cost more than it explains and would bury the spans that matter. "Instrument everything" means every boundary, not every call.

**Trace root.** `cli.{command}` (`cli/dispatch.py`) wraps the whole invocation. Before it existed, every span a run produced was its own orphan trace — `harvest.discord` and the `llm.chat` calls it caused ended up unrelated in the backend, so you could ask "what happened" but never "what did *this run* do" or "how long did the whole thing take". Attributes: `cli.command`, the relevant sub-command, `cli.verbose`/`cli.quiet`, `cli.exit_code`, and `cli.interrupted` on Ctrl-C.

| Span | Tracer | Notable attributes |
|---|---|---|
| `cli.{command}` (root) | `event_harvester.cli` | `cli.command`, `cli.exit_code`, `cli.interrupted` |
| `pipeline.run` | `…pipeline.orchestrator` | `pipeline.days_back`, `pipeline.unlimited_caps`, `pipeline.outcome` (`ok` / `no_messages` / `skipped_analysis`), `event.count`, `reject.{bucket}` per reject bucket |
| `pipeline.{harvest,filter,extract,match}` | same | per-stage counts; `message.count.{platform}` on harvest, `link.count` on filter, `event.new`/`event.known` on match |
| `harvest` + `harvest.{platform}` | `…pipeline.harvest` | (pre-existing) |
| `source.{discord,telegram,gmail,signal}.*` | `…sources.{name}` | `message.count`; `message.pinned` for Telegram |
| `classifier.filter_actionable` / `classifier.predict` | `…pipeline.classifier` | `classifier.message.in/out/filtered`, `classifier.bypassed`, `classifier.web_passthrough` |
| `rerank.messages` | `…pipeline.reranker` | `rerank.candidates`, `rerank.dropped_past`, `rerank.dropped_no_signal`, `rerank.score.top/cutoff`, `rerank.truncated` |
| `rerank.cross_encoder.predict` | same | `rerank.pair_count` — the GPU work, split out so it is distinguishable from the filtering around it |
| `rerank.model.get` | same | `rerank.model.cached`, `rerank.model.loaded_from` (`local` / `hf_cache` / `huggingface_download`) |
| `llm.chat` | `event_harvester.llm` | `llm.backend`, `llm.model`, `llm.message_count`, `llm.request_chars`, `llm.response_chars` |
| `llm.litellm.completion` | same | **`llm.prompt_tokens`, `llm.completion_tokens`, `llm.total_tokens`**, `llm.finish_reason` |
| `llm.local.generate` | same | `llm.local.restarted`, `llm.local.tokens_generated`, `llm.local.elapsed_s` |
| `llm.vision` | same | `llm.image_count`, `llm.image_sent_count`, `llm.image_missing_count`, `llm.image_bytes`, token counts |
| `extract.batch.{i}`, `match.fingerprints` | `…pipeline.analysis` | (pre-existing) |
| `fingerprint.load` | `…pipeline.event_match` | `fingerprint.loaded/kept/pruned` |
| `fingerprint.save`, `dedup.within_run` | same | `dedup.event.in/out`, `dedup.collapsed` |
| `web.{source}` and `web.{name}.{mode}` | `event_harvester.web_fetch` | (pre-existing) |
| `feedprobe` + `feedprobe.{fetch_page,discover.*,validate}` | `…sources.feed_probe` | see the web-sources doc |
| `web.verb.{name}` (16 L1 verbs) | `event_harvester.web_primitives` | `web.found`, `web.element.selector/label/confidence`, `web.element.low_confidence`, `web.arbitrate.candidates` |
| `mcp.tool.{name}` (6), `mcp.resource.{name}` (2) | `…servers.mcp_server` | — |
| `ticktick.create_tasks` | `…outputs.ticktick` | `ticktick.created/updated/skipped/**failed**`, `ticktick.dry_run` |
| `report.generate`, `obsidian.*` | `…outputs.*` | — |
| `cli.web.{sub}`, `cli.web.probe.{batch,apply}` | `…cli.commands.web` | `probe.url_count`, `probe.hit_count`, `apply.changed_count` |
| `profiles.{load,apply}`, `watch.loop`, `training.*`, `web.login` | various | — |

Two of these exist because the thing they measure was previously *unobservable*, not merely uninstrumented:

- **`llm.litellm.completion` token counts.** litellm logs nothing by default and the pipeline discards the response object, so per-run API cost was not recoverable from anything the app produced.
- **`ticktick.failed`.** `create_ticktick_tasks` catches a per-task exception, prints it, and moves on — the failure never reaches `result`, so no caller could count it. The span attribute (and a new WARNING log line) is now the only way to learn that fewer tasks exist than events were extracted.

**`web.verb.*` is the substrate for crystallised navigation.** Every `find_*` verb funnels through `_arbitrate`, which records how many candidates the detectors surfaced, which was chosen, its confidence, and whether that confidence fell below `CONFIDENCE_THRESHOLD`. A trace of an exploration run is therefore already an ordered, attributed record of the L1 verbs used to reach the content — the raw material a route distiller would consume, and the signal for which step will break first when the page changes.

Every `NavigationTracer.record()`/`record_with_screenshot()` call additionally attaches a **span event** (not a new span) named after the JSONL `kind` (`open`, `framenavigated`, `popup`, `xhr`, `close`, `feed-gif`) to whatever span is current at write time, with attributes `url`, `step`, `parent` (`navigation_trace.py:141-153`, `tracing.py`). This means JSONL trace rows and OTel span events are two independent, redundant recordings of the same navigation events — the JSONL is always written; the span event is best-effort and silently dropped if no span is active or OTel is unavailable.

Every `NavigationTracer.record()`/`record_with_screenshot()` call additionally attaches a **span event** (not a new span) named after the JSONL `kind` (`open`, `framenavigated`, `popup`, `xhr`, `close`, `feed-gif`) to whatever span is current at write time, with attributes `url`, `step`, `parent` (`navigation_trace.py:141-153`, `tracing.py:129-143`). This means JSONL trace rows and OTel span events are two independent, redundant recordings of the same navigation events — the JSONL is always written; the span event is best-effort and silently dropped if no span is active or OTel is unavailable.

## `logging_tags.py` (unrelated, co-located)

**Domain:** Per-task log-line tagging via `contextvars`, so concurrent harvest tasks (Discord, Telegram, ...) can prefix log lines like `[discord] foo bar` in the UI log panel even when sources run in parallel. Not part of the trace/screenshot/OTel feature set — grouped under `observability/` only by theme.

**Runs at:** Any code wrapped in `with tag_task("discord"): ...` (`logging_tags.py:23-30`). `task_tag: ContextVar[str | None]` propagates across `await` boundaries and into `asyncio.to_thread` (context is copied, not shared) automatically per Python's `contextvars` semantics.

**Outputs:** None directly — it's a context variable a log `Formatter`/`Filter` elsewhere would need to read (that consumer was not in scope for this document; unverified where `task_tag.get()` is actually read).

## Failure-mode summary (cross-module)

| Failure | Behavior |
|---|---|
| Trace JSONL write fails (`OSError`) | Logged WARNING, record dropped, pipeline continues |
| Screenshot fails once | Logged WARNING, `None` returned, row written without `image` |
| Screenshot fails 3x consecutively | Capture instance disabled for the rest of the run |
| Screenshot cap (25) reached | Capture instance disabled, further calls return `None` immediately |
| Pillow not installed | GIF capture skipped with an actionable WARNING; no exception |
| VLM call raises | `classification="unknown"`, `nav_score` from structural signals only |
| VLM image budget exceeded | Short-circuits to `unknown` without an LLM call |
| `quality.json` write fails | Logged WARNING, result still returned in-process |
| `opentelemetry` not installed | Entire tracing subsystem becomes a no-op; no exception anywhere |
| chrome-extension:// URL | Filtered before any trace/screenshot write (three separate filter points) |

## Verification

```
pixi run -e dev pytest tests/test_navigation_trace.py tests/test_screenshot_capture.py tests/test_nav_quality.py tests/test_tracing.py -v
```
Proves: JSONL schema/step-numbering/chrome-extension filtering (`test_navigation_trace.py`), screenshot cap + circuit breaker + GIF assembly (`test_screenshot_capture.py`), `compute_score`/`pick_key_frames`/`parse_classification`/budget-guard behavior (`test_nav_quality.py`), and OTel no-op-vs-real idempotent init + span-event capture via `InMemorySpanExporter` (`test_tracing.py`). This document was produced by reading source only — the app was not run, so runtime behavior beyond what these tests assert is marked `unverified` above.
