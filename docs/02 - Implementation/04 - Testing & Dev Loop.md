# Testing & Dev Loop

**Status:** CURRENT · **Date:** 2026-07-29 · **Applies to:** inner dev loop — tests, lint, and iterating on the web-harvest path without hammering live sites

Assumes Doc 0 (`00 - Environment & Toolchain.md`) is done — `pixi install`, `dev` environment available, `.env` in place.

## 1. Running the suite

```powershell
pixi run test                                   # full suite (pytest -v, via the `test` pixi task)
pixi run test tests/test_nav_quality.py -v      # single file
pixi run test tests/test_config.py::test_all_data_dirs_agree -v   # single test
pixi run test -k "web_fetch" -v                 # substring match across files
```

`pixi run test` resolves to `pytest -v` (`[tool.pixi.feature.dev.tasks] test`) in the `dev` pixi environment; extra args pass straight through to `pytest`. `[tool.pytest.ini_options] testpaths = ["tests"]` and `asyncio_mode = "auto"` are set in `pyproject.toml`, so `async def test_...` functions need no `@pytest.mark.asyncio` decorator.

The suite is organized as one file per module under `src/event_harvester/` (`tests/test_discord.py`, `tests/test_web_fetch_parallel.py`, `tests/test_pipeline.py`, ...). `tests/conftest.py` holds shared fixtures (e.g. `sample_messages` — a small mixed Discord/Telegram fixture used across classifier/analysis tests). None of the tests hit live network/browser paths by default — `tests/test_web_fetch_helpers.py` is explicit about this: it "exercises the data-in / data-out surface" (parsers, cooldown logic, link filtering) without touching Playwright.

`tests/test_data_paths.py` is a regression pin worth knowing about if you ever touch a `_DATA_DIR`/`_REPO_ROOT` computation: this codebase has twice had a path computed with the wrong number of `.parent` calls (once in `pipeline/harvest.py`, once in `pipeline/classifier.py`), silently writing/reading from `src/data` or `src/classifiers` instead of `<repo>/data` or `<repo>/classifiers`. Both are fixed now (`pipeline/harvest.py` derives `_REPO_ROOT` via `Path(__file__).resolve().parents[3]` with a comment explaining the count; `pipeline/classifier.py` walks upward looking for a real `classifiers/` dir instead of counting parents at all). If you add a new module that needs the repo/data root, prefer the classifier module's search-upward approach over hardcoding a `.parent` chain.

## 2. Lint and format

```powershell
pixi run lint                        # ruff check src/ tests/
pixi run format                      # ruff format src/ tests/
```

Equivalently, to target a single file or pass extra ruff flags:

```powershell
pixi run ruff check src/event_harvester/pipeline/harvest.py
pixi run ruff format --diff src/event_harvester/sources/web_fetch.py
```

Config: `[tool.ruff] target-version = "py310"`, `line-length = 100`; `[tool.ruff.lint] select = ["E", "F", "W", "I"]` (pyflakes + pycodestyle + import sort — no complexity/docstring rules enabled).

## 3. Iterating on the web path without hammering live sites

Five tools exist specifically so you don't need a live harvest run against real websites for every iteration:

| Tool | What it does | Cost |
|---|---|---|
| `event-harvester web test URL` | Opens the URL once, runs all detection modes (calendar/feed/search/raw + UC pattern detection), prints a recommendation. Does **not** save anything. | One real page load |
| `event-harvester web add URL [--mode M]` | Same diagnostics, then appends the detected (or forced) config to `data/web_sources.json` | One real page load + a file write |
| `event-harvester web list` | Prints configured sources from `data/web_sources.json` — no browser | Free |
| `--save messages.json` (on `harvest`) | Snapshot the raw harvested messages (all sources, post-fetch, pre-analysis) to a JSON file | One real harvest, then free replays |
| `--load messages.json` (on `harvest`) | Replay from a snapshot instead of harvesting — skips every source fetch entirely (`pipeline/harvest.py` `harvest_messages`: `load_path` short-circuits before the cache check or any source call) | Free |

Typical loop when working on classifier/reranker/LLM-extraction logic (not the web fetch itself):

```powershell
pixi run event-harvester --days 7 --save data/snap.json          # once, live
pixi run event-harvester --load data/snap.json --dry-run --no-sync --show-rejects
```

`--dry-run` shows what TickTick tasks *would* be created without creating them; `--no-sync` skips TickTick entirely (no client construction, no OAuth prompt); `--show-rejects` prints per-stage reject counts and writes the full detail to `data/rejects.ini` (classifier / reranker / caps / LLM-past / LLM-no-events).

There's also an in-pipeline 4-hour message cache (`data/.message_cache.json`, keyed on the active source set and, from 2026-11-24, on the web-source filter) that kicks in automatically on plain `harvest` runs with no `--load`/`--save` — if you're iterating fast and don't see fresh messages, that cache (not staleness of your changes) is often why; the CLI prints `Using cached messages (...). Delete <path> to force refresh.` when it's serving from it. Repeating a run of the *same* single source inside four hours is still served from it, `no_cooldown` or not.

## 4. Web fetch debug flags

