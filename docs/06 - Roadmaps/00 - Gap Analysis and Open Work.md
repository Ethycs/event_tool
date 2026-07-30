# Gap Analysis and Open Work

**Status:** Living Document · **Date:** 2026-07-29 · **Supersedes:** `TODO.md`
**Depends on:** every doc in `docs/03 - Architecture/`

The test suite is green (479 passed, 0 failed) and the documentation tree is now complete, which means the interesting number is different: a full as-built read of all 60 modules turned up **11 correctness bugs, 5 documentation-vs-code contradictions in `README.md`, and 4 modules of dead code**, none of which any test would catch. Tier 0 and Tier 1 below are that list. Tier 3 is the pre-existing feature backlog from `TODO.md`, unchanged.

## Legend

`✅ Done` · `🟡 In flight` · `🔲 Open` · **[V]** = verified directly against the code during this pass · **[R]** = reported by a documentation agent, not independently re-verified

## Tier 0 — Quick wins (< 1 day each)

Documentation and messaging defects. Nothing here changes behavior; everything here is currently lying to a reader.

| # | Gap | Where | Depends on | Status |
|---|---|---|---|---|
| 0.1 | `README.md:197,202` documents `--no-cap`; the flag **does not exist** in `parser.py`. `CapConfig.no_cap` is reachable only via profile, MCP param, or the UI checkbox. Decide: add the flag or delete the docs. **[V]** | `README.md`, `cli/parser.py` | — | 🔲 |
| 0.2 | `README.md:197,201` documents `--cap web=-1` for unlimited; `parse_cap_arg` raises `ArgumentTypeError` on negatives, so that exact command exits non-zero. The `-1` sentinel works only via `CAP_WEB=-1` or a profile. **[V]** | `README.md`, `cli/parse_helpers.py:18` | — | 🔲 |
| 0.3 | `README.md:16` and `web_modes/__init__.py`'s own docstring both say "five web modes". There are **six** — `ics` is a full mode with its own dispatch branch and OTel span. **[R]** | `README.md`, `sources/web_modes/__init__.py` | — | 🔲 |
| 0.4 | `README.md:223-264` "Project structure" describes a flat pre-refactor layout (`app.py`, `analysis.py`, … at package root). The real layout is `cli/ pipeline/ sources/ outputs/ observability/ servers/ training/ ui/`. **[V]** | `README.md` | — | 🔲 |
| 0.5 | `README.md:112-124` lists 6 MCP tools; the server registers **20** (6 harvest/review + `chat` + `list_chat_sessions` + `forget_chat_session` + 11 `grok_*`). **[R]** | `README.md` | — | 🔲 |
| 0.6 | `sources/signal.py:23` logs `SIGNAL_DATA_PATH=...` but the var the config actually reads is `SIGNAL_DB_PATH`. Misleads every debugging attempt. **[V]** | `sources/signal.py:23` | — | 🔲 |
| 0.7 | `training/eval_classifier.py:276` prints a `--load-labels` hint for a flag that does not exist; the real one is `classifier train --in-labels FILE`. **[R]** | `training/eval_classifier.py:276` | — | 🔲 |
| 0.8 | `08 - Analysis/00 - Browser Configuration Attempt Log.md` instructs readers to edit `scripts/browser_configs.py` and run `scripts/browser_test.py`. Neither file exists (`data/browser_test_results.jsonl` does — they existed once). **[V]** | that doc, `scripts/` | — | 🔲 |
| 0.9 | `grok_tools.py` docstring says "Exposes 12 tools"; it registers 14 plus a prompt. **[R]** | `ext/universal_controller/.../grok_tools.py` | — | 🔲 |
| 0.10 | `ui/display.py:print_raw_events` tests for truncation against a 200-char slice but prints 120 chars — content between 120 and 200 chars is cut with no ellipsis. **[R]** | `ui/display.py` | — | 🔲 |
| 0.11 | `models/` is an empty tracked directory; the real artifacts are three `.pkl` files in `classifiers/`. Delete it or document why it exists. **[V]** | `models/` | — | 🔲 |
| 0.12 | `.token-oauth` — `ticktick-py`'s OAuth cache defaults to the CWD and `outputs/ticktick.py:30` never sets `cache_path`, so the first TickTick consent drops a live access token at the repo root, un-ignored. **[V]** | `.gitignore` | — | ✅ Done 2026-07-29 |
| 0.13 | Better: pass an explicit `cache_path` under `data/` in `outputs/ticktick.py:30` so the token never lands at the repo root in the first place. The ignore rule in 0.12 is a guard, not a fix. **[V]** | `outputs/ticktick.py:30` | 0.12 | 🔲 |

