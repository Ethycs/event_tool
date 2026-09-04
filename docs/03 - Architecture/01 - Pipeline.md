# The Harvest Pipeline

**Status:** CURRENT · **Date:** 2026-07-29 · **Applies to:** `src/event_harvester/pipeline/orchestrator.py`, `harvest.py`, `analysis.py`, `classifier.py`, `reranker.py`, `weights.py`, `event_match.py`, `dedup.py`, `state.py`, `__init__.py`, `src/event_harvester/sources/gmail.py` (`filter_read_sent`) · **Depends on:** none

One `Pipeline` class (`pipeline/orchestrator.py`) runs harvest → filter → extract → match for all three entry points — CLI `harvest`, Flet `serve`, MCP `harvest_events` — collapsing what used to be three independently-drifting ~100-line copies of the same sequence. Inside the `extract` stage, messages pass through a second, finer pipeline — classifier → per-source caps → reranker → LLM — each stage populating one bucket of a five-bucket reject report. Two on-disk stores persist across runs: a 4-hour message cache and a fingerprint store that remembers which events the user has already approved or declined.

## Hard constraints

1. `Pipeline.run()` stage order is fixed: `harvest → filter → extract-links → [skip_analyze short-circuit] → extract(LLM) → match`. `extract_links` runs unconditionally before the `skip_analyze` check; the LLM extraction stage does not.
2. Reject buckets are exactly five keys, always present in `PipelineResult.rejects`: `classifier`, `reranker`, `caps`, `llm_past`, `llm_no_events` (`orchestrator.py:67-74`, `218-226`).
3. Per-source cap value `-1` means unlimited for that source; `caps.total = -1` means unlimited globally; `caps.no_cap = True` bypasses both regardless of the numeric fields (`config.py:172-196`, `analysis.py:169-174`).
4. `find_acted_fingerprint()` only returns a match when the stored status is `"approved"` or `"declined"`; `"auto"`-status fingerprints (web link dedup, CLI TickTick sync) never suppress an event's `"new"` status (`event_match.py:249,267-283`).
5. `events_match()` / `dedup_events()` call two events the same event when score ≥ 2, where title-overlap = 1, date-exact = 1, location-overlap = 1, **link-exact = 2 on its own** (`event_match.py:130-158`).
6. The message cache (`data/.message_cache.json`) is keyed on the *sorted set of active sources*, not on content — changing `--only`/`--skip` between runs invalidates it even with a fresh timestamp (`harvest.py:94-99`).
7. `extract_events_llm(caps=None)` — the path CLI `--only` and MCP `only=` use via `unlimited_caps=True` — is **not** actually unlimited; see BUG below.

## Stage order (`Pipeline.run()`)

| Stage | `update_stage()` id | Input | Output | Adds to message/event dict |
|---|---|---|---|---|
| Harvest | `"harvest"` | `cfg`, `load_path`/cache/live sources | `all_messages: list[dict]` | — |
| Filter | `"filter"` | `all_messages` | `actionable: list[dict]` (Gmail read/sent dropped) | — |
| Links | *(no stage id — runs inline)* | `actionable` | `links: list[dict]` | — |
| skip_analyze gate | — | `cfg.skip_analyze` | early return, `skipped_analysis=True` | — |
| Extract | `"extract"` | `actionable`, `cfg.days_back`, `cfg.llm`, caps | `(summary, events)` | `title/date/time/location/link/notes/source/priority/due_in_days` |
| Match | `"match"` | `events` | same `events`, tagged | `ev["_status"]` = `"new"` \| `"approved"` \| `"declined"` |

A checkpoint (`await reporter.checkpoint()`) fires before each of the four `update_stage()` calls; with no reporter (CLI, MCP) these are no-ops (`orchestrator.py:240-250`). If `harvest_messages()` returns nothing, only the `"harvest"` stage ever fires and `PipelineResult.no_messages=True` short-circuits everything else (`orchestrator.py:158-168`, confirmed by `tests/test_pipeline.py::test_reporter_skipped_for_no_messages`).

## Module map

### `pipeline/orchestrator.py` — `Pipeline` / `PipelineResult`

**Domain:** shared orchestration, the single source of truth for stage order. **Runs at:** async, in the caller's event loop (no threads of its own). **Inputs:** `AppConfig`, optional `_ReporterProtocol`. **Outputs:** `PipelineResult` dataclass (`all_messages`, `actionable`, `events`, `summary`, `rejects`, `source_counts`, `links`, `skipped_analysis`, `no_messages`).

**Internal structure:** `Pipeline.run()` is the entire class's logic — no private helpers beyond the no-op reporter shims and `_status_for()`, a static method that mirrors `mcp_server._event_status()` (duplicated, not shared — see Drift below). `unlimited_caps: bool` swaps `cfg.caps` for `None` before calling `extract_events_llm`. `no_cooldown: bool | None` is tri-state: `None` defers to `cfg.web.no_cooldown`, an explicit bool overrides per-run.

