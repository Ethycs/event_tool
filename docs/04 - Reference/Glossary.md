# Glossary

**Status:** Living Document · **Date:** 2026-07-29 · **Applies to:** all docs and code in this repo

Terms that mean something specific here and would be guessed wrong from general usage. Format: **is** (the definition) / **means** (the consequence) / **feels like** (how you encounter it).

| Term | Is | Means | Feels like |
|---|---|---|---|
| **actionable** | A message that survived `filter_read_sent` and the classifier gate | It will reach the LLM and cost money | The number in "Filtered: 412 → 38 actionable" |
| **candidate / event candidate** | An event dict extracted by the LLM, not yet approved | It exists in a report or cache but has not touched TickTick | A card in the Flet UI |
| **cap** | A per-source or global ceiling on messages entering LLM extraction | Not a quality filter — a budget guard so one chatty source can't crowd out the rest. `-1` = unlimited, reachable only via env or profile | `--cap discord=20 --cap total=100` |
| **fingerprint** | A content-derived identity for an event (title/date/location/link) | The same event arriving via Discord, Luma, and email collapses to one; approve/decline is remembered across runs | Events tagged `new` vs. already-acted |
| **`_status`** | Underscore-prefixed internal field: `new`, `approved`, `declined` | Underscore-prefixed fields are internal and are stripped from the MCP `public_event` projection | The colored icon on a card |
| **reject bucket** | One of five named lists of what each stage dropped: `classifier`, `reranker`, `caps`, `llm_past`, `llm_no_events` | A missing event is always attributable to a stage. A filter that drops without recording is a bug | The reject panels in the UI, `--show-rejects` |
| **mode** (web) | Which handler runs for a web source: `calendar`, `feed`, `search`, `raw`, `ics`, `chat` | Six, not five — `README.md` is stale. `ics` uses no browser at all | The `"mode"` field in `data/web_sources.json` |
| **profile** | A named bundle of web sub-sources + caps in `data/profiles.json` | Applied via `--profile`; overrides env, is overridden by CLI flags | The UI dropdown |
| **UC / Universal Controller** | The bundled MV3 Chrome extension + Python `UCBrowser` wrapper that drives pages | Detection and actions happen via `page.evaluate()` into the extension, not via Playwright selectors | `ext/universal_controller/`, `sources/uc_browser.py` |
| **native chrome** | Launching the user's real `chrome.exe` over CDP instead of Playwright's Chromium | The Cloudflare-safe path. Always serial, never parallel — concurrency would defeat the "one real user" premise | `"native_chrome": true` on a source |
| **standard source** | A web source that is *not* `native_chrome` | Eligible for the parallel pool (`WEB_PARALLEL`, default 3) | Everything in `web_sources.json` without the flag |
| **cooldown** | A per-source minimum interval between fetches, recorded in `data/.web_cooldowns.json` | A source skipped for cooldown looks identical to one that returned nothing — check the log | `--no-cooldown` |
| **trace / run-id** | One JSONL of nav steps (+ optional PNGs/GIF) per source per run, under `data/.web_traces/<source>/<run-id>` | Written only under `--debug-web-trace`/`--debug-web-screenshots`; never cleaned up | The Traces panel |
| **quality verdict** | A VLM label for a captured run: `content`, `login_wall`, `cloudflare_challenge`, `cookie_banner`, `error`, `empty`, `unknown` | Turns "0 events" into an attributable cause. Produced only by `serve`, never by `harvest`/`watch` | The colored badge in the Traces panel |
| **interlock** | `interlock.py` + `_interlock_worker.py` — a subprocess wrapper around the local NPU LLM backend | **Not** a lock despite the name: same-process singleton reuse only, no lock file, no cross-process exclusion | Only when `backend="local"` |
| **gate** | The cheap regex/keyword pre-filter that runs before the trained classifier | First of five filters; hand-tuned, no learned component | `weights.py:gate_signal` |
| **reranker** | A cross-encoder that re-scores surviving messages before the LLM | Runs *after* caps, so its `top_k` is bounded by the cap | `pipeline/reranker.py` |
| **decompose** | Turning a raw chat transcript into filed docs | Chat is the probe; the doc is the deliverable. A conversation is not evidence until decomposed | `docs/09 - Archived/` → `docs/08 - Analysis/` |
| **reconcile** | Diffing a doc against what was actually built and fixing the gap | A doc is trustworthy only if reconciled since the last substantial change | `docs/000 - Doc Maintenance/` |

## Verification

```powershell
pixi run event-harvester web list
```

Every `mode` value printed should appear in the **mode** row above. One that doesn't means a seventh mode was added and this glossary — plus `EH-STD-WEBSRC-v0` — is stale.