## Tier 1 — Correctness bugs (1–3 days each)

Ordered by blast radius. Every one of these is silent — the run looks successful.

1. **`--only` silently applies a cap of 150.** `unlimited_caps=True` → `Pipeline` passes `caps=None` → `analysis.py:296` falls back to `cap_total = 150`, which then also becomes the reranker's `top_k`. The flag exists specifically to remove the cap. `tests/test_pipeline.py::test_unlimited_caps_passes_none_to_extract` pins the *call*, not the *effect*. **[V]** — `pipeline/analysis.py:296`, `pipeline/orchestrator.py:197`. *Blocks: any "just harvest this one source exhaustively" workflow.*
2. **LLM extraction blocks the Flet event loop.** `orchestrator.py:198` calls the synchronous `extract_events_llm` inside `async def run` with no `to_thread`. The UI freezes for the whole extraction stage and Pause/Stop cannot be delivered until it returns — which is also why Stop reports "Stopped" while the run is still executing. **[V]** — one-line fix, but it interacts with item 3. *Blocks: 1.3, and honest Stop semantics.*
3. **Cross-thread read of a function attribute.** `analysis.py:399` reads `_parse_event_ini._last_dropped` in a statement *after* the call, while up to 5 `ThreadPoolExecutor` workers call it concurrently — one thread can read another's dropped list. **[R]** — the general fix is Tier 2 item 1. *Depends on: nothing; blocks reject-panel trustworthiness.*
4. **Approve marks approved even when TickTick failed.** Both `_on_approve` and `_on_approve_all` write the `"approved"` fingerprint after an exception is logged, so a failed sync is permanently remembered as done and never re-offered. **[R]** — `ui/app.py`. *Highest user-visible data-loss risk in the repo.*
5. **Reject buckets are read from the wrong backend.** `orchestrator.py:222-226` always reads `_extract_events_cloud`'s stashed rejects, even when `cfg.llm.backend == "local"` ran — local runs get stale or empty `llm_past`/`llm_no_events`. **[R]**
6. **The shipped `no-web` profile does not disable web.** Its `__none__` sentinel is treated as an unmatched filter name, so every source still runs. **[R]** — `profiles.py`, `data/profiles.json`.
7. **CLI-approved events never register as approved.** CLI TickTick sync saves fingerprints with `status="auto"`, which the `_status` lifecycle does not recognize, so the same events come back tagged `new` in later MCP/Flet runs. **[R]** — `outputs/ticktick.py`.
8. **`chromium_cache.py:468` raises `NameError`** on undefined `gzip_event_score` / `gzip_threshold`, swallowed by a bare `except Exception` — so the HTML-extraction path has never worked. The whole module is also uncalled (Tier 2 item 4); fix or delete, don't leave both. **[V]**
9. **CWD-relative data paths.** `event_match.FINGERPRINT_FILE` and `mcp_server.DATA_DIR` are bare `data/` paths. Launch the stdio MCP server from any other directory and it silently uses a different fingerprint store and event cache. `harvest.py` and `classifier.py` were both hardened after exactly this bug; these two were missed. **[R]**
10. **`RyzenSession._recv()` blocks forever.** `interlock.py:242` calls `readline()` with no timeout; a wedged-but-alive local worker hangs `chat_completion(backend="local")` permanently. Only a clean process exit is recovered. Orphaned workers also survive an abnormal parent death — `shutdown_local()` is the sole reaper. **[R]**
11. **`web_modes/feed.py` tags messages with the source name** (`"instagram"`) instead of `"web"`, violating `EH-STD-MSG-v0`. Tolerated only because `CapConfig.get()` happens to degrade gracefully — so per-source caps silently do not apply to feed sources. **[R]**

## Tier 2 — Structural (> 3 days, or needs a decision first)

