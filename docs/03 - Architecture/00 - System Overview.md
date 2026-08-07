# Event Harvester — System Overview

**Status:** CURRENT · **Date:** 2026-07-29 · **Applies to:** `src/event_harvester/` (all subsystems)
**Supersedes:** the "Project structure" section of `README.md`, which still describes a pre-refactor flat module layout

Five message sources fan out concurrently into one shared pipeline that filters, scores, and LLM-extracts event candidates, then three interchangeable control surfaces (CLI, Flet UI, MCP server) present the same candidates for approval. The pipeline core is a single class — `pipeline/orchestrator.py:Pipeline` — precisely because all three surfaces previously re-implemented it and drifted. Everything below is the as-built map; each subsystem has its own doc in this folder.

## Design axioms (non-negotiable)

1. **One pipeline, three callers.** Harvest → filter → extract → match lives only in `Pipeline`. A control surface may add stages around it (the UI adds `quality_check`; MCP adds an on-disk event cache) but must not re-implement the middle.
2. **Sources are read-only against systems we do not own.** Discord is read from the desktop client's LevelDB cache, not a bot API; Signal from the local SQLCipher DB. The harvester never posts, never joins, never mutates a source.
3. **Every stage is skippable and every drop is recorded.** Rejects are bucketed by the stage that dropped them (`classifier` / `reranker` / `caps` / `llm_past` / `llm_no_events`) so a missing event is always attributable.
4. **The LLM is the last resort, not the first pass.** A regex gate, a trained classifier, a reranker, and per-source caps all run before extraction, because extraction is the only cost-bearing stage.
5. **Data paths resolve from one repo-root anchor.** Counting `.parent` calls has silently broken twice (message cache → `src/data/`, classifier models → wrong dir). See `pipeline/harvest.py:40` for the scar tissue.
6. **A long-lived process must be able to say what code it is running.** `buildinfo.build_info()` on every server surface. Established 2026-08-06 after a service ran four-day-old code undetected: `/healthz` reported events and refresh counts and nothing about itself, so diagnosing it took process enumeration and start-time comparison. Identity is not a nice-to-have on a system with three services and five harvest triggers.

## 1. Architecture

```
 SOURCES (concurrent; asyncio.gather over 5 tasks)
 ┌──────────────────────────────────────────────────────────┐
 │ discord   LevelDB cache      to_thread                   │
 │ telegram  MTProto/Telethon   native async                │
 │ gmail     Gmail API          to_thread                   │
 │ signal    SQLCipher DB       to_thread                   │
 │ web       Playwright × N     to_thread → parallel UC     │
 └───────────────────────────┬──────────────────────────────┘
                             │  list[message dict]  (EH-STD-MSG-v0)
                             ▼
              data/.message_cache.json  ── 4h TTL, keyed on the active source set
                             │
 PIPELINE  (pipeline/orchestrator.py:Pipeline)
 ┌───────────────────────────▼──────────────────────────────┐
 │ harvest → filter_read_sent → extract_links               │
 │        → [ gate → classifier → reranker → caps ]         │
 │        → extract_events_llm  ← the only cost-bearing step│
 │        → match (fingerprint → _status)                   │
 └───────────────────────────┬──────────────────────────────┘
                             │  list[event dict] (EH-STD-EVENT-v0) + rejects{5 buckets}
        ┌────────────────────┼────────────────────┐
        ▼                    ▼                    ▼
   CLI harvest          Flet UI (serve)      MCP server
   print + report       control + review     tools + resources
        │                    │                    │
        └────────────────────┴────────────────────┘
                             ▼
              SINKS: TickTick · Markdown report · Obsidian vault
                             │
              fingerprint store ── approve/decline are remembered
                                    so the next run tags them, not re-offers them

 CROSS-CUTTING
   observability/  nav traces · screenshots · VLM quality · OTel spans
   interlock.py    RyzenAI NPU subprocess for local ONNX inference
   config.py       AppConfig ← CLI flags ← profile ← env ← defaults
```

## 2. Subsystems

| # | Subsystem | Code | Doc |
|---|---|---|---|
| 1 | Pipeline | `pipeline/` | `01 - Pipeline.md` |
| 2 | Messaging sources | `sources/{discord,telegram,gmail,signal,chromium_cache}.py` | `02 - Sources — Messaging.md` |
| 3 | Web sources | `sources/web_*.py`, `sources/web_modes/`, `web_primitives.py` | `03 - Sources — Web.md` |
| 4 | Observability | `observability/` | `04 - Observability.md` |
| 5 | Outputs | `outputs/`, `ui/display.py` | `05 - Outputs.md` |
| 6 | CLI | `cli/` | `06 - CLI.md` |
| 7 | Flet UI | `ui/` | `07 - Flet UI.md` |
| 8 | MCP server | `servers/`, `gateway.py` | `08 - MCP Server.md` |
| 9 | Configuration | `config.py`, `profiles.py` | `09 - Configuration & Profiles.md` |
| 10 | LLM layer & interlock | `llm.py`, `interlock.py`, `_interlock_worker.py` | `10 - LLM Layer & Interlock.md` |
| 11 | Training & evaluation | `training/` | `11 - Training & Evaluation.md` |