Three flags, each implying the ones before it, add observability to the web fetch path (all are shared across `harvest`/`watch`/`serve` in `cli/parser.py`'s `_add_debug_flags`):

| Flag | Env equivalent | Produces |
|---|---|---|
| `--debug-web-trace` | `WEB_DEBUG_TRACE=1` | `data/.web_traces/<source>/<run-id>.jsonl` — per-step navigation events |
| `--debug-web-screenshots` | `WEB_DEBUG_SCREENSHOTS=1` | Implies trace; adds `data/.web_traces/<source>/<run-id>/*.png` + `feed.gif` for scroll feeds |
| `--debug-otel` | n/a (see `OTEL_EXPORTER_OTLP_ENDPOINT`) | OpenTelemetry spans for harvest stages and per-source web fetches, printed to stderr via the console exporter |

```powershell
pixi run event-harvester --days 1 --only web --web-source erobay --debug-web-screenshots --dry-run --no-sync
```

narrows a debug run to exactly one web sub-source (`--web-source NAME`) while getting full trace + screenshot output for it. `--no-cooldown` additionally forces past a source's `cooldown_hours` if you're re-testing the same source repeatedly (per-source cooldown state lives in `data/.web_cooldowns.json`).

## 5. How to add a source (new platform)

Pattern from the four existing platform readers (`sources/discord.py`, `sources/telegram.py`, `sources/gmail.py`, `sources/signal.py`):

1. Add `src/event_harvester/sources/<platform>.py` with a fetch function returning `list[dict]`, each dict shaped like the common message schema: `platform`, `id`, `timestamp` (ISO), `author`, `channel`, `content` (plus any platform-specific extras, e.g. Gmail's `is_read`/`is_sent`, Telegram's `pinned`).
2. Export it from `src/event_harvester/sources/__init__.py` (add to both the `from ... import` block and `__all__`).
3. Add a config dataclass to `src/event_harvester/config.py` if it needs settings (mirror `SignalConfig`/`DiscordConfig` for something auto-detected with an optional override, or `TelegramConfig`/`GmailConfig` for something with real required credentials) — including an `is_configured` property, and wire it into `AppConfig` + `load_config()`.
4. Wire the fetch call into `src/event_harvester/pipeline/harvest.py` `harvest_messages`: add a `no_<platform>` kwarg, an entry in `active_sources`, and a `_run_<platform>()` async wrapper following the existing pattern (OTel span `harvest.<platform>`, `tag_task(...)` context, `asyncio.to_thread(...)` if the reader is sync).
5. Add the platform name to `CapConfig` (per-source cap, default value) and `SourceConfig` (enable flag) in `config.py`, and to the `--only`/`--skip` platform validation in `cli/parse_helpers.py`.
6. Write characterization tests in `tests/test_<platform>.py` mirroring `tests/test_discord.py` — no live credentials needed if the reader takes an `override_path`/injectable client param.

## 6. How to add a web source

Two levels, depending on whether an existing mode fits:

**A — new site, existing mode (the common case, no code):**

```powershell
pixi run event-harvester web test https://example.com/events   # see what mode it recommends
pixi run event-harvester web add  https://example.com/events    # accept it, or:
pixi run event-harvester web add  https://example.com/events --mode calendar   # force a mode
```

This appends an entry to `data/web_sources.json` (the one `data/*.json` file that **is** tracked in git — see the `.gitignore` comment explaining why: it's the harvest config, not message content). Schema, from the existing entries in `sources/web_utils.py`'s `_DEFAULT_SOURCES`:

```json
{"url": "...", "name": "...", "mode": "calendar|feed|search|raw|chat|ics",
 "native_chrome": false, "cooldown_hours": 4, "scroll_seconds": 5,
 "api_pattern": "graphql", "query": "AI meetup", "timeout_s": 45}
```

Set `native_chrome: true` for Cloudflare-gated sites (routes through the serial native-Chrome retry path instead of the parallel `UCBrowser` pool).

**B — new mode (code change):** `src/event_harvester/sources/web_modes/` has one file per mode (`calendar.py`, `feed.py`, `search.py`, `raw.py`, `chat.py`, `ics.py`), each exposing a `_do_<mode>(uc, src, tracer=None, capture=None)` entry point.

1. Add `web_modes/<mode>.py` with `_do_<mode>(...)` following an existing handler's shape (e.g. `raw.py` for the simplest case).
2. Register it in `web_modes/__init__.py` (import + `__all__`).
3. Add the dispatch branch in `sources/web_fetch.py`'s `_process_group` (the `if mode == "chat": ...` / `if mode == "ics": ...` chain — `chat` and `ics` skip the shared `uc.open(...)` + Cloudflare-wait prologue that every other mode goes through, so decide up front whether your mode needs a pre-opened page).
4. Re-export from `web_fetch.py` if other modules will import it directly (back-compat re-export block at the top of that file) — new code should import from `web_modes.<mode>` directly instead.
5. Add a `--mode` choice in `cli/parser.py`'s `web add` subparser if it should be selectable via `web add --mode`.
6. Pin the pure-parsing parts (link extraction, JSON-LD, API-response walking) with characterization tests in `tests/test_web_fetch_helpers.py` style — these test the data-in/data-out helpers without touching Playwright, per that file's own docstring.

## Verification

```powershell
pixi run test
```

Success looks like a summary line with zero failures, e.g.:

```
================= 137 passed in 42.31s =================
```

(Exact count will drift as tests are added — the pass/fail status, not the number, is what matters.)
