# Event Harvester

Pulls messages from **Discord, Telegram, Gmail, Signal, and the open web**, extracts event candidates with an LLM, and surfaces them through three review interfaces: a CLI report, an interactive Flet web app, and an MCP server for AI clients.

## Pipeline

```
Discord LevelDB cache ─┐
Telegram MTProto ──────┤
Gmail API ─────────────┼─► classifier ─► reranker ─► LLM extract ─► match/dedup ─┬─► CLI report
Signal SQLCipher ──────┤                                                          ├─► Flet web UI
Web (Playwright × N) ──┘                                                          ├─► MCP tools
                                                                                  └─► TickTick sync
```

Five web modes (`calendar`, `feed`, `search`, `raw`, `chat`) run in parallel browser instances, each per-source-tunable via `data/web_sources.json`. Native-chrome mode is used for Cloudflare-gated sources.

## Setup

This project is managed with [pixi](https://pixi.sh). See [pixi_guide.md](pixi_guide.md) for env details.

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

On first run, Telegram prompts for phone OTP once and TickTick opens a browser for OAuth consent once; both cache thereafter.

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
pixi run event-harvester --group-by-source
```

Subcommands:

```powershell
pixi run event-harvester watch                 # poll every 30s
pixi run event-harvester web list              # show web sources
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

Exposed tools:

| Tool                     | Use                                                            |
|--------------------------|----------------------------------------------------------------|
| `config_summary`         | enabled sources, caps, credential readiness                    |
| `harvest_events`         | run the full pipeline; cache extracted events                  |
| `list_latest_events`     | list cached event candidates (filter by status)                |
| `approve_event`          | create/update a TickTick task by index                         |
| `decline_event`          | mark a cached event index as declined (saves fingerprint)      |
| `generate_latest_report` | write a Markdown report from the cached events                 |

Resources: `event-harvester://config`, `event-harvester://latest-events`.

A stdio probe lives at [scripts/mcp_probe.py](scripts/mcp_probe.py):

```powershell
pixi run python scripts/mcp_probe.py                   # list tools/resources
pixi run python scripts/mcp_probe.py call config_summary
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
- `OTEL_EXPORTER_OTLP_ENDPOINT` — point spans at any OTLP collector.

## Per-source scroll time

Each entry in `data/web_sources.json` has a `scroll_seconds` field that controls how long the page is scrolled before extraction (or in the case of `feed` mode, the dwell loop length):

```json
[
  {"url": "https://lu.ma/discover", "name": "luma", "mode": "calendar", "scroll_seconds": 5},
  {"url": "https://www.instagram.com/", "name": "instagram", "mode": "feed", "scroll_seconds": 25, "api_pattern": "graphql"}
]
```

Default if omitted: 5 seconds.

## Parallel web fetch

`WebConfig.parallel` (default 3) runs N UCBrowser instances concurrently for standard sources. Native-chrome sources stay serial (Cloudflare-safe). Tune via `WEB_PARALLEL=3` env var or the UI control.

## Filtering and caps

`--only` and `--skip` are mutually exclusive. Valid platforms: `discord`, `telegram`, `gmail`, `signal`, `web`.

```powershell
pixi run event-harvester --only discord
pixi run event-harvester --only discord,gmail
pixi run event-harvester --skip web
```

Per-source caps prevent any noisy source from crowding out higher-signal events. `total` is the global ceiling applied after per-source caps. Defaults: discord=50, telegram=50, gmail=30, signal=30, web=30, total=150. Set any cap to `-1` for unlimited, or use `--no-cap` to disable all caps.

```powershell
pixi run event-harvester --cap discord=20,telegram=30 --cap total=100
pixi run event-harvester --cap web=-1                 # unlimited web
pixi run event-harvester --no-cap
```

Env overrides: `CAP_DISCORD=100`, `CAP_TOTAL=200`, etc.

## Discord cache notes

- Reads from the Electron client's LevelDB cache — no bot token needed.
- Auto-detects `%APPDATA%/discord/Cache/Cache_Data` on Windows, `/mnt/c/Users/<you>/AppData/Roaming/discord/Cache/Cache_Data` under WSL.
- Only contains messages your client has fetched — gaps possible for channels you haven't opened recently.
- If the DB is locked by a running Discord, automatically copies to a temp dir first.

## Development

```powershell
pixi run test                 # full suite
pixi run test tests/test_nav_quality.py -v
ruff check src/ tests/
ruff format src/ tests/
```

## Project structure

```
src/event_harvester/
├── cli/
│   ├── dispatch.py            # main(), routing, argv normalization
│   ├── parser.py              # argparse subcommand setup
│   ├── parse_helpers.py       # --cap, --only, --skip parsers
│   └── commands/              # one file per subcommand
│       ├── harvest.py
│       ├── watch.py
│       ├── web.py
│       ├── recruiters.py
│       ├── classifier.py
│       ├── serve.py
│       └── mcp.py
├── app.py                     # Flet web UI + pipeline control state machine
├── config.py                  # AppConfig, env loading, validation
├── display.py                 # Terminal formatting
├── harvest.py                 # async source fan-out + message cache
├── analysis.py                # LLM extract + prioritize + caps
├── classifier.py              # sklearn classifier (actionable vs noise)
├── weights.py                 # reranker + gate signals
├── event_match.py             # fingerprint match/dedup
├── dedup.py                   # event dedup helpers
├── llm.py                     # litellm chat + vision wrappers
├── profiles.py                # named profiles (web sub-sources + caps)
├── ticktick.py                # TickTick OAuth + task creation
├── report.py                  # Markdown report writer
├── navigation_trace.py        # JSONL nav-event recorder (Phase 1)
├── screenshot_capture.py      # PNG + GIF capture (Phase 2)
├── nav_quality.py             # VLM scorer + key-frame picker (Phase 3)
├── tracing.py                 # OpenTelemetry bootstrap
├── mcp_server.py              # Model Context Protocol server
└── sources/
    ├── discord.py             # LevelDB cache reader
    ├── telegram.py            # Telethon MTProto reader
    ├── gmail.py               # Gmail API reader
    ├── signal.py              # Signal Desktop SQLCipher reader
    ├── uc_browser.py          # Playwright + UC extension wrapper
    └── web_fetch.py           # calendar/feed/search/raw/chat modes
```
