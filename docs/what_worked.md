# What Worked — Session Analysis

This file is the aggregated output of 6 parallel analysis agents that read disjoint segments of `docs/session_transcript.md` (the 990 KB / 34,389-line transcript of this session). Each agent was asked to identify **only what worked** — confirmed-functional items, passing tests, usable output. Failed attempts and rolled-back code are intentionally excluded; those live in [docs/browser_attempts.md](browser_attempts.md).

Source: 1692 merged conversation turns. Index: `_ingest/manifests/turns.json`.

Segments analyzed:
- Q1: lines 1-8500
- Q2: lines 8501-17000
- Q3a: lines 17001-19800
- Q3b: lines 19801-22600
- Q3c: lines 22601-25500
- Q4: lines 25501-34389

---

## Architecture & Refactors

### CLI subcommand restructure (Q1, Q3a)
- **Pre-dispatch normalization** — `_normalize_argv()` injects `harvest` when no subcommand given, preserving backward compat without polluting argparse with parent= patterns. (`src/event_harvester/cli/dispatch.py`)
- **Parser package split** — 711-line monolith split into `parser.py` (285 lines), `parse_helpers.py` (104 lines), and 6 command modules. (`src/event_harvester/cli/`)
- **Test coverage held** — 63 new parser tests + 123 existing = 186 passing through the refactor. Only the pre-existing date-hardcoded failure remained.

### Display module cleanup (Q1)
- Moved `print_links`, `print_validated_events`, `print_raw_events`, `print_recruiter_grades` out of cli.py into `display.py` next to color constants. All 103 tests passed after the move.

### Harvest module extraction (Q1)
- `harvest.py` created to handle Discord/Telegram/Gmail message fetching with auto-cache (1h TTL). State files moved to `data/.message_cache.json` via `__file__`-relative paths.

### Classifier evaluation extraction (Q1)
- `eval_classifier.py` created with 210+ lines pulled from cli.py. Includes pipeline funnel simulation and per-platform classification reports.

### Code cleanup (Q1)
- Consolidated three duplicate `_SCHEDULING_KEYWORDS` definitions (classifier/weights/analysis) into one source in `weights.py`.
- In-memory `_model_cache` for joblib classifiers — avoids re-loading per `predict()` call.
- `utils.py` with `load_json()`/`save_json()` boilerplate, applied across event_match, harvest, eval_classifier.
- Made private APIs public (`parse_llm_ini`, `prioritize`) and updated callers (label, recruiter_score, eval_classifier, obsidian).
- Link-based index in `dedup_events()` — O(1) exact-URL fast path before fuzzy matching.

---

## ML Pipeline (Q2)

### DOM raster classifier (Stage 1)
- `window.__UC_rasterize()` in `extension-entry.js` — converts element bounding boxes into 32×32×4 feature grids (interactive / text / iframe / overlay channels).
- sklearn MLP (`StandardScaler → PCA(48) → Dense(128,64)`) trained on 554 samples. 43–62% validation accuracy. Weights exported to `weights.json` (3.9 MB) for in-browser inference.

### Code feature classifier (Stage 2)
- sklearn RandomForest on 34 DOM structural features (word count, interactive ratio, tag counts, keywords, position signals). 73% validation accuracy on 518 samples — outperforms raster alone.

### Storybook scraper
- Crawls MUI, Ant Design, Chakra, Bootstrap, Radix, Mantine, HeadlessUI; extracts ~550+ rasterized demos labeled by component type. Exports JSON.

### Vanilla JS inference engine
- `__UC_loadWeights()` + `__UC_classify()` in `extension-entry.js` — pure JS forward pass (Scaler → PCA → Dense) runs <1ms. No TFJS dependency.

### Two-stage pipeline integration
- `dom_classifier.py` blends raster (wide net) + code (precision filter) via 30/70 weighted confidence. Combined model is what's actually used in the UC extension.

---

## Pipeline Behavior Fixes (Q3a, Q3b)

### CLI ergonomics (Q3a)
- `--version`, `-q/--quiet` (mutex with `-v`), `--save`/`--load` for message snapshots.
- `--only` automatically disables per-source caps (`caps=None` to `extract_events_llm`) — caps exist to prevent noisy sources crowding quiet ones, irrelevant when scope is explicit.
- Terminal output sorted by date ascending, TBD events last, grouped by source. Reuses `_normalize_date` from `event_match.py`.