The scoring model that stages 3–6 of the pipeline implement is derived in `docs/00 - Theory/00 - Ranking, Gating and Event Identity.md`. The record shapes that cross every boundary above are specified in `docs/05 - Standards/`.

## 3. Concurrency model

| Layer | Mechanism | Constraint |
|---|---|---|
| Source fan-out | `asyncio.gather(..., return_exceptions=True)` over ≤5 tasks | A failing source logs and yields `[]`; it never aborts the run (`pipeline/harvest.py:298`) |
| Sync sources | `asyncio.to_thread` | Discord, Gmail, Signal, and the whole web path are blocking underneath |
| Web fetch | `WebConfig.parallel` UCBrowser instances, default 3 | Native-chrome sources stay serial — Cloudflare safety |
| Local LLM (NPU) | `interlock.py` + `_interlock_worker.py` subprocess | A `ryzenai-python` subprocess running ONNX/OGA inference; one long-lived `RyzenSession` per process, created lazily by `llm.py:35`. **Not** a browser lock — corrected 2026-07-31, this row previously read "one browser-driven session at a time, cross-process" |
| Harvest execution | `jobs.py` + `job_runner.py` | **One runner, serial.** The MCP backend (streamable-http only) owns the queue; the site, the CLI and MCP tools all *submit*. The claim is an `os.rename` from `queued/` to `running/`, so it is a mutex even if a second runner ever appears. Added 2026-08-01 — see `01 - Design/03 - The Job Queue.md` |
| Browser sessions | the job queue, indirectly | `known rough edge` — there is still **no lock** on browsers. Native Chrome takes a fixed `--remote-debugging-port=9222` (`uc_browser/browser.py:614`). The queue removes the case that actually happened (site and backend both harvesting), but a CLI `harvest` or a stdio MCP client runs in its own process and can still overlap. `data/.harvest.lock` is the recorded fix |
| UI | Flet event loop + cooperative `checkpoint()` | Pause/stop take effect only at stage boundaries |

Message order is deterministic regardless of completion order: results are re-assembled in the canonical source order `discord, telegram, gmail, signal, web` (`pipeline/harvest.py:311`).

## 4. State on disk

| Path | Written by | Lifetime |
|---|---|---|
| `data/.message_cache.json` | `pipeline/harvest.py` | 4h TTL; invalidated when the active source set changes |
| `data/.web_traces/<source>/<run-id>*` | `observability/` | **No cleanup policy** — grows unbounded (`TODO.md`) |
| `data/web_sources.json` | hand-edited + `web add` | Permanent config |
| `data/profiles.json` | hand-edited | Permanent config |
| fingerprint store | `pipeline/event_match.py` | Prunes at 30 days |
| `classifiers/`, `models/` | `training/` | Replaced on retrain |
| `harvest_session.session`, `token.json` (repo root) | Telegram / Gmail auth | Until revoked — **secrets, never commit** |

## 5. Known structural hazards

1. **Side-channel function attributes.** `extract_events_llm._last_rejects` and `_extract_events_cloud._last_llm_dropped` carry results out of band; `orchestrator.py:216` reads them right after the call. Any concurrent second pipeline run in one process would interleave them. `known rough edge`
2. **Fail-open classifier.** If the model files cannot be found, filtering is disabled and everything is deemed actionable, with only a log warning. `pipeline/classifier.py`
3. **Path anchoring by `.parent` counting** has produced two silent bugs already; both are fixed, the pattern is not.
4. **`events_match` is not transitive** and `dedup_events` is greedy first-match, so cluster deduplication depends on insertion order. `unverified` — no test pins a 3-event chain.

## Verification

```powershell
pixi run test                                   # 1018 passed, 0 failed as of 2026-08-07
pixi run event-harvester service doctor         # what runs, at what version, what is stray
pixi run event-harvester jobs list              # the harvest queue: what ran, what waits
pixi run event-harvester --days 1 --dry-run     # exercise the full pipeline, write nothing external
pixi run event-harvester site --build-only      # write data/.site/index.html and stop
```

If the second command prints a stage sequence other than harvest → filter → extract → match, this overview is stale and the pipeline was restructured — reconcile against `01 - Pipeline.md` before trusting anything here.
