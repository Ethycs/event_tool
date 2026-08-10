# Event Harvester

**Status:** CURRENT · **Date:** 2026-07-29 · **Applies to:** the repo as a whole; flag and tool tables are summarized here and authoritative in [docs/04 - Reference/](docs/04%20-%20Reference/)

Pulls messages from **Discord, Telegram, Gmail, Signal, and the open web**, extracts event candidates with an LLM, and surfaces them through three review interfaces: a CLI report, an interactive Flet web app, and an MCP server for AI clients.

This file is the landing page. It covers install, first run, and the shape of the system. It deliberately does **not** restate the full flag list or tool list — those drifted from the code once already, so they now live in one derived place each:

| You want | Read |
|---|---|
| every CLI flag, with defaults and gotchas | [docs/04 - Reference/CLI Command Reference.md](docs/04%20-%20Reference/CLI%20Command%20Reference.md) |
| every MCP tool, with parameters | [docs/04 - Reference/MCP Tool Reference.md](docs/04%20-%20Reference/MCP%20Tool%20Reference.md) |
| how the system fits together | [docs/03 - Architecture/00 - System Overview.md](docs/03%20-%20Architecture/00%20-%20System%20Overview.md) |
| why it has this shape | [docs/01 - Design/00 - Why The System Has This Shape.md](docs/01%20-%20Design/00%20-%20Why%20The%20System%20Has%20This%20Shape.md) |
| what's broken / open work | [docs/06 - Roadmaps/00 - Gap Analysis and Open Work.md](docs/06%20-%20Roadmaps/00%20-%20Gap%20Analysis%20and%20Open%20Work.md) |
| the whole doc tree | [docs/README.md](docs/README.md) |

## Pipeline

```
Discord LevelDB cache ─┐
Telegram MTProto ──────┤
Gmail API ─────────────┼─► classifier ─► reranker ─► LLM extract ─► match/dedup ─┬─► CLI report
Signal SQLCipher ──────┤                                                          ├─► Flet web UI
Web (Playwright × N) ──┘                                                          ├─► MCP tools
                                                                                  └─► TickTick sync
```

All three control surfaces call the same `Pipeline` class (`src/event_harvester/pipeline/orchestrator.py`); none of them re-implement stages.

Web modes — `calendar`, `feed`, `search`, `raw`, `ics`, `chat`, `steps` — run in parallel browser instances, each per-source-tunable via `data/web_sources.json`. `ics` skips the browser entirely and parses the feed directly. Native-chrome mode is used for Cloudflare-gated sources and stays serial.

**Adding a venue** — three routes, cheapest first:

1. **`web add URL`** probes for a published iCalendar feed *before* launching a browser — most venue sites run a platform (WordPress + The Events Calendar, Tockify, Squarespace, an embedded Google Calendar) that already exports one at a predictable URL. A hit becomes an `ics` source: no browser, no Cloudflare, no scraping. `web probe` runs the same check across every source you already have.
2. **No feed, simple page** — `web add` falls back to browser diagnostics and picks one of the hardcoded modes.
3. **No feed, and the events are behind interaction** (Meetup, Eventbrite, Luma) — walk it once with the `web_explore_*` MCP tools and save the walk as a `steps` route. Replay after that is deterministic and never calls an LLM. `web replay NAME` shows each step; `web_diagnose URL` says what is blocking a page that returns nothing.

## Setup