### Watermark removal (Q3b)
- Deleted `_load_watermarks`, `_save_watermarks`, `_update_watermarks`, `filter_seen` from `harvest.py` (~80 lines). The bug: filtered subset was being cached, so subsequent runs worked from impoverished data → 0 events. Fingerprints already cover the correctness concern; cache covers refetch avoidance.

### Serve dedup (Q3b)
- Added `find_fingerprint` filter in `serve.py` after extraction — previously approved/declined events stop reappearing.
- One-line change: `events = [ev for ev in events if not find_fingerprint(ev)]`.

### Web source targeting (Q3b)
- `--web-source NAME` flag threaded through parser → harvest_cmd → harvest_messages → fetch_web_sources. Lets you run just erobay or just luma without editing config.

---

## Erobay / Calcium Calendar (Q3c, Q4)

### Calcium Python parser (Q3c, Q4)
- `_parse_calcium_links()` in `web_fetch.py` extracts events directly from static Calcium CGI HTML using `<noscript>` fallback URLs. **107 events** parsed in seconds with title + date + time + venue inline. No JS DOM traversal needed.
- The HTML contains server-generated URLs that already work; reconstruction was the bug.

### URL encoding fixes (Q3c)
- JS Step 4b: switched `encodeURIComponent()` → `escape()` to preserve `/` in dates. The `Date=2026/4/29` parameter survives, server lookups succeed, "deleted" errors stop.
- Removed Step 4b's `results.length < 3` gate — it always runs now. Erobay's events only exist in `javascript:PopupWindow()` hrefs; earlier steps were picking up nav junk.

### Detail page integration (Q4)
- Calcium links feed through the existing detail-page pipeline (link dedup + parallel fetch). Unified path with luma/sfchamber.

### Cloudflare/navigation race fix (Q4, Q3c)
- `page.wait_for_load_state("load")` + retry loop in `_wait_cloudflare()`. Native Chrome takes longer to stabilize on CDP connect; retries catch transient "page is navigating" errors.

### URL-decode for LLM (Q3c, Q4)
- `unquote()` applied to erobay URLs in the `channel` field before they reach the LLM prompt. Stops litellm's `%`-format string from misinterpreting `%2F` sequences as format specifiers.

---

## Pipeline Visibility (Q4)

### Reject viewer
- `--show-rejects` flag dumps filtered messages per stage (classifier / reranker / caps / LLM) to `data/rejects.ini`. Made hidden filtering visible — debugging dead air in the pipeline became a one-line operation. (`src/event_harvester/cli/commands/harvest.py`, `src/event_harvester/analysis.py`)

### Web classifier bypass (Q4)
- Web messages skip the chat classifier entirely (no trained model for them; pre-curated by source selection + link extraction + dead-page detection already). (`src/event_harvester/classifier.py`)

### Status labels (Q3c, Q4)
- Changed event status `(in TickTick)` → `(known)` — fingerprints save on both sync and decline, the original label was misleading.

---

## UI: Flet Events Browser (Q4)

- Material Design 3 Flet app: event cards, batch approve/decline, filters, dark mode.
- Replaces the old static markdown report + bare HTTP server.
- Files: `src/event_harvester/app.py`, `src/event_harvester/cli/commands/serve.py`.
- Worked because Flet's bundled widget set covers our interaction needs without depending on CDP or extension loading — sidesteps all the browser-launch problems entirely.

---

## Browser Auth (Q4 — partial)

The only **working path** for authenticated web sources after extensive iteration:
- **BC-001 + BC-007** (see [docs/browser_attempts.md](browser_attempts.md)): Playwright `launch_persistent_context` + `channel="chrome"` + `data/.chrome_profile/`. User logs in once via `web login`. Sessions persist for weeks. Cookies exported to `.playwright_state.json`. Fetch-time UCBrowser injects those cookies into the extension-mode Chromium context.
- All other attempts (real profile + CDP, profile copy, rookiepy injection, Chromium with extension via Playwright, etc.) failed — see the attempt log for failure modes.

KeePassXC integration is still in progress (BC-010+).

---

## How this report was generated

1. JSONL session transcript → `docs/session_transcript.md` (Python script, dedup'd, tool-result truncated).
2. Stage 0 of [claude-chat-decompose](https://github.com/Ethycs/claude-chat-decompose) — turn index built (`_ingest/manifests/turns.json`).
3. 6 parallel `Explore` agents, each given a disjoint line range, asked only for "what worked".
4. Outputs aggregated and de-duplicated by hand into the sections above.