**Hard constraints:** `source_counts` always has all five keys (`discord, telegram, gmail, signal, web`) even when harvest returned nothing, because it's computed from `_ALL_PLATFORMS`, not from whatever platforms happened to appear (`orchestrator.py:64,153-156`).

**Failure modes:** none caught here — `Pipeline.run()` propagates whatever `harvest_messages`/`extract_events_llm` raise; per-source harvest failures are already swallowed one level down in `harvest.py` (see next section).

**BUG — reject collection reads the wrong function's rejects for local backend.** `orchestrator.py:216-226` unconditionally imports `_extract_events_cloud` and reads `getattr(_extract_events_cloud, "_last_llm_dropped", [])` / `_last_llm_no_events` for `rejects["llm_past"]` / `rejects["llm_no_events"]`, regardless of which backend actually ran. When `cfg.llm.backend == "local"`, `_extract_events_local` (`analysis.py:443-493`) runs instead and never touches `_extract_events_cloud`'s attributes — so a local-backend run's `llm_past`/`llm_no_events` rejects are either stale (left over from an earlier cloud call in the same process) or empty, never reflecting what the local backend actually dropped.

### `pipeline/harvest.py` — `harvest_messages()`, `save_messages()`

**Domain:** fan out to the five source readers and cache the result. **Runs at:** async; Discord/Gmail/Signal are sync functions wrapped in `asyncio.to_thread`, Telegram/Web are natively async — all five run concurrently via `asyncio.gather(..., return_exceptions=True)` (`harvest.py:120-298`). **Inputs:** `AppConfig`, per-source `no_*` bools, `load_path`, `web_source`/`web_sources`, `no_cooldown`. **Outputs:** `list[dict]` of messages (each with `platform`, `id`, `timestamp`, `author`, `channel`, `content`, plus platform-specific keys like `is_read`/`is_sent` for Gmail, `pinned`).

**Internal structure:** if `load_path` is set, everything else is skipped — messages are read straight from that JSON file. Otherwise: try the 4-hour cache (`CACHE_FILE`), keyed on `sorted(active_sources)`; on a miss, launch one `asyncio.Task` per enabled source, each independently OpenTelemetry-spanned (`harvest.<name>`), then reassemble results in canonical source order regardless of completion order. A per-source exception is logged and that source's contribution becomes `[]` — it does not abort the run (`harvest.py:303-306`).

**Hard constraints:** `CACHE_FILE = _REPO_ROOT / "data" / ".message_cache.json"`, where `_REPO_ROOT = Path(__file__).resolve().parents[3]` — anchored by counting `.parent` calls, with an extensive in-code comment explaining a prior off-by-one that silently wrote to `<repo>/src/data` instead (`harvest.py:24-42`). Web messages over 2000 chars get `_decimate_text`-shrunk before being written to cache (not before being used in this run) to keep the cache file size bounded (`harvest.py:319-325`).

**Failure modes:** cache-read failures (missing/corrupt JSON, stale timestamp, source-set mismatch) fall through silently to a live re-fetch (`harvest.py:91-110`) — never raises. Cache-write failures are logged at `debug` and swallowed (`harvest.py:336-337`) — a full harvest with an unwritable `data/` directory still returns messages successfully, just uncached.

### `pipeline/analysis.py` — `extract_events_llm()` and the classifier→caps→reranker→LLM chain