| # | Gap | Why it matters | Decision needed |
|---|---|---|---|
| 2.1 | Replace side-channel function attributes (`extract_events_llm._last_rejects`, `_extract_events_cloud._last_llm_dropped/_last_llm_no_events`, `_parse_event_ini._last_dropped`) with returned values | Root cause of Tier 1 items 3 and 5; makes two concurrent pipeline runs in one process impossible | No — just work |
| 2.2 | `ui/app.py` (1456 lines) has **zero** test coverage. `tests/test_app.py` is misleadingly named — it tests two helpers in `cli/commands/serve.py`. **[V]** | The largest module in the repo, and the one that mutates TickTick | No |
| 2.3 | Add the 5 golden tests proposed in `08 - Analysis/02 - Test Suite Inventory.md` (fixed messages → fixed event JSON, recruiter-grade corpus, Obsidian snapshot, dedup golden set, end-to-end `Pipeline.run()`) | 479 green tests, and not one of them would have caught any Tier 1 bug | No |
| 2.4 | Dead code: `ui/server.py` (no callers), `sources/chromium_cache.py` (no callers), `web_primitives.py` (only its own tests), parts of `weights.py` and `analysis.py` | Four modules a reader must currently understand to conclude they don't matter | **Yes** — delete, or wire `web_primitives` into the modes as its docstring intends |
| 2.5 | Classifier **fails open**: missing model files ⇒ everything is actionable, log warning only | Chosen so a broken model costs money instead of recall. Two path bugs have already triggered it silently | **Yes** — keep fail-open but make it loud, or fail closed |
| 2.6 | `events_match` is non-transitive; `dedup_events` is greedy first-match, so clustering depends on insertion order | Unverified — no test pins a 3-event chain | **Yes** — pick a clustering rule, then pin it |
| 2.7 | `EventsApp` is a single instance reused across every Flet session; a second browser tab steals rendering from the first (swallowed by a bare `except: pass`) | Silent, and looks like a UI bug to the user | No |
| 2.8 | No cleanup policy for `data/.web_traces/` — JSONL, PNGs, GIFs, `quality.json` accumulate forever; both `close()` methods are explicit no-ops | Unbounded disk growth | No (already in `TODO.md`) |
| 2.9 | `chat_completion` has no API-key-based model fallback (only `vision_completion` does), so an Anthropic-only setup silently sends `gpt-4o-mini` unless `LLM_MODEL` is set | Confusing first-run failure for a valid configuration | No |
| 2.10 | `quality_check` only runs from `serve`; `harvest` and `watch` can write screenshots but never score them | The VLM verdict is the whole point of the screenshots | No |
| 2.11 | Dead/inert config: `WEB_SOURCES_FILE`, `GmailConfig.query`/`max_results`, `AppConfig.dry_run`, `OPENROUTER_API_KEY` are read by nothing or never populated | Config that appears to work and doesn't | No |

## Tier 3 — Feature backlog (unchanged from `TODO.md`)

1. Tighter web targeting per source (per-site selectors, search-bar hints) — partial via UC, still mode-dependent.
2. Auto-remediation from VLM verdicts: `cloudflare_challenge` → native-chrome retry, `login_wall` → trigger `web login`. *(Depends on 2.10.)*
3. Multi-image vision call — batch all source frames into one VLM prompt to cut cost.
4. Cache VLM responses keyed by screenshot hash.
5. Cleanup policy for old `data/.web_traces/*` run directories. *(= 2.8.)*
6. Historical quality trend ("luma at 90 for 7 days → 30 today").

## Done (recent)

~~MCP server with TickTick approve/decline tools~~ · ~~per-URL navigation trace~~ · ~~screenshot capture + scroll-feed GIF~~ · ~~VLM quality scoring~~ · ~~OpenTelemetry tracing~~ · ~~async source fan-out~~ · ~~parallel web fetch~~ · ~~named profiles~~ · ~~bounded screenshot timeouts + circuit breaker~~ · ~~per-source `scroll_seconds`~~ · ~~full documentation tree (2026-07-29)~~

## Verification

```powershell
pixi run test                                   # expect 479 passed
pixi run event-harvester --no-cap               # Tier 0.1: expect "unrecognized arguments"
pixi run event-harvester --cap web=-1           # Tier 0.2: expect SystemExit from parse_cap_arg
```

If the last two commands succeed instead of erroring, items 0.1 and 0.2 have been fixed — strike them and update `README.md` to match whichever direction was chosen. Tier 1 items are not observable from the CLI; each needs the test named in its entry before it can be called done.
