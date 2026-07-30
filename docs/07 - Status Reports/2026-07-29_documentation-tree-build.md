# Session Handoff — Documentation tree build

**Date**: 2026-07-29 · **Status**: ✅ LANDED (docs), 🟡 IN FLIGHT (the defects they exposed)
**Scope**: `docs/` (new tree, 24 new docs), `CLAUDE.md`, `TODO.md`, `.gitignore` — **all uncommitted in the working tree**

## 1. What this session did (chronological)

1. Read `Agentic Technique Master.md` (the portable style guide) and built the numbered folder tree under the existing `docs/` root.
2. Relocated seven existing docs into the tree with `git mv` so history follows: the deploy playbook and UC-Grok-proxy notes → `02`, the UC and pixi guides → `04`, the style guide itself → `05`, the browser attempt log and what-worked analysis → `08`, the 33k-line session transcript → `09`.
3. Ran the test suite for a baseline **before** documenting anything: **479 passed, 0 failed, 14.76s**.
4. Dispatched 15 documentation agents in parallel, one per subsystem, each instructed to read its files in full, write in house style, and mark uncertainty rather than smooth it over.
5. Wrote the cross-cutting docs by hand: the tree index, the system overview, the design-rationale doc, the gap analysis, the glossary, the archive index, the drift note, and this handoff.
6. Independently re-verified the highest-impact agent findings against the code (see §3) and corrected three false claims agents made.

## 2. Changes in the working tree

| File / area | Change | Test state |
|---|---|---|
| `docs/` (11 numbered folders) | New tree per the style guide | n/a |
| `docs/03 - Architecture/` (12 docs) | Overview + one as-built doc per subsystem | n/a |
| `docs/05 - Standards/` (3 `EH-STD-*` docs) | Message, event, and web-source record specs — **v0 DRAFT, derived from code, not yet ratified** | n/a |
| `docs/00`, `01`, `02`, `04`, `06`, `07`, `08`, `09`, `000` | Theory, design, setup×3, roadmap, this report, test inventory, archive index, drift note | n/a |
| 7 pre-existing docs | Relocated via `git mv` (renames staged) | n/a |
| `CLAUDE.md` | Rewritten — its `./pixi_guide.md` pointer was already dead; now points into the tree and states the reconcile rule | n/a |
| `TODO.md` | Reduced to a pointer at `06 - Roadmaps` | n/a |
| `.gitignore` | Added `.token-oauth` (see finding 1) | n/a |
| `src/` | **Untouched.** No code changed this session. | 479 passed (unchanged) |

## 3. Findings (the valuable part — verify, then fix)

Full list with tiers in `docs/06 - Roadmaps/00 - Gap Analysis and Open Work.md`. The ones worth acting on first:

1. **`.token-oauth` was not gitignored.** `outputs/ticktick.py:30` constructs `OAuth2(...)` without `cache_path`, so `ticktick-py` defaults to writing a live access token to `.token-oauth` at the repo root. The file does not exist in this checkout yet — the first successful TickTick consent creates it, and `git add -A` would have staged it. **validated** (read the call site, confirmed the absence in `.gitignore`). **Fixed this session** — the ignore rule is the only code-adjacent change made.
2. **`--only` silently caps at 150.** `unlimited_caps=True` → `caps=None` → `analysis.py:296` falls back to `cap_total = 150`, which also becomes the reranker's `top_k`. **validated** by reading `analysis.py:288-320`. The existing test pins that `None` is passed, not what happens next.
3. **`README.md` documents two flags that do not work.** `--no-cap` is absent from `parser.py` entirely; `--cap web=-1` raises `ArgumentTypeError` and exits. **validated** by grep of `parser.py` and reading `parse_helpers.py:18`.
4. **LLM extraction blocks the Flet event loop.** `orchestrator.py:198` calls the synchronous `extract_events_llm` inside `async def run`. **validated** by reading the file. Consequence: the UI freezes for the whole extract stage, and Stop reports "Stopped" while the run is still executing.
5. **`chromium_cache.py:468` references undefined `gzip_event_score` / `gzip_threshold`.** **validated** by grep — the names appear exactly once in the file. Guaranteed `NameError`, swallowed by a bare `except`. The module has no callers at all, so this path has never worked.
6. **`sources/signal.py:23` logs `SIGNAL_DATA_PATH`; the real var is `SIGNAL_DB_PATH`.** **validated** against `config.py:281`.
7. **`tests/test_app.py` does not test `ui/app.py`.** It tests two helpers in `cli/commands/serve.py`; nothing in the suite imports the 1456-line UI module. **validated** by reading the file and grepping the suite.
8. Six further correctness bugs reported by agents but **not independently re-verified** — a cross-thread read of `_parse_event_ini._last_dropped`, approve-marks-approved-on-TickTick-failure, reject buckets read from the wrong backend, the `no-web` profile not disabling web, CWD-relative fingerprint/cache paths, and `RyzenSession._recv()` blocking without a timeout. Each is marked `[R]` in the roadmap. **Verify before fixing.**

Three agent claims were **wrong** and have been corrected in place: that `docs/00 - Theory/` was empty (it globbed before the theory doc landed), that `pixi_guide.md` was missing (it had moved, and `CLAUDE.md` now points at it), and a test count of 412 where the measured collection is 479.

## 4. Open work, in priority order

1. **Decide the direction on `README.md`** (Tier 0.1–0.5 in the roadmap) — five contradictions against the code. Blocked on one choice: keep it as a full onboarding doc and fix it, or cut it to a landing page that links into `docs/`. Everything else in Tier 0 is mechanical.
2. **Fix `--only`'s phantom 150 cap** (Tier 1.1) and add a test that pins the *effect*, not the call. Nothing blocks this.
3. **Move `extract_events_llm` off the event loop** (Tier 1.2) — one-line `to_thread`, but it interacts with the concurrent function-attribute reads in Tier 1.3, so do them together.
4. **Verify the six `[R]` findings** (Tier 1.3–1.11) before touching them. Cheapest first: the `no-web` profile and the Signal log string.
5. **Ratify or downgrade `05 - Standards`** — the three record specs each carry a §4 listing where the code already violates them. Until someone decides per item "fix the code" or "amend the spec", the folder is normative in name only. See Gap 5 in the drift note.
6. **Add the five golden tests** proposed in `08 - Analysis/02 - Test Suite Inventory.md`. 479 green tests did not catch a single Tier 1 bug; that is the gap those tests close.

## 5. Artifacts

| Path | What |
|---|---|
| `docs/README.md` | Tree index, governance, where-to-start table |
| `docs/03 - Architecture/00 - System Overview.md` | The single trusted map — start here |
| `docs/06 - Roadmaps/00 - Gap Analysis and Open Work.md` | Every finding, tiered, marked `[V]` verified or `[R]` reported |
| `docs/000 - Doc Maintenance/2026-07-29_tree-build-drift.md` | Six tree-health gaps + the decision log for this build |
| `docs/08 - Analysis/02 - Test Suite Inventory.md` | What the 479 tests actually pin, and the 27 modules with none |
| `docs/04 - Reference/CLI Command Reference.md` · `MCP Tool Reference.md` | Derived from `parser.py` and the server, not from `README.md` |
| `docs/05 - Standards/EH-STD-{MSG,EVENT,WEBSRC}-v0` | Record schemas, each with a non-conformance section |

**First action for the next session**: run `git status` to confirm the 24 new docs and 7 staged renames are still uncommitted, then run `pixi run event-harvester --no-cap` — it should fail with "unrecognized arguments", confirming finding 3. Then decide item 1 (fix `README.md` vs. reduce it), because Tier 0 cannot be closed without that call.
