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

```bash
# Full pipeline — last 7 days, both platforms, analyse + create tasks
event-harvester

# Custom time window
event-harvester --days 14

# Discord only, skip analysis
event-harvester --no-telegram --no-analysis

# Telegram only
event-harvester --no-discord

# Preview tasks without creating them
event-harvester --dry-run

# Save raw messages for later
event-harvester --save messages.json

# Load saved messages (skip harvesting)
event-harvester --load messages.json

# Watch mode — poll every 30s for new messages
event-harvester --watch
event-harvester --watch --interval 10

# Debug output
event-harvester -v
```

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
├── cli.py              # CLI entry point + orchestration
├── config.py           # Config loading + validation
├── display.py          # Terminal formatting
├── analysis.py         # OpenRouter client + prompt builder
├── ticktick.py         # TickTick OAuth2 + task creation
├── watch.py            # Watch-mode polling loop
└── sources/
    ├── discord.py      # LevelDB cache reader
    └── telegram.py     # Telethon MTProto reader
```