**Domain:** turn `actionable` messages into event dicts via a local pre-filter chain plus an LLM call. **Runs at:** sync entry point (`extract_events_llm`), but `_extract_events_cloud` internally parallelizes LLM batches with a `ThreadPoolExecutor(max_workers=5)` (`analysis.py:408-416`); `_extract_events_local` processes batches sequentially. **Inputs:** `actionable: list[dict]`, `days_back: int`, `LLMConfig`, optional `CapConfig`. **Outputs:** `(summary: str, events: list[dict])` — `summary` is always `""` in the current code (never populated by either backend's return path meaningfully; `_extract_events_local` does thread through `batch_summary` but the prompt/parser never actually emits one, see `dead code?` below).

**Internal structure**, in order (`analysis.py:265-350`):
1. If `not cfg.is_configured`: return `("", [])` immediately, log a warning.
2. `cap_total` is computed once: `max(len(messages), 1)` — i.e. no limit — if `caps is None` **or** `caps.no_cap` **or** `caps.total < 0`; otherwise `caps.total`. The three ways a caller says "don't cap this run" all converge here. *(Fixed 2026-07-29; the `caps is None` case used to fall through to a hardcoded `150` — see the resolved BUG note below.)*
3. If `classifier.has_trained_models()`: `filter_actionable()` → `rejects["classifier"]`; then `prioritize_with_caps()` (or a flat sort+slice to `cap_total` if `caps` is falsy) → `rejects["caps"]`; then `rerank_messages(top_k=cap_total)` (falls back to a plain score-sort if `sentence-transformers` isn't installed) → `rejects["reranker"]`.
4. Else (no trained classifiers): skip straight to caps (`prioritize_with_caps` or `prioritize(max_messages=cap_total)`) → `rejects["caps"]`; `rejects["reranker"]` is never set in this branch (the orchestrator's `setdefault` fills it with `[]`).
5. Rejects are **returned** on an `ExtractionResult(summary, events, rejects)`. *Changed 2026-07-30: they used to be stashed on the function object (`extract_events_llm._last_rejects`) and read back by the orchestrator via `getattr`.*
6. **Row-as-event bypass** (`_calendar_rows_to_events`, added 2026-09-03): **before** filtering, structured web-calendar messages are split off and emitted as events directly, never reaching the LLM. A message qualifies when `platform == "web"`, its content has a `date:` line and a title, and it carries its own `url`. Such a row already *is* an event; the LLM adds only cost and the chance to mis-attach its link — and it cannot dedup (that is `event_match`). The bypass parses the `time:` line to 24h, takes `location:`/`url` verbatim, drops past-dated rows and noise titles (RESERVED/TBD/…), and stamps `_source_ids` from the row's own id. Measured 2026-09-03: erobay's 208 rows → 208 events deterministically, vs 88 via the LLM (which was silently dropping ~120) and with correct per-row links. Non-structured web rows (e.g. a recurring calcium row with no `url`) stay in the LLM path, where `_carry_calendar_dates` still tends their date.
7. **Extraction cache** (`pipeline/extraction_cache.py`, added 2026-09-02): the *remaining* (non-bypassed) messages are filtered, then split into cache hits and misses by `xcache.partition()`. Only the misses (`to_extract`) go to the backend; hits contribute their previously-extracted events with no LLM call. After the backend returns, `xcache.record()` stores each miss's outcome and `xcache.save()` persists it. On by default; `EH_EXTRACTION_CACHE=0` restores the old send-everything path. See the module entry below for the safety rule.
8. Dispatch to `_extract_events_local` (backend `"local"`) or `_extract_events_cloud` (everything else) — on `to_extract`, not the full filtered set.
9. Bypass events are folded back in and the union runs through `dedup_events()` once; then `_carry_calendar_dates()` tends any LLM-produced calendar dates.

`_extract_events_cloud`: splits into batches of 10, submits each to the thread pool, calls `chat_completion()` then `_parse_event_ini(raw)`, and for empty-result batches records the whole batch into `llm_no_events`. Returns `(summary, events, llm_rejects)`. Finally runs `dedup_events()` (from `event_match.py`) as a within-run fingerprint-based collapse.

`_extract_events_local`: same return shape. *Changed 2026-07-30 — it previously produced no reject buckets at all, so a local run's `--show-rejects` displayed the cloud function's leftovers.*

`_parse_event_ini()`: parses the LLM's INI-format response and returns `(summary, events, dropped)`, where `dropped` holds events whose parsed date is in the past.

**~~Three function-attribute side channels~~ — removed 2026-07-30.** `extract_events_llm._last_rejects`, `_extract_events_cloud._last_llm_dropped/._last_llm_no_events`, and `_parse_event_ini._last_dropped` were module-level mutable state used to smuggle data past the return type. They broke in two ways:

1. The orchestrator read `_extract_events_cloud`'s attributes **unconditionally**, including on `backend="local"` runs where that function never executed — so a local run reported a previous cloud run's leftovers, or nothing.
2. `_parse_event_ini._last_dropped` was written and then read as two separate statements while up to 5 pool workers called it concurrently, so one batch could read a sibling's dropped list.

Returning the data closed both, and was the prerequisite for moving the whole stage onto a worker thread — see the `to_thread` note in the orchestrator section.

**Hard constraints:** batch size is fixed at 10 messages for both backends (`batch_size: int = 10` default param, not config-driven). `_EVENT_EXTRACTION_PROMPT` and `build_prompt()` escape every literal `%` to `%%` to survive a downstream printf-style formatter in litellm/httpx logging (`analysis.py:82-86`) — an intentional workaround, not a parsing bug.

**Failure modes:** a batch-level exception in `_process_batch_with_raw` is caught, logged with `logger.exception`, and turned into a synthetic `llm_dropped` entry (`{"title": "(batch error)", "reason": str(e)}`) plus the whole batch added to the not-tasks list, but not into `llm_no_events` (`analysis.py:401-406`) — one bad batch does not fail the run, it just silently contributes zero events and one reject-bucket line.

**~~BUG~~ — resolved 2026-07-29: `caps=None` now means unlimited, matching the name `unlimited_caps`.** Kept here because the failure is instructive. When `Pipeline.run(unlimited_caps=True)` passed `caps=None` into `extract_events_llm`, step 2 evaluated `caps.total if caps else 150` — `None` is falsy, so `cap_total` fell back to the **hardcoded literal `150`**, not "no limit," and that value was then used as `max_messages`/`top_k` for `prioritize()`/`rerank_messages()`. So `harvest --only telegram` (or MCP `only="telegram"`) — whose entire stated purpose is "the user has already scoped the run and caps would silently drop their target source" (`cli/commands/harvest.py:69-73`) — silently dropped anything past the top 150 by score before the LLM saw it. `caps.total = -1` and `caps.no_cap = True` were always handled correctly; only the `caps=None` path was broken, because it was the one spelling of "unlimited" the condition did not enumerate.

The test that should have caught it, `tests/test_pipeline.py::test_unlimited_caps_passes_none_to_extract`, asserted only that `caps is None` *reaches* `extract_events_llm`. The regression test added with the fix, `tests/test_analysis.py::TestCapEffect`, counts the messages that actually reach `_extract_events_cloud` instead — 200 in, 200 out for both `caps=None` and an all-`-1` `CapConfig`, with `CapConfig(total=40)` still trimming.

**`dead code?`** — `summary` returned by both `_extract_events_local` and `_extract_events_cloud` is effectively always `""`/unused downstream: `_extract_events_cloud` hardcodes `return "", unique`; `_extract_events_local` threads `batch_summary` from `_parse_event_ini`, but `_parse_event_ini` always returns `""` as its first element (`analysis.py:568`, `_PRIO_MAP`/`PRIORITY_LABEL` constants near the top of the file also appear unused by any code path read here — `unverified`, not traced further outside this subsystem).

### `pipeline/extraction_cache.py` — memoize extraction, keyed by message content

**Domain:** stop re-sending unchanged messages to the LLM every harvest. **Why it exists:** once the Discord collector 10×'d message volume (measured 2026-09-02: **11,093** cached messages, caps off, batch-of-5 at ~10 s each), a harvest wedged for 30+ min in `stage: extract` — not a deadlock, just an uncapped re-extraction of the same messages every run. **Runs at:** sync, inside `extract_events_llm` between filtering and backend dispatch; file I/O once per harvest (`load` at start, `save` at end — not per message).

| | |
|---|---|
| File | `data/.extraction_cache.json` (`CACHE_FILE`) — gitignored under `data/*` |
| Key | `sha256(_VERSION ␟ platform ␟ content)` — an edit is a miss; bumping `_VERSION` invalidates all entries at once when the prompt/schema changes |
| Value | `{"events": [...], "ts": epoch}` — the events attributed to that message, or `[]` for a message proven to yield nothing |
| Toggle | on by default; `EH_EXTRACTION_CACHE` ∈ {`0`,`false`,`off`,`no`} disables |
| Pruned | `save()` drops entries older than 30 days, then caps at 100k oldest-first |

**The safety rule (the whole point):** never suppress an event we are not certain about, because a false negative is an event that silently stops appearing.
- A message is cached as **no-event** only when its *entire batch* was empty (`llm_no_events`) — proof it yielded nothing, not merely that nothing linked to it.
- A message is cached **with events** only for events confidently linked to it (`_source_ids`, from `attach_source_ids`'s single-match rule).
- A message that reached the model in an event-producing batch but linked to none of them, and any message whose batch **errored** (`llm_errors`), is **not** cached — re-extracted next run. Correctness over hit-rate.

**Interaction with dates:** web-calendar events are immune to staleness — `_carry_calendar_dates` re-applies each row's stated date every run over the full filtered set (cached messages included). Chat events the LLM dated *relative to today* are cached with their first-seen date; if identical text recurs on a later day the date can be stale (v1 limitation — bump `_VERSION` if it bites). **Tests:** `tests/test_extraction_cache.py` (19, incl. a two-run wiring test proving the second run sends zero messages to the LLM); the real cache is isolated per-test in `tests/conftest.py`.

### `pipeline/classifier.py` — noise pre-filter

**Domain:** two RandomForest binary classifiers (email-tuned, chat-tuned) that gate messages into actionable/noise before the expensive reranker+LLM stages. **Runs at:** sync, in-process (`sklearn` + `joblib`). **Inputs:** message dicts. **Outputs:** `list[bool]` (`predict`) or a filtered `list[dict]` (`filter_actionable`).

**Internal structure:** `extract_email_features()`/`extract_chat_features()` build hand-engineered feature dicts (length, link count, question marks, unsubscribe/noreply markers for email; mentions, code blocks, pinned for chat). `_find_classifiers_dir()` searches upward from `classifier.py`'s own location for a real `classifiers/` directory rather than counting `.parent` calls — the file's docstring explains this replaced an earlier bug where `.parent.parent.parent` landed on `src/classifiers` instead of `<repo>/classifiers`, so `filter_actionable()` took its "no models" branch and silently passed every message through unfiltered (`classifier.py:19-54`). `EVENT_HARVESTER_CLASSIFIERS_DIR` env var overrides.

**Hard constraints:** `ACTIONABLE_THRESHOLD = 0.1` (not the sklearn default 0.5) — the in-code comment documents a precision/recall sweep on a held-out set justifying this as a deliberate recall-favoring choice (0.1 keeps 686/2347 messages, misses 6/125 true events vs. 0.5's 240/2347 kept, 31/125 missed) (`classifier.py:241-260`). Web-platform messages always pass through unclassified — treated as pre-curated (`classifier.py:359-361`).

**Module-level mutable state:** `_model_cache: dict[str, dict] = {}` (`classifier.py:262`) — loaded `joblib` models are cached in memory forever, keyed by `str(path)`. No invalidation: if a model file on disk is retrained/replaced while the process is still running (e.g. long-lived MCP or Flet server), `predict()` keeps serving the stale in-memory model until process restart. `known rough edge`, not exercised by the tests read here.

**Failure modes:** `has_trained_models() == False` (no `.pkl` files found anywhere upward) → `filter_actionable()` logs a warning and returns every message unfiltered — the noise filter degrades to a no-op rather than raising.

### `pipeline/reranker.py` — cross-encoder + temporal scoring

**Domain:** rank `caps`-filtered messages by combined semantic relevance (cross-encoder) and date proximity, cut to `top_k`. **Runs at:** sync; loads a `sentence_transformers.CrossEncoder` lazily on first use, offline-first. **Inputs:** message list, `top_k`. **Outputs:** filtered/reordered message list.

**Internal structure:** Step 1 (`_resolve_message_dates` per message) drops any message with no future date mention and no event-platform link **before** the cross-encoder runs at all — this is a hard gate, not a scoring input (`reranker.py:174-202`). Step 2 scores survivors with the cross-encoder (batch 64). Step 3 combines `semantic*10 + date_proximity + time_bonus(2) + url_bonus(2) + event_link_bonus(3) + pinned_bonus(2)`, sorts, slices to `top_k`.

**Hard constraints:** model resolution order is local model-store path (`C:\models\ms-marco-MiniLM-L-6-v2` or `/data/models/...`, overridable via `EVENT_HARVESTER_RERANKER_PATH`) → HF cache offline → HF network download as last resort, with `HF_HUB_OFFLINE`/`TRANSFORMERS_OFFLINE` pinned whenever a local/cached copy is used so a stray lookup can't silently go to the network (`reranker.py:36-104`). A candidate directory only counts if it has `config.json` — an empty/partial pull is treated as absent, not as a broken load.

**Module-level mutable state:** `_model = None` global singleton (`reranker.py:33`) — loaded once per process, never reloaded, no thread lock around the lazy-init (`_get_model()` check-then-set is not atomic under concurrent callers — `unverified` whether this is exercised concurrently anywhere in this codebase today, since `extract_events_llm` itself is sync and only the LLM-call stage is threaded).

**Failure modes:** if `sentence-transformers` isn't installed, `analysis.py` catches the `ImportError` one level up and falls back to a plain regex/score sort (`analysis.py:314-319`) — reranker absence degrades gracefully, doesn't crash the pipeline.

### `pipeline/weights.py` — regex/date primitives shared across the chain

**Domain:** no orchestration of its own — a library of regexes (`DATE_RE`, `TIME_RE`, the looser `GATE_DATE_RE`/`GATE_TIME_RE`), `_resolve_date()`, `_event_proximity_score()`, `extract_links()`, `prefilter_events()`. Used by `analysis.py` (prompt hints), `classifier.py` (scheduling-keyword feature), `reranker.py` (date gate), and `orchestrator.py` (`extract_links` stage) directly.

**Internal structure:** `extract_links()` (`weights.py:250-283`) is the one function `orchestrator.py` calls directly — regex-finds URLs in every actionable message, scores by link-type (`_link_type_score`, event platforms score 8, tech/article/social lower, `tenor`/`giphy` score 0 and are dropped) and recency, dedupes by URL keeping the highest score, returns sorted. `prefilter_events()` and `extract_events()` (`weights.py:286-409`) are a separate, older-looking structural pre-filter/scorer over messages, distinct from the LLM extraction path — `unverified` whether any of the three live callers still invoke them (a grep restricted to this subsystem's files did not show `orchestrator.py`/`analysis.py` calling `prefilter_events`; `dead code?` pending a full-repo check outside this subsystem's scope).

**Hard constraints:** `_resolve_date()` promotes year-less dates ("March 25") to next year only if the resolved date is more than 30 days in the past relative to the reference date — a same-week "March 25" stays in the current year (`weights.py:176-181`).

### `pipeline/event_match.py` — fingerprinting, structural matching, the fingerprint store

**Domain:** normalize events into comparable signatures; maintain the cross-run "have I seen this" store. **Runs at:** sync, file I/O on every `load_fingerprints()` call (no in-memory cache — every call re-reads and re-prunes the JSON file). **Inputs:** event dicts. **Outputs:** `bool`/`(bool, int)` match results, or the fingerprint list itself.

**Internal structure:** `_normalize_date()` guards against dateutil's fuzzy parser fabricating a date from stray digits by parsing twice with far-apart default years (1900 vs 2099) and requiring month/day to agree between the two parses — genuine disagreement (defaulted value leaking through) returns `None` instead of a wrong date (`event_match.py:21-71`). `event_signature()` produces `{title_words, date, location_words, link}`; `events_match()` scores field-by-field (rule 5 above). `dedup_events()` reimplements the same scoring with a link-index fast path plus cached signatures for O(n) typical-case within-run dedup (`event_match.py:286-333`).

**Fingerprint store:**

| Property | Value |
|---|---|
| File | `data/.event_fingerprints.json` (`FINGERPRINT_FILE = Path("data/.event_fingerprints.json")`, `event_match.py:16`) |
| Format | JSON list of `{title, date (ISO, normalized), time, location, link, ticktick_id, status, created_at (ISO date)}` |
| `status` values | `"auto"` (pre-fetch dedup hint, e.g. web link dedup or CLI TickTick sync — see Drift), `"approved"`, `"declined"` |
| Written by | `save_fingerprint()` — callers: `outputs/ticktick.py` (status defaults to `"auto"`), `servers/mcp_server.py` `approve_event`/`decline_event` (explicit `"approved"`/`"declined"`), `ui/app.py` Flet approve/decline handlers (explicit) |
| Pruned | on every `load_fingerprints()` read: past-dated entries dropped; undated entries older than 30 days by `created_at` dropped. Pruning is **not persisted** until the next `save_fingerprint()` call — a read-only process (e.g. a report generator) never shrinks the file on disk |
| Read for "seen before" hint | `find_fingerprint()` — matches **any** status, used for pre-fetch web dedup |
| Read for "user acted on this" filter | `find_acted_fingerprint()` — matches only `"approved"`/`"declined"` (rule 4 above) |

**`known rough edge`:** `FINGERPRINT_FILE` and MCP's `DATA_DIR = Path("data")` (`servers/mcp_server.py:32`) are bare relative paths resolved against the process's current working directory — unlike `harvest.py`'s `CACHE_FILE` and `classifier.py`'s `_CLASSIFIERS_DIR`, both of which were hardened (with documented prior bugs as the reason) to anchor off `Path(__file__).resolve()` instead of trusting CWD. A CLI/MCP process launched from a directory other than the repo root would write/read the fingerprint store and MCP's `.mcp_events.json` from the wrong place, silently — the same failure class the other two files' code comments explicitly warn about, left unfixed here.

### `pipeline/dedup.py` — event vs. existing-TickTick-task classification

**Domain:** classify already-extracted events as `create` / `update` / `skip` against the user's live TickTick task list — this is the CLI's pre-sync dedup, distinct from `event_match.dedup_events()`'s within-run collapse. **Runs at:** sync. **Inputs:** `events: list[dict]`, `existing_tasks: list[dict]` (TickTick API shape). **Outputs:** `(to_create, to_update, to_skip)` tuple.

**Internal structure:** first checks `find_acted_fingerprint()` — an approved/declined event is always `to_skip`, before even looking at TickTick. Only for events with no acted fingerprint does it compare against `existing_tasks` via `events_match()` (adapting each TickTick task into a pseudo-event with only `title`/`date` populated — `location`/`link` are always `None` for TickTick tasks, so a TickTick-side match can only ever come from title+date, never a link match). A title/date match with new info not already in the task's content triggers `to_update`; otherwise `to_skip`.

**Hard constraints:** `_merge_action()`'s "new info" check is a case-insensitive substring test of `link`/`location`/`time`/`notes` against the existing task's `content + title` — it is not aware of formatting differences, so a semantically-identical but differently-formatted value (e.g. `"6:00pm"` vs `"18:00"`) reads as new info and forces an `update` (`dedup.py:14-32`) (`unverified` — no test in `tests/test_pipeline.py`/`test_event_match.py` covers this ambiguity; not in the assigned test-file list).

**Failure modes:** none raised internally — every event lands in exactly one of the three buckets.

### `pipeline/state.py` — `PipelineState` / `PipelineStateMachine`

**Domain:** Flet-serve-only lifecycle FSM (`IDLE → RUNNING ⇄ PAUSED → DONE → RUNNING…`); not used by CLI `harvest` or MCP, both of which are one-shot. **Runs at:** sync, pure in-memory, no I/O. **Inputs:** `transition(target)` calls from the Flet UI's button handlers. **Outputs:** raises `IllegalTransitionError` on an illegal move; otherwise mutates `self._state`.

**Internal structure:** `_TRANSITIONS` table (`state.py:37-49`) is the single source of truth; `transition()` treats a self-loop target as a no-op rather than requiring callers to guard; `reset()` bypasses the table entirely to force `IDLE` from any state, for exception/cancellation handlers. `is_active` = `RUNNING or PAUSED`; `is_startable` = `IDLE or DONE`.

**Hard constraints:** `PAUSED → DONE` is a legal transition specifically because the pipeline can finish its last stage in the window between a Pause click and the next checkpoint being honored (`state.py:42-44`) — this is documented as intentional, not a gap in the table.

## Caps semantics (`config.CapConfig`)

| Field | Default | Meaning |
|---|---|---|
| `discord`, `telegram` | 50 each | per-source message cap before merge |
| `gmail`, `signal`, `web` | 30 each | per-source message cap before merge |
| `total` | 150 | global ceiling after per-source caps merge; `-1` = unlimited |
| `no_cap` | `False` | if `True`, bypasses **all** per-source and total trimming — `prioritize_with_caps()` returns everyone, still score-sorted |
| `group_by_source` | `False` | output bucketing flag, not read by anything in this subsystem's files |
| per-source value `-1` | — | unlimited for that one source only (checked individually in `prioritize_with_caps`) |
| `caps.get(platform)` | — | unknown platform name falls back to the **`web`** cap, not `total` (`config.py:188-196`) — deliberate, since `web_fetch` tags messages with sub-source names like `luma`/`eventbrite` that aren't top-level `CapConfig` fields |
| `unlimited_caps=True` (orchestrator) → `caps=None` | — | unlimited (fixed 2026-07-29 — it used to fall back to a hardcoded 150-message cap; see the resolved BUG note in the `analysis.py` section) |

## Reject buckets — where each is populated

| Bucket | Populated in | Condition | Granularity |
|---|---|---|---|
| `classifier` | `analysis.py:304`, inside `extract_events_llm` | only when `classifier.has_trained_models()` is true | message |
| `caps` | `analysis.py:312` / `:336` | always (both classifier-present and classifier-absent branches) | message |
| `reranker` | `analysis.py:322` | only when `classifier.has_trained_models()` is true (reranker only runs on the classifier-filtered branch) | message |
| `llm_past` | `analysis.py:425` → orchestrator reads `_extract_events_cloud._last_llm_dropped` | events whose parsed date resolved to the past, in `_parse_event_ini` | event (partial: title/date/source/reason) |
| `llm_no_events` | `analysis.py:426` → orchestrator reads `_extract_events_cloud._last_llm_no_events` | whole LLM batches that yielded zero parsed events | message (entire batch) |

All five keys are guaranteed present in `PipelineResult.rejects` — the orchestrator `.setdefault()`s the three message-stage keys and always assigns `llm_past`/`llm_no_events` as lists (possibly empty) (`orchestrator.py:217-226`), confirmed by `tests/test_pipeline.py::test_run_full_pipeline_tags_status_and_collects_rejects`, which specifically asserts the detached-dict behavior doesn't leak `llm_past`/`llm_no_events` back into `extract_events_llm._last_rejects` across runs.

## Module-level mutable state and side-channel function attributes

These are process-lifetime traps: they survive across `Pipeline.run()` calls within the same process (long-lived MCP/Flet servers), and several are read via `getattr` from a different module than the one that set them.

| Object | Set in | Read in | Trap |
|---|---|---|---|
| `extract_events_llm._last_rejects` | `analysis.py:344` | `orchestrator.py:217` (`getattr(extract_events_llm, "_last_rejects", {})`) | overwritten every call — fine for a single-threaded sequential pipeline, but there is no isolation between concurrent `Pipeline.run()` calls in the same process |
| `_extract_events_cloud._last_llm_dropped` | `analysis.py:425` | `orchestrator.py:222` | **only set when the cloud backend actually ran** — see BUG above for the local-backend staleness |
| `_extract_events_cloud._last_llm_no_events` | `analysis.py:426` | `orchestrator.py:224` | same trap as above |
| `_parse_event_ini._last_dropped` | `analysis.py:567` | `analysis.py:399`, inside `_process_batch_with_raw`, itself run on up to 5 concurrent `ThreadPoolExecutor` workers | **BUG — data race.** `_process_batch_with_raw` calls `_parse_event_ini(raw)` then, as a *separate* statement, reads `getattr(_parse_event_ini, "_last_dropped", [])`. Since `_parse_event_ini` is one shared function object called from up to 5 threads concurrently (`analysis.py:408`), another thread's call can overwrite `_last_dropped` between this thread's call returning and its `getattr` read — a batch can silently pick up a different batch's dropped-events list, or its own list can be clobbered before being read. `unverified` whether this has produced an observed symptom; it is a real race by inspection, not a guess. |
| `classifier._model_cache: dict` | `classifier.py:262`, `_load_model()` | every `predict()` call | no invalidation — a retrained model on disk is not picked up by a running process until restart |
| `reranker._model` | `reranker.py:33`, `_get_model()` | every `rerank_messages()` call | lazy singleton, check-then-set is not lock-protected; `unverified` whether concurrently exercised anywhere in this codebase |

## On-disk artifacts under `data/`

| Path | Written by | Format | Lifetime |
|---|---|---|---|
| `data/.message_cache.json` | `harvest.py:317-337`, `harvest_messages()` | `{cached_at, sources, messages}` | 4-hour TTL (`CACHE_MAX_AGE`), also invalidated by a source-set change |
| `data/.event_fingerprints.json` | `event_match.py:211-246`, `save_fingerprint()` | JSON list of fingerprint entries (see table above) | pruned on read, persisted on next write; never deleted wholesale by this subsystem |
| `data/rejects.ini` | `cli/commands/harvest.py:296-361`, `_print_rejects()` — **CLI-only** | INI, one `[Reject.N]` section per rejected message/event | overwritten every `harvest --show-rejects` run |
| `data/.mcp_events.json` | `servers/mcp_server.py:33,133-150`, `_save_latest_events()` — **MCP-only** | `{generated_at, version, stats, source_counts, links, events}` | overwritten every `harvest_events` MCP call; read back by `list_latest_events`/`approve_event`/`decline_event` |

(`classifiers/classifier_email.pkl`, `classifier_chat.pkl`, `classifier.pkl` live under repo-root `classifiers/`, not `data/` — models, not data; see `classifier.py:19-60`.)

## Public entry points other modules call

| Symbol | Module | Called by |
|---|---|---|
| `Pipeline`, `PipelineResult` | `pipeline/orchestrator.py` (re-exported via `pipeline/__init__.py`) | `cli/commands/harvest.py`, `cli/commands/serve.py`, `servers/mcp_server.py` |
| `PipelineState`, `PipelineStateMachine`, `IllegalTransitionError` | `pipeline/state.py` (re-exported) | Flet `ui/app.py` (only) |
| `harvest_messages`, `save_messages` | `pipeline/harvest.py` | `orchestrator.py` only within this subsystem; `training/` scripts likely call it directly (`unverified`, outside assigned files) |
| `extract_events_llm` | `pipeline/analysis.py` | `orchestrator.py` |
| `filter_read_sent` | `sources/gmail.py` (re-exported via `sources/__init__.py`) | `orchestrator.py`; also directly by `training/eval_classifier.py` (`unverified` role, outside subsystem) |
| `find_acted_fingerprint`, `find_fingerprint`, `save_fingerprint`, `events_match`, `dedup_events` | `pipeline/event_match.py` | `orchestrator.py`, `analysis.py` (`dedup_events`), `dedup.py`, `outputs/ticktick.py`, `servers/mcp_server.py`, `ui/app.py` |
| `dedup_events_against_ticktick` | `pipeline/dedup.py` | `unverified` within this subsystem's files — no caller found in the assigned file list; likely `outputs/ticktick.py`, outside scope |

## How the three callers diverge

| Concern | CLI `harvest` (`cli/commands/harvest.py`) | Flet `serve` (`cli/commands/serve.py`) | MCP `harvest_events` (`servers/mcp_server.py`) |
|---|---|---|---|
| Reporter | none (`Pipeline(cfg)`, no `reporter=`) | `ProgressReporter` wired to the UI progress panel | none |
| `unlimited_caps` | `True` when `--only` is set | never set (always `False`) | `True` when `only=` is set |
| Post-`match` filtering | none — prints/reports every event with its `_status` | filters to `_status == "new"` before handing to `app.set_events()` | filters to `_status == "new"` **unless** `include_known=True` |
| Extra stage after `Pipeline.run()` | TickTick sync (`create_ticktick_tasks`), Markdown/Obsidian report generation | `quality_check` stage — only when `cfg.web.debug_screenshots` **and** an LLM/vision key is configured; scores the most recent web-trace run per source via `NavQualityScorer` | none |
| Fingerprint status written | `"auto"` (via `outputs/ticktick.py`'s default-arg `save_fingerprint(event, id)` call during sync) — **not** `"approved"**, so CLI-synced events remain `"new"` in a later MCP/Flet run even though a TickTick task already exists for them (cross-caller drift, not a crash) | `"approved"`/`"declined"` explicitly, from the UI's approve/decline buttons | `"approved"`/`"declined"` explicitly, from the `approve_event`/`decline_event` tools |
| Result persistence | none beyond the optional `--report`/`--obsidian` files and `--save` raw-message dump | none (state lives in the running Flet app's memory) | `data/.mcp_events.json`, always overwritten |
| `_status` tagging helper | `orchestrator.Pipeline._status_for()` inside the pipeline run, **also** re-derived independently in `harvest.py`'s own `_print_events()` via `find_acted_fingerprint()` for display | reads `ev["_status"]` as set by the pipeline | `mcp_server._event_status()` — a second, separately-maintained copy of the same three-line logic as `Pipeline._status_for()` (`orchestrator.py:254-262` vs `mcp_server.py:76-82`) |

**`known rough edge`:** `Pipeline._status_for()` and `mcp_server._event_status()` are near-identical duplicated logic (`existing = event.get("_status"); if existing: return it; else find_acted_fingerprint(event)...`), not shared through a common helper despite the MCP server file's own comment acknowledging the mirror ("Fingerprint status (mirrors mcp_server._event_status)"). A future change to one and not the other would silently diverge.

## Verification

```powershell
pixi run pytest tests/test_pipeline.py tests/test_pipeline_state.py tests/test_event_match.py tests/test_analysis.py tests/test_weights.py -v
```

Expect all tests green. In particular:
- `test_pipeline.py::test_run_full_pipeline_tags_status_and_collects_rejects` demonstrates the five-key reject shape and the `_status` tagging described above.
- `test_pipeline.py::test_unlimited_caps_passes_none_to_extract` only proves `caps=None` reaches `extract_events_llm`. The gap it left — the hardcoded-150 fallback — is now covered by `test_analysis.py::TestCapEffect`, which calls `extract_events_llm(..., caps=None)` with 200 qualifying messages and asserts all 200 reach the LLM call. That is the pattern worth copying for the rest of Tier 1: assert the effect, not the argument.
- `test_event_match.py::TestFingerprintStore` exercises the pruning rules but writes to a `tmp_path`-monkeypatched `FINGERPRINT_FILE`, so it does not exercise the CWD-relative-path rough edge noted above.
