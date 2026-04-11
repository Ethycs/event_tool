# Event Harvester

Pulls messages from Discord (local cache) and Telegram (MTProto), analyses them with OpenRouter AI, and creates actionable tasks in TickTick.

## Pipeline

```
Discord LevelDB cache ─┐
                        ├─→ OpenRouter (task extraction) ─→ TickTick (task creation)
Telegram MTProto ───────┘
```

## Setup

**1. Clone and install**

```bash
# WSL / Linux (recommended)
pip install -e ".[dev]"

# Native Windows (uses pure-Python LevelDB reader)
pip install -e ".[dev,windows]"
```

**2. Configure credentials**

```bash
cp .env.example .env
# Edit .env with your values
```

| Service | Required credentials | Where to get them |
|---------|---------------------|-------------------|
| Telegram | `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_PHONE` | [my.telegram.org](https://my.telegram.org) → API Development Tools |
| OpenRouter | `OPENROUTER_API_KEY` | [openrouter.ai/settings/keys](https://openrouter.ai/settings/keys) |
| TickTick | `TICKTICK_CLIENT_ID`, `TICKTICK_CLIENT_SECRET`, `TICKTICK_USERNAME`, `TICKTICK_PASSWORD` | [developer.ticktick.com](https://developer.ticktick.com) → Manage Apps |

**3. First run**

- **Telegram**: prompts for phone OTP once, then caches the session
- **TickTick**: opens browser for OAuth2 consent once, then caches the token

## Usage

The CLI uses subcommands. Bare `event-harvester` runs the default `harvest` pipeline.

```bash
# Full pipeline — last 7 days, all sources, extract events, sync to TickTick
event-harvester
event-harvester harvest                # equivalent

# Custom time window
event-harvester --days 14

# Only specific sources (positive framing)
event-harvester --only discord,telegram

# Skip specific sources
event-harvester --skip web,signal

# Preview events without creating TickTick tasks
event-harvester --dry-run

# Skip the LLM analysis step entirely
event-harvester --no-analyze

# Save raw messages, or load from a previous run
event-harvester --save messages.json
event-harvester --load messages.json

# Per-source caps (max events fed to the LLM per platform)
event-harvester --cap discord=20,telegram=30 --cap total=100

# Group output by source
event-harvester --group-by-source

# Verbose / quiet
event-harvester -v
event-harvester -q
```

### Subcommands

```bash
# Watch mode — poll every 30s for new Discord/Telegram messages
event-harvester watch
event-harvester watch --interval 10

# Web source management
event-harvester web list                            # show configured web sources
event-harvester web test https://lu.ma/discover     # diagnostics only
event-harvester web add  https://lu.ma/discover     # diagnostics + save to config
event-harvester web login                           # browser login for session state

# Recruiter email workflows
event-harvester recruiters grade                    # grade Gmail recruiters
event-harvester recruiters grade --auto-trash       # also trash low-scoring ones
event-harvester recruiters reparse report.md        # interactively act on saved report

# Classifier training and evaluation
event-harvester classifier train --out-labels labels.json
event-harvester classifier eval  --labels labels.json --out-samples samples/

# Local web review server (alternative to TickTick sync)
event-harvester serve
```

### Filtering sources

`--only` and `--skip` are mutually exclusive. Valid platforms:
`discord`, `telegram`, `gmail`, `signal`, `web`.

```bash
event-harvester --only discord                  # just Discord
event-harvester --only discord,gmail            # Discord + Gmail
event-harvester --skip web                      # everything except web
```

### Per-source caps

The `--cap` flag accepts `key=value` pairs and is repeatable. Per-source caps
prevent any single noisy source from crowding out higher-signal events from
other sources. `total` is the global ceiling applied after per-source caps.

Valid keys: `discord`, `telegram`, `gmail`, `signal`, `web`, `total`.

```bash
event-harvester --cap discord=20,telegram=30 --cap total=100
event-harvester --cap web=50               # raise web ceiling, others default
```

Defaults: discord=50, telegram=50, gmail=30, signal=30, web=30, total=150.
Override the defaults via env vars: `CAP_DISCORD=100`, `CAP_TOTAL=200`, etc.

## Optional filtering

```env
# Only scan these Telegram dialogs (comma-separated, partial match)
TELEGRAM_CHANNELS=work-chat,project-updates

# Exclude these Telegram dialogs
TELEGRAM_EXCLUDE=memes,random

# Override auto-detected Discord cache path
DISCORD_CACHE_PATH=/custom/path/to/Cache_Data
```

## Discord cache notes

- Reads from the Electron client's LevelDB cache — no bot token needed
- **WSL** (recommended): auto-detects `/mnt/c/Users/<you>/AppData/Roaming/discord/Cache/Cache_Data`
- **Native Windows**: auto-detects `%APPDATA%/discord/Cache/Cache_Data`
- Only contains messages your client has fetched — channels you haven't opened recently may have gaps
- If the DB is locked by a running Discord client, automatically copies to a temp dir

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check src/ tests/
ruff format src/ tests/
```

## Project structure

```
src/event_harvester/
├── cli/
│   ├── dispatch.py     # main(), routing, normalization
│   ├── parser.py       # argparse subcommand setup
│   ├── parse_helpers.py # --cap, --only, --skip parsers
│   └── commands/       # one file per subcommand
│       ├── harvest.py
│       ├── watch.py
│       ├── web.py
│       ├── recruiters.py
│       ├── classifier.py
│       └── serve.py
├── config.py           # Config loading + validation
├── display.py          # Terminal formatting
├── analysis.py         # OpenRouter client + prompt builder
├── ticktick.py         # TickTick OAuth2 + task creation
├── watch.py            # Watch-mode polling loop
└── sources/
    ├── discord.py      # LevelDB cache reader
    ├── telegram.py     # Telethon MTProto reader
    ├── gmail.py        # Gmail API reader
    ├── signal.py       # Signal Desktop SQLCipher reader
    └── web_fetch.py    # Playwright-based web event scraper
```