This project is managed with [pixi](https://pixi.sh). See [docs/04 - Reference/Pixi CLI Reference.md](docs/04%20-%20Reference/Pixi%20CLI%20Reference.md) for env details — never invoke bare `python`.

```powershell
# Install everything (conda + PyPI) into .pixi/envs/
pixi install

# First-run credentials
cp .env.example .env
# Edit .env with your values
```

| Service       | Env vars                                                                                  | Where to get them                                       |
|---------------|-------------------------------------------------------------------------------------------|---------------------------------------------------------|
| Telegram      | `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_PHONE`                                  | [my.telegram.org](https://my.telegram.org) → API Tools  |
| OpenAI        | `OPENAI_API_KEY`                                                                          | [platform.openai.com](https://platform.openai.com/api-keys) |
| Anthropic     | `ANTHROPIC_API_KEY` (fallback if `OPENAI_API_KEY` unset)                                  | [console.anthropic.com](https://console.anthropic.com)  |
| TickTick      | `TICKTICK_CLIENT_ID`, `TICKTICK_CLIENT_SECRET`, `TICKTICK_USERNAME`, `TICKTICK_PASSWORD`  | [developer.ticktick.com](https://developer.ticktick.com) |

On first run, Telegram prompts for phone OTP once and TickTick opens a browser for OAuth consent once; both cache thereafter. The TickTick consent writes a live token to `.token-oauth` at the repo root — it is gitignored, keep it that way.

Full credential walkthrough: [docs/02 - Implementation/01 - Credentials & First Run.md](docs/02%20-%20Implementation/01%20-%20Credentials%20&%20First%20Run.md).

## Three ways to use it

### 1. Flet web UI (primary control surface)

```powershell
pixi run event-harvester serve
```

Opens a control panel at <http://127.0.0.1:8550> that:

- Drives the whole pipeline (Start / Stop / Pause / Resume / Refresh)
- Lets you tweak caps, sources, days, and behavior flags live before each run
- Selects [named profiles](#profiles) from a dropdown
- Renders extracted events as cards with batch approve/decline
- Shows reject panels per stage (classifier / reranker / caps / LLM) for debugging
- (When `--debug-web-screenshots` is set) shows a Traces panel with per-source nav steps, inline screenshots, GIFs, and VLM quality badges

`known rough edge`: LLM extraction runs synchronously on the UI's event loop, so the panel freezes for the duration of the extract stage and Stop reports "Stopped" while the run is still going. Tracked as Tier 1.2 in the roadmap.

### 2. CLI (one-shot batch + cron)

```powershell
# Bare invocation = harvest subcommand
pixi run event-harvester              # last 7 days, all sources
pixi run event-harvester --days 14
pixi run event-harvester --only discord,telegram
pixi run event-harvester --skip web,signal
pixi run event-harvester --dry-run             # preview without TickTick sync
pixi run event-harvester --no-analyze          # skip LLM extraction
pixi run event-harvester --save messages.json  # snapshot raw messages
pixi run event-harvester --load messages.json  # replay from snapshot
pixi run event-harvester --cap discord=20,telegram=30 --cap total=100
pixi run event-harvester --show-rejects        # per-stage reject counts + data/rejects.ini
pixi run event-harvester --report              # events_report.html — one file,
                                               # mobile-friendly, no server, forwardable
pixi run event-harvester site                  # serve it at http://127.0.0.1:8800
                                               # and re-harvest every 24h
pixi run event-harvester service install       # ...as a Windows service (NSSM, elevated)
```

Subcommands:

```powershell
pixi run event-harvester watch                 # poll Discord + Telegram every 30s
pixi run event-harvester web list              # show web sources
pixi run event-harvester web probe             # find iCalendar feeds, no browser
pixi run event-harvester web probe --apply     # ...and switch those sources to ics
pixi run event-harvester web test URL          # diagnostics
pixi run event-harvester web add  URL          # diagnostics + save
pixi run event-harvester web login             # browser login for session cookies
pixi run event-harvester recruiters grade
pixi run event-harvester recruiters reparse REPORT.md
pixi run event-harvester classifier train --out-labels labels.json
pixi run event-harvester classifier eval  --labels labels.json --out-samples samples/
pixi run event-harvester serve                 # Flet UI (above)
pixi run event-harvester mcp                   # MCP server (below)
```

Roughly twenty more flags exist that are not shown above — see the [CLI Command Reference](docs/04%20-%20Reference/CLI%20Command%20Reference.md) for the complete tables.

### 3. MCP server

Run as a [Model Context Protocol](https://modelcontextprotocol.io) server over stdio (default) or streamable HTTP:

```powershell
pixi run event-harvester mcp
pixi run event-harvester mcp --transport streamable-http --port 8765
```

Project-local registration for Claude Code lives in [.mcp.json](.mcp.json):

```json
{
  "mcpServers": {
    "event-harvester": {
      "type": "stdio",
      "command": "pixi",
      "args": ["run", "event-harvester", "mcp"]
    }
  }
}
```

Twenty-nine tools are registered, in three groups:

| Group | Tools |
|---|---|
| Harvest / review (6) | `config_summary`, `harvest_events`, `list_latest_events`, `approve_event`, `decline_event`, `generate_latest_report` |
| Site authoring (9) | `web_explore_open`, `web_explore_look`, `web_explore_act`, `web_explore_undo`, `web_explore_save`, `web_explore_close`, `web_explore_list`, `web_diagnose`, `web_validate_steps` |
| Chat / Grok (14) | `chat`, `grok_send_message`, `grok_read_conversation`, `grok_list_conversations`, `grok_delete_conversation`, `grok_new_chat`, `grok_rename_conversation`, `grok_archive_conversation`, `grok_regenerate`, `grok_stop_generation`, `grok_get_models`, `grok_switch_model`, `list_chat_sessions`, `forget_chat_session` |

The third group comes from `uc_browser.mcp.grok_tools` and is registered inside a `try`/`except`. If that import fails the server still starts, logs a warning, and exposes only the first fifteen — so a client seeing 15 tools instead of 29 means the `uc_browser` submodule is missing, not that the server is broken.

Parameters and return shapes for each: [MCP Tool Reference](docs/04%20-%20Reference/MCP%20Tool%20Reference.md).

Resources: `event-harvester://config`, `event-harvester://latest-events`.

A stdio probe lives at [scripts/mcp_probe.py](scripts/mcp_probe.py):

```powershell
pixi run python scripts/mcp_probe.py                   # list tools/resources
pixi run python scripts/mcp_probe.py call config_summary
```

To run it as a long-lived service behind the FastMCP gateway, see [docs/02 - Implementation/02 - Deploy Playbook.md](docs/02%20-%20Implementation/02%20-%20Deploy%20Playbook.md):

```powershell
pixi run eh-service    # backend on :8765
pixi run gateway       # gateway on :9000/mcp
```

## Profiles

Named bundles of web sub-sources + caps live in `data/profiles.json`:

```json
{
  "profiles": [
    {
      "name": "tech-meetups",
      "description": "Luma + Eventbrite + Meetup, relaxed web cap",
      "web_sources": ["luma", "eventbrite", "meetup"],
      "caps": {"web": 60, "total": 120}
    }
  ]
}
```

Apply via `--profile tech-meetups` on `harvest`, `serve`, or `watch`. The UI dropdown re-applies live.

## Web debugging features

Three independent flags add observability to the web fetch path. Each implies any flag earlier in this list (so `--debug-web-screenshots` also turns on traces).

| Flag                         | Produces                                                                                  |
|------------------------------|-------------------------------------------------------------------------------------------|
| `--debug-web-trace`          | `data/.web_traces/<source>/<run-id>.jsonl` — per-step navigation events                   |
| `--debug-web-screenshots`    | `data/.web_traces/<source>/<run-id>/*.png` + `feed.gif` for scroll feeds                  |
| `--debug-otel`               | OpenTelemetry spans for harvest stages and per-source web fetches (console or OTLP)       |

When screenshots are enabled and an LLM is configured, a `quality_check` stage runs after `match` and asks a vision LLM to classify each captured run as `content` / `login_wall` / `cloudflare_challenge` / `cookie_banner` / `error` / `empty` / `unknown`, producing `quality.json` next to the screenshots and a colored badge in the Flet Traces panel.

Tunables:

- `WEB_DEBUG_TRACE`, `WEB_DEBUG_SCREENSHOTS` — env equivalents of the CLI flags.
- `VLM_MODEL` — override the vision model (default: `claude-sonnet-4-7` if Anthropic key, else `gpt-4o-mini`).
- `VLM_MAX_IMAGES_PER_RUN` — image-budget circuit breaker (default `10`).
- `OTEL_EXPORTER_OTLP_ENDPOINT` — point spans at any OTLP collector. Note this exports whenever it is set, with or without `--debug-otel`.

Details: [docs/03 - Architecture/04 - Observability.md](docs/03%20-%20Architecture/04%20-%20Observability.md).

## Per-source scroll time

Each entry in `data/web_sources.json` has a `scroll_seconds` field that controls how long the page is scrolled before extraction (or in the case of `feed` mode, the dwell loop length):

```json
[
  {"url": "https://lu.ma/discover", "name": "luma", "mode": "calendar", "scroll_seconds": 5},
  {"url": "https://www.eventbrite.com/d/ca--san-francisco/events/", "name": "eventbrite", "mode": "feed", "scroll_seconds": 15, "api_pattern": "api|search|events"}
]
```

Default if omitted: 5 seconds. The full schema for a web-source entry is [EH-STD-WEBSRC-v0](docs/05%20-%20Standards/EH-STD-WEBSRC-v0%20—%20Web%20Source%20&%20Profile%20Config.md).

## Instagram

`mode: "instagram"` asks named accounts for their posts over Instagram's own
GraphQL, instead of scrolling the ranked home feed and hoping. Requires a
logged-in session (`pixi run event-harvester web login`).

The account list is read off the venues rather than guessed — venues link
their own Instagram from their own site:

```powershell
pixi run event-harvester web instagram --category venues          # report
pixi run event-harvester web instagram --category venues --apply  # adopt
```

Only *confident* matches are adopted (the handle shares ≥5 characters with
the venue's name or domain), because a wrong handle harvests a stranger's
posts forever and never errors. Measured 2026-08-06: 17 of 23 venue sites
name their account clearly enough. Details:
[docs/03 - Architecture/03 - Sources — Web.md](docs/03%20-%20Architecture/03%20-%20Sources%20—%20Web.md).

## Parallel web fetch

`WebConfig.parallel` (default 3) runs N UCBrowser instances concurrently for standard sources. Native-chrome sources stay serial (Cloudflare-safe). Tune via `WEB_PARALLEL=3` env var or the UI control.

## Filtering and caps

`--only` and `--skip` are mutually exclusive. Valid platforms: `discord`, `telegram`, `gmail`, `signal`, `web`.

```powershell
pixi run event-harvester --only discord
pixi run event-harvester --only discord,gmail
pixi run event-harvester --skip web
```

Per-source caps prevent any noisy source from crowding out higher-signal events. `total` is the global ceiling applied after per-source caps. Defaults: discord=50, telegram=50, gmail=30, signal=30, web=30, total=150.

```powershell
pixi run event-harvester --cap discord=20,telegram=30 --cap total=100
```

Env overrides: `CAP_DISCORD=100`, `CAP_TOTAL=200`, etc.

Two caveats the CLI does not make obvious:

- **There is no `--no-cap` flag.** `CapConfig.no_cap` is real, but it is reachable only from the MCP `harvest_events` tool, a profile in `data/profiles.json`, or the Flet UI checkbox.
- **`--cap web=-1` exits non-zero.** `parse_cap_arg` rejects negative values. The `-1` "unlimited" sentinel is honored everywhere else, so use `CAP_WEB=-1` in the environment instead.

`BUG:` `--only` disables per-source caps but still lands on a `total` ceiling of 150 downstream (`pipeline/analysis.py:296`), so "unlimited" is not unlimited. Tier 1.1 in the roadmap.

## Discord cache notes

- Reads from the Electron client's LevelDB cache — no bot token needed.
- Auto-detects `%APPDATA%/discord/Cache/Cache_Data` on Windows, `/mnt/c/Users/<you>/AppData/Roaming/discord/Cache/Cache_Data` under WSL.
- Only contains messages your client has fetched — gaps possible for channels you haven't opened recently.
- If the DB is locked by a running Discord, automatically copies to a temp dir first.

## Development

```powershell
pixi run test                 # full suite — 479 tests, ~15s
pixi run test tests/test_nav_quality.py -v
pixi run lint                 # ruff check src/ tests/
pixi run format               # ruff format src/ tests/
```

`pixi run test`, `lint`, and `format` live in the `dev` feature. See [docs/02 - Implementation/04 - Testing & Dev Loop.md](docs/02%20-%20Implementation/04%20-%20Testing%20&%20Dev%20Loop.md) for what the suite does and does not cover — notably, `tests/test_app.py` does not test the Flet UI.

## Project structure

```
src/event_harvester/
├── cli/                       # argparse + one file per subcommand
│   ├── dispatch.py            # main(), routing, argv normalization
│   ├── parser.py              # subcommand + flag definitions
│   ├── parse_helpers.py       # --cap, --only, --skip parsers
│   └── commands/              # harvest, watch, web, recruiters, classifier, serve, mcp
├── pipeline/                  # the one shared pipeline
│   ├── orchestrator.py        # Pipeline class — every surface calls this
│   ├── harvest.py             # async source fan-out + message cache
│   ├── analysis.py            # LLM extract + prioritize + caps
│   ├── classifier.py          # sklearn classifier (actionable vs noise)
│   ├── reranker.py            # cross-encoder rerank
│   ├── weights.py             # gate signals
│   ├── event_match.py         # fingerprint match/dedup
│   ├── dedup.py               # event dedup helpers
│   └── state.py               # run state / checkpointing
├── sources/                   # one module per input
│   ├── discord.py             # LevelDB cache reader
│   ├── telegram.py            # Telethon MTProto reader
│   ├── gmail.py               # Gmail API reader
│   ├── signal.py              # Signal Desktop SQLCipher reader
│   ├── chromium_cache.py      # dead code? — no callers, see roadmap
│   ├── uc_browser.py          # Playwright + UC extension wrapper
│   ├── web_fetch.py           # web source driver
│   ├── web_session.py         # cookie/session state
│   ├── web_utils.py
│   └── web_modes/             # calendar, feed, search, raw, ics, chat, steps
├── outputs/                   # ticktick.py, report.py, obsidian.py
├── observability/             # navigation_trace, screenshot_capture, nav_quality, tracing
├── servers/mcp_server.py      # Model Context Protocol server
├── ui/                        # app.py (Flet), server.py, display.py
├── training/                  # label.py, eval_classifier.py, recruiter_score.py
├── config.py                  # AppConfig, env loading, validation
├── llm.py                     # litellm chat + vision wrappers
├── interlock.py               # single-GPU/model access interlock
├── profiles.py                # named profiles (web sub-sources + caps)
├── watch.py                   # poll loop
└── web_primitives.py          # L1 page-traversal verbs
```

## Verification

```powershell
pixi run event-harvester --help          # flags here should be a subset of what --help prints
pixi run python scripts/mcp_probe.py     # 20 tools + 2 resources (6 tools if uc_browser is absent)
pixi run test                            # 479 passed
```

If any of those three disagree with this file, this file is the one that is wrong — fix it here and reconcile the matching doc under `docs/`.
