# Configuration & Profiles

**Status:** CURRENT · **Date:** 2026-07-29 · **Applies to:** `src/event_harvester/config.py`, `src/event_harvester/profiles.py`, `src/event_harvester/utils.py`, `src/event_harvester/logging_tags.py`, `.env.example`, `pyproject.toml`, `data/web_sources.json`, `data/profiles.json`

`config.py` loads a single `AppConfig` dataclass tree from environment variables (via `python-dotenv`) with hard-coded defaults; nothing is required to import the module. CLI flags and named profiles (`data/profiles.json`) both mutate an already-loaded `AppConfig` in place, and the two mutation paths run in a fixed order per command. Four other modules read `os.environ`/`os.getenv` directly for host-path and offline-model concerns that don't belong in `AppConfig`.

## Hard constraints

| Constraint | Value | Where |
|---|---|---|
| Precedence order (per-run) | **CLI flag > profile > env var > dataclass default** | see "Precedence" below |
| `load_config()` reads env once | `load_dotenv()` runs at module import time; env is not re-read per call | `config.py:10` |
| Screenshots imply trace | `cfg.web.debug_screenshots=True` forces `cfg.web.debug_trace=True` inside `load_config()`, and again in each CLI command that sets the flag directly | `config.py:337-338`, `cli/commands/harvest.py:47-49`, `cli/commands/serve.py:65-67` |
| `WEB_PARALLEL` floor | `max(1, int(env))` — cannot go below 1 | `config.py:307` |
| Web source-name resolution order | `web_sources` kwarg (list) > `web_source` kwarg (single name, back-compat) > `cfg.web.selected_names` (env `WEB_SOURCE_NAMES` or profile) > all sources in `data/web_sources.json` | `pipeline/harvest.py:218-230` |
| `--cap` CLI flag key allowlist | Only `discord\|telegram\|gmail\|signal\|web\|total` (`VALID_CAP_KEYS`) — **`no_cap` and per-web-sub-source keys (e.g. `luma`) are rejected by `--cap`** | `cli/parse_helpers.py:12,18-56` |
| Profile caps bypass that allowlist | `apply_profile()` calls `apply_caps_to_config()` directly with the profile's raw `caps` dict — no key validation, so a profile (or the MCP layer) can set `no_cap` or dynamic sub-source keys that `--cap` cannot | `profiles.py:124-132`, `parse_helpers.py:92-104` |
| Negative per-source/total cap | `cap < 0` (e.g. `-1`) means "unlimited for this bucket" at the analysis stage — a second, config-adjacent way to disable capping besides `caps.no_cap` | `pipeline/analysis.py:184-199,290-291` |
| Data dir anchor | `<repo>/data`, computed as `Path(__file__).resolve().parents[3]` (`pipeline/harvest.py`, `sources/web_session.py`) or `...parent.parent.parent.parent` (`sources/web_utils.py`) from the module file — **4 levels up**, not 3 | `tests/test_data_paths.py` |

## 1. The `AppConfig` dataclass tree

`AppConfig` (`config.py:199-223`) composes nine nested config dataclasses plus top-level scalars. All are populated by `load_config()`; direct construction (as in tests) uses the field defaults shown below.

### `AppConfig` (top level)

| Field | Type | Default | Env var | CLI flag | Meaning |
|---|---|---|---|---|---|
| `days_back` | `int` | `7` | `DAYS_BACK` | `--days` (harvest/serve/recruiters-grade/classifier-train) | Lookback window for message harvesting |
| `skip_analyze` | `bool` | `False` | `SKIP_ANALYZE` | `--no-analyze` (harvest, sets `cfg.skip_analyze=True`) | Skip LLM event extraction, return raw harvest only |
| `dry_run` | `bool` | `False` | `DRY_RUN` | `--dry-run` (harvest, read via `args.dry_run` — not written back to `cfg`) | Never create real TickTick tasks |
| `obsidian_events_dir` | `str` | `""` | `OBSIDIAN_EVENTS_DIR` | none | Output dir for Obsidian event reports (used only if `--obsidian` set) |
| `obsidian_recruiters_dir` | `str` | `""` | `OBSIDIAN_RECRUITERS_DIR` | none | Output dir for Obsidian recruiter reports |
| `telegram_channels` | `list[str]` | `[]` | `TELEGRAM_CHANNELS` (CSV) | none | Allowlist of Telegram dialog names |
| `telegram_exclude` | `list[str]` | `[]` | `TELEGRAM_EXCLUDE` (CSV) | none | Blocklist of Telegram dialog names |
| `telegram` | `TelegramConfig` | — | — | — | nested |
| `discord` | `DiscordConfig` | — | — | — | nested |
| `gmail` | `GmailConfig` | — | — | — | nested |
| `signal` | `SignalConfig` | — | — | — | nested |
| `llm` | `LLMConfig` | — | — | — | nested |
| `ticktick` | `TickTickConfig` | — | — | — | nested |
| `web` | `WebConfig` | — | — | — | nested |
| `caps` | `CapConfig` | — | — | — | nested |
| `sources` | `SourceConfig` | — | — | — | nested |

`dry_run`: note `harvest_cmd` reads `args.dry_run` straight into the TickTick sync call and the printed banner — it never assigns `cfg.dry_run`, so `cfg.dry_run` stays at its env/default value for the whole run even when `--dry-run` is passed. `cfg.dry_run` is a live field only for other callers (UI, MCP `harvest_events`'s… — actually MCP's `harvest_events` doesn't take a dry_run param either). **undocumented**: `cfg.dry_run` and the `DRY_RUN` env var currently have no code path that reads them at all outside test fixtures — grep shows `dry_run` written by `load_config()` but never read via `cfg.dry_run` in `pipeline/` or `cli/`. `AppConfig.dry_run` looks like dead config surface for the CLI path.

### `TelegramConfig`

| Field | Type | Default | Env var | CLI flag | Meaning |
|---|---|---|---|---|---|
| `api_id` | `int` | `0` | `TELEGRAM_API_ID` | none | Telethon API ID; non-numeric env value silently falls back to `0` (`config.py:234-238`) |
| `api_hash` | `str` | `""` | `TELEGRAM_API_HASH` | none | Telethon API hash |
| `phone` | `str` | `""` | `TELEGRAM_PHONE` | none | Login phone number |
| `session` | `str` | `"harvest_session"` | `TELEGRAM_SESSION` | none | Telethon session file name |

`is_configured` = `bool(api_id and api_hash)`.

### `DiscordConfig`

| Field | Type | Default | Env var | CLI flag | Meaning |
|---|---|---|---|---|---|
| `cache_path` | `Optional[str]` | `None` | `DISCORD_CACHE_PATH` | none | Override for auto-detected Discord Chromium-cache path |

`is_configured` is hard-coded `True` (no credentials required; cache reading only).

### `GmailConfig`

| Field | Type | Default | Env var | CLI flag | Meaning |
|---|---|---|---|---|---|
| `credentials_file` | `str` | `"credentials.json"` | `GMAIL_CREDENTIALS_FILE` | none | OAuth client secret file path |
| `token_file` | `str` | `"token.json"` | `GMAIL_TOKEN_FILE` | none | Cached OAuth token file path |
| `query` | `str` | `"newer_than:7d"` | **none** | none | Gmail search query prefix, combined with a computed date filter (`sources/gmail.py:156`) |
| `max_results` | `int` | `200` | **none** | none | Max messages fetched per Gmail run (`sources/gmail.py:163-164`) |

**undocumented / dead config?**: `query` and `max_results` are real, load-bearing fields (`sources/gmail.py` reads `cfg.query` and `cfg.max_results`), but `load_config()` never sets them from any env var — `GmailConfig(...)` in `config.py:276-279` only passes `credentials_file`/`token_file`, so `query`/`max_results` are stuck at their dataclass defaults for every CLI/MCP run. There is no `GMAIL_QUERY` or `GMAIL_MAX_RESULTS` env var and no CLI flag; the only way to change them is constructing `GmailConfig` directly (tests only).

`is_configured` = `Path(credentials_file).exists() or bool(os.getenv("GMAIL_CREDENTIALS_JSON")) or bool(os.getenv("GMAIL_TOKEN_JSON"))` — these two env vars are read directly inside the property (`config.py:100-101`), not through `load_config()`.

### `SignalConfig`

| Field | Type | Default | Env var | CLI flag | Meaning |
|---|---|---|---|---|---|
| `db_path` | `Optional[str]` | `None` | `SIGNAL_DB_PATH` | none | Override for auto-detected Signal Desktop data directory |

`is_configured` is hard-coded `True`. `db_path` is passed as `override_path` into `sources/signal.py:_find_signal_dir()`, whose "path does not exist" warning names `SIGNAL_DB_PATH` — the variable actually read. *Fixed 2026-07-29: it used to hard-code `SIGNAL_DATA_PATH`, which exists nowhere else in the codebase.*

### `LLMConfig`

| Field | Type | Default | Env var | CLI flag | Meaning |
|---|---|---|---|---|---|
| `model` | `str` | `"gpt-4o-mini"` | `LLM_MODEL` | none | litellm-format model string (e.g. `anthropic/claude-3.5-haiku`) |
| `api_key` | `str` | `""` | `OPENAI_API_KEY` else `ANTHROPIC_API_KEY` | none | First non-empty of the two wins (`config.py:246-250`); `OPENROUTER_API_KEY` is documented in `.env.example` but is **not** read by `load_config()` at all — litellm may pick it up itself when invoked, but `cfg.llm.api_key`/`is_configured` never see it |
| `backend` | `str` | `"litellm"` | `LLM_BACKEND` | none | `"litellm"` or `"local"` |
| `model_path` | `str` | `""` | `LLM_MODEL_PATH` | none | `.onnx` path, local backend only |
| `tokenizer_path` | `str` | `""` | `LLM_TOKENIZER_PATH` | none | HF-format tokenizer dir, local backend only |
| `device` | `str` | `"npu"` | `LLM_DEVICE` | none | `"npu"`, `"gpu"`, or `"cpu"`, local backend only |
| `vision_model` | `str` | `""` | `VLM_MODEL` | none | Empty = auto-pick by API-key prefix (`sk-ant-` → Claude Sonnet, else gpt-4o-mini) |
| `vision_max_images_per_run` | `int` | `10` | `VLM_MAX_IMAGES_PER_RUN` | none | Cost-guard ceiling for the nav-quality VLM scorer; non-int env value logs a warning and keeps `10` (`config.py:252-264`) |

`is_configured`: `bool(model_path)` if `backend == "local"`, else `bool(model)`.

### `TickTickConfig`

| Field | Type | Default | Env var | CLI flag | Meaning |
|---|---|---|---|---|---|
| `client_id` | `str` | `""` | `TICKTICK_CLIENT_ID` | none | OAuth app client ID |
| `client_secret` | `str` | `""` | `TICKTICK_CLIENT_SECRET` | none | OAuth app client secret |
| `redirect_uri` | `str` | `"http://127.0.0.1:8080"` | `TICKTICK_REDIRECT_URI` | none | OAuth redirect URL |
| `username` | `str` | `""` | `TICKTICK_USERNAME` | none | TickTick account login |
| `password` | `str` | `""` | `TICKTICK_PASSWORD` | none | TickTick account password |
| `project` | `str` | `""` | `TICKTICK_PROJECT` | none | Target project name; blank = Inbox |

`is_configured` = all of `client_id, client_secret, username, password` truthy.

### `WebConfig`

| Field | Type | Default | Env var | CLI flag | Meaning |
|---|---|---|---|---|---|
| `sources_file` | `str` | `"data/web_sources.json"` | `WEB_SOURCES_FILE` | none | Path to the web-source list (see §5 — note `web_utils.py` resolves its own copy independent of this field, see below) |
| `max_events` | `int` | `30` | `WEB_MAX_EVENTS` | none | Cap on events returned per web source |
| `timeout_ms` | `int` | `30000` | `WEB_TIMEOUT_MS` | none | Playwright navigation timeout |
| `no_cooldown` | `bool` | `False` | `WEB_NO_COOLDOWN` | `--no-cooldown` (harvest; passed as a `pipeline.run()` kwarg, `None`-defaults to `cfg.web.no_cooldown` — see `pipeline/orchestrator.py:132,139`) | Ignore per-source fetch cooldowns |
| `selected_names` | `list[str]` | `[]` | `WEB_SOURCE_NAMES` (CSV) | `--web-source NAME` (single name; and profiles set the whole list) | Restrict harvest to named web sub-sources; empty = all |
| `parallel` | `int` | `3` | `WEB_PARALLEL` | none | Parallel UCBrowser instances for non-Cloudflare sources; floored at 1 |
| `debug_trace` | `bool` | `False` | `WEB_DEBUG_TRACE` | `--debug-web-trace` | Record navigation trace JSONL |
| `debug_screenshots` | `bool` | `False` | `WEB_DEBUG_SCREENSHOTS` | `--debug-web-screenshots` | Capture per-step PNGs (implies `debug_trace`) |

**undocumented**: `cfg.web.sources_file`/`WEB_SOURCES_FILE` is set on `AppConfig` but `sources/web_utils.py` computes its own module-level `_WEB_SOURCES_FILE = _DATA_DIR / "web_sources.json"` (`web_utils.py:68`) independent of `cfg.web.sources_file` — grep shows no call site reads `cfg.web.sources_file` to override the load path. Setting `WEB_SOURCES_FILE` therefore has **no effect** on which file is actually loaded; this looks like a dead config field. `data/web_sources.json` currently holds 30 entries (Luma, SF Chamber, erobay, Instagram, Eventbrite, Meetup, Tockify, plus ~23 SF venue calendars/ICS/raw pages).

### `CapConfig`

| Field | Type | Default | Env var | CLI flag | Meaning |
|---|---|---|---|---|---|
| `discord` | `int` | `50` | `CAP_DISCORD` | `--cap discord=N` | Per-source message cap before merge |
| `telegram` | `int` | `50` | `CAP_TELEGRAM` | `--cap telegram=N` | ″ |
| `gmail` | `int` | `30` | `CAP_GMAIL` | `--cap gmail=N` | ″ |
| `signal` | `int` | `30` | `CAP_SIGNAL` | `--cap signal=N` | ″ |
| `web` | `int` | `30` | `CAP_WEB` | `--cap web=N` | ″; also the fallback cap for unknown platform names (web sub-source tags like `luma`, `instagram`) via `CapConfig.get()` |
| `total` | `int` | `150` | `CAP_TOTAL` | `--cap total=N` | Global ceiling after per-source caps |
| `group_by_source` | `bool` | `False` | `CAP_GROUP_BY_SOURCE` | `--group-by-source` (deprecated, terminal output is always grouped) | Output bucketing flag |
| `no_cap` | `bool` | `False` | none | none (rejected by `--cap`'s key allowlist) | Bypass all per-source and total caps; settable only via a profile's `caps.no_cap`, the MCP `harvest_events(no_cap=...)` param, or the Flet UI checkbox |

Dynamic per-web-sub-source caps (e.g. `luma=25`) are not dataclass fields — they're added to the `CapConfig` instance via `setattr()` in `apply_caps_to_config()`, reachable only through profiles (or direct programmatic use), never through `--cap` (blocked by `VALID_CAP_KEYS`). `CapConfig.get(platform)` looks them up case-insensitively via `getattr(self, platform.lower(), self.web)`.

### `SourceConfig`

| Field | Type | Default | Env var | CLI flag | Meaning |
|---|---|---|---|---|---|
| `discord` | `bool` | `True` | `SOURCE_DISCORD` (`"0"/"false"/"no"` disable) | none directly† | Persistent per-source enable toggle (UI-controlled) |
| `telegram` | `bool` | `True` | `SOURCE_TELEGRAM` | none directly† | ″ |
| `gmail` | `bool` | `True` | `SOURCE_GMAIL` | none directly† | ″ |
| `signal` | `bool` | `True` | `SOURCE_SIGNAL` | none directly† | ″ |
| `web` | `bool` | `True` | `SOURCE_WEB` | none directly† | ″ |

† `--only`/`--skip` (harvest, watch, serve, classifier-train) do **not** go through `SourceConfig`/env for the `harvest` subcommand — they're parsed by `resolve_platforms()` into a one-off `no_<source>` kwargs dict passed straight to `Pipeline.run()`/`harvest_messages()`, leaving `cfg.sources` untouched. `serve_cmd` is the one exception: it *does* write `--only`/`--skip` back onto `cfg.sources.*` (`cli/commands/serve.py:49-55`) so the Flet UI's persistent toggles start in the right state.

## 2. Complete env-var table

Vars read inside `config.py` (via `load_config()` or an `is_configured` property):

| Env var | Consumed by | Notes |
|---|---|---|
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_PHONE`, `TELEGRAM_SESSION` | `TelegramConfig` | |
| `DISCORD_CACHE_PATH` | `DiscordConfig` | |
| `GMAIL_CREDENTIALS_FILE`, `GMAIL_TOKEN_FILE` | `GmailConfig` | |
| `GMAIL_CREDENTIALS_JSON`, `GMAIL_TOKEN_JSON` | `GmailConfig.is_configured` | read directly in the property, not in `load_config()`; base64-JSON alternative to the file paths, decoded in `sources/gmail.py:_decode_env_json` |
| `SIGNAL_DB_PATH` | `SignalConfig` | |
| `LLM_MODEL`, `LLM_BACKEND`, `LLM_MODEL_PATH`, `LLM_TOKENIZER_PATH`, `LLM_DEVICE`, `VLM_MODEL`, `VLM_MAX_IMAGES_PER_RUN` | `LLMConfig` | |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` | `LLMConfig.api_key` | first non-empty wins |
| `TICKTICK_CLIENT_ID`, `TICKTICK_CLIENT_SECRET`, `TICKTICK_REDIRECT_URI`, `TICKTICK_USERNAME`, `TICKTICK_PASSWORD`, `TICKTICK_PROJECT` | `TickTickConfig` | |
| `WEB_SOURCES_FILE`, `WEB_MAX_EVENTS`, `WEB_TIMEOUT_MS`, `WEB_NO_COOLDOWN`, `WEB_SOURCE_NAMES`, `WEB_PARALLEL`, `WEB_DEBUG_TRACE`, `WEB_DEBUG_SCREENSHOTS` | `WebConfig` | `WEB_SOURCES_FILE` is dead — see §1 |
| `CAP_DISCORD`, `CAP_TELEGRAM`, `CAP_GMAIL`, `CAP_SIGNAL`, `CAP_WEB`, `CAP_TOTAL`, `CAP_GROUP_BY_SOURCE` | `CapConfig` | |
| `SOURCE_DISCORD`, `SOURCE_TELEGRAM`, `SOURCE_GMAIL`, `SOURCE_SIGNAL`, `SOURCE_WEB` | `SourceConfig` | |
| `SKIP_ANALYZE`, `DRY_RUN` | `AppConfig` | `DRY_RUN`/`cfg.dry_run` appears unread by the CLI — see §1 |
| `OBSIDIAN_EVENTS_DIR`, `OBSIDIAN_RECRUITERS_DIR` | `AppConfig` | |
| `DAYS_BACK` | `AppConfig.days_back` | non-numeric value silently falls back to `7` |
| `TELEGRAM_CHANNELS`, `TELEGRAM_EXCLUDE` | `AppConfig` | CSV, whitespace-trimmed |

Vars read **outside `config.py`** (grepped `os.environ`/`os.getenv` across `src/`, marked `read outside config.py`):

| Env var | File:function | Purpose |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `cli/dispatch.py:main` (read outside config.py) | Passed to `init_tracing()` at CLI startup, independent of `--debug-otel` |
| `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_DEBUG_CONSOLE` | `observability/tracing.py:_autoinit_from_env` (read outside config.py) | Optional convenience auto-init hook — **unverified** whether anything actually calls `_autoinit_from_env()`; `dispatch.py` reads `OTEL_EXPORTER_OTLP_ENDPOINT` itself instead |
| `APPDATA` | `sources/discord.py:find_discord_cache`, `sources/signal.py:_find_signal_dir` (read outside config.py) | Windows per-user cache root, used when `DISCORD_CACHE_PATH`/`SIGNAL_DB_PATH` are unset |
| `LOCALAPPDATA` | `sources/chromium_cache.py:find_chrome_cache,find_edge_cache` (read outside config.py) | Windows Chrome/Edge cache root (generic reader, not currently wired to `AppConfig`) |
| `EVENT_HARVESTER_CLASSIFIERS_DIR` | `pipeline/classifier.py:_find_classifiers_dir` (read outside config.py) | Overrides the searched-upward `classifiers/` dir location |
| `EVENT_HARVESTER_RERANKER_PATH` | `pipeline/reranker.py:_resolve_model_source` (read outside config.py) | Overrides the local cross-encoder model directory |
| `HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE` | `pipeline/reranker.py:_get_model` (read outside config.py) | Set/unset by the reranker itself (`os.environ.setdefault`/`.pop`) to force or release offline mode around a local `sentence_transformers` load — not user-facing inputs |
| `OPENROUTER_API_KEY` | documented in `.env.example` only | **undocumented / dead config?** — not read anywhere in `src/`; litellm may consume it internally when a request is made, but `AppConfig`/`is_configured` never see it |

## 3. `validate_config` — warnings

`validate_config(cfg, *, need_telegram=True, need_discord=True, need_gmail=True, need_analysis=True, need_ticktick=True)` (`config.py:342-378`) returns a list of warning strings; it never raises (despite the docstring mentioning `ConfigError` for fatal issues — no fatal path is implemented in this function today). Each `need_*` flag is set per-command by the caller (e.g. `harvest_cmd` passes `need_ticktick=not args.no_sync and not args.dry_run`).

| Condition | Trigger | Warning text (abridged) |
|---|---|---|
| `need_telegram and not cfg.telegram.is_configured` | `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` unset | "Telegram: ... not set - Telegram will be skipped." |
| `need_gmail and not cfg.gmail.is_configured` | no `credentials.json` file and neither `GMAIL_CREDENTIALS_JSON` nor `GMAIL_TOKEN_JSON` set | "Gmail: credentials.json not found - Gmail will be skipped." |
| `need_analysis and not cfg.llm.is_configured` | local backend with no `model_path`, or litellm backend with empty `model` (model has a non-empty default, so this fires only if something explicitly blanks it) | "LLM: No model configured - analysis will be skipped." |
| `need_ticktick and not cfg.ticktick.is_configured` | any of `TICKTICK_CLIENT_ID/CLIENT_SECRET/USERNAME/PASSWORD` unset | "TickTick: Missing one or more of ... - task creation will be skipped." |

Discord (`need_discord`) is accepted as a parameter but has **no corresponding warning branch** in the function body — `DiscordConfig.is_configured` is hard-coded `True`, so there's nothing to warn about; the parameter exists only so callers have a uniform kwarg signature. Same non-issue for Signal (no `need_signal` parameter at all, no warning).

## 4. Profiles

`data/profiles.json` schema (`profiles.py:31-76`):

```json
{
  "profiles": [
    {
      "name": "tech-meetups",
      "description": "Luma + Eventbrite + Meetup, relaxed web cap",
      "web_sources": ["luma", "eventbrite", "meetup"],
      "caps": {"web": 60, "total": 120, "luma": 25, "eventbrite": 20, "meetup": 15}
    }
  ]
}
```

| Key | Type | Required | Meaning |
|---|---|---|---|
| top-level `profiles` | list of objects | yes | `ProfileError` if missing/not a list |
| `name` | string | yes, unique | `ProfileError` on missing/blank/duplicate |
| `description` | string | no (default `""`) | Free text |
| `web_sources` | list of strings | no (default `[]`) | Names from `data/web_sources.json`; `[]` means "don't restrict" (all sources) |
| `caps` | object | no (default `{}`) | Merged into `cfg.caps` via `setattr` — any key, not validated against `VALID_CAP_KEYS` |

The repo's `data/profiles.json` ships three profiles: `tech-meetups` (Luma/Eventbrite/Meetup, relaxed caps), `sf-local` (SF Chamber + erobay only), and `no-web` — which sets `web_sources: ["__none__"]`, a sentinel name that matches nothing in `data/web_sources.json`, so the web-fetch filter selects zero sources. Not special-cased in code; it relies on no real source being named `__none__` **and** on a zero-match filter meaning zero. *That second half only became true on 2026-07-30 — before then an unmatched filter fell through to "all sources", so this profile ran a full web harvest. See BUG-WEBSRC-1 in `EH-STD-WEBSRC-v0`.*

**Applying a profile** (`apply_profile`, `profiles.py:111-132`):
1. `cfg.web.selected_names = list(profile.web_sources)` (full replace, not merge — an empty list explicitly clears any prior selection, per `test_empty_web_sources_means_all`).
2. `apply_caps_to_config(cfg, [profile.caps])` if `profile.caps` is non-empty — `setattr(cfg.caps, key, value)` per key, so unknown keys become new instance attributes.

**What a profile can override**: `cfg.web.selected_names`, any `CapConfig` attribute (including dynamically-created per-source ones and `no_cap`, bypassing the CLI's key allowlist).

**What a profile cannot touch** (by design, per the module docstring): `cfg.sources.*` (top-level per-source enable toggles), `cfg.days_back`, or any behavior flag (`skip_analyze`, `dry_run`, debug flags). There is intentionally no in-UI save/delete for profiles — hand-edit the JSON file.

**Application order relative to CLI flags** — from `harvest_cmd` (`cli/commands/harvest.py:32-54`):
```python
if args.days is not None:
    cfg.days_back = args.days          # 1. --days
if getattr(args, "profile", None):
    _apply_profile_by_name(cfg, args.profile)   # 2. profile (web sources + caps)
apply_caps_to_config(cfg, args.cap)    # 3. --cap (explicit, wins over profile)
if args.group_by_source: ...
if args.debug_web_trace: ...
if args.no_analyze: ...
```
Explicit `--cap key=value` flags are applied *after* the profile and win on shared keys — this is the concrete instance of the documented CLI > profile > env > default precedence. `serve_cmd` follows the same order (`--only`/`--skip` → profile → debug flags).

## 5. Data-path resolution

**The pinned rule** (`tests/test_data_paths.py`, added after a regression): the data directory is `<repo>/data`, and every module that needs it independently computes `Path(__file__).resolve()` walked up to the repo root, then appends `"data"`. A past commit had `pipeline/harvest.py` using 3 `.parent` hops instead of 4, landing on `<repo>/src/data` — a stray directory nothing else read, silently breaking the "delete the cache to force refresh" instruction and causing `harvest_events(web_source=X)` to serve source Y's stale cache. `pipeline/classifier.py` had the identical off-by-one for its `classifiers/` directory (fixed by switching to an upward directory *search* instead of a fixed `.parent` count — see `EVENT_HARVESTER_CLASSIFIERS_DIR` in §2).

| Module | `_DATA_DIR` computation | Anchor depth |
|---|---|---|
| `pipeline/harvest.py` | `Path(__file__).resolve().parents[3]` | 3 (via `.parents[3]`, equivalent to 4 `.parent` hops) |
| `sources/web_session.py` | `Path(__file__).resolve().parent.parent.parent.parent` | 4 explicit `.parent`s |
| `sources/web_utils.py` | `Path(__file__).resolve().parent.parent.parent.parent` | 4 explicit `.parent`s |
| `ui/server.py` | `Path("data")` — **relative to CWD**, not anchored to `__file__` at all | n/a |
| `pipeline/classifier.py` | searches upward from `__file__` for a directory literally named `classifiers` (falls back to `parents[3] / "classifiers"` if not found) | search, not a fixed count |

**unverified**: `ui/server.py`'s `_DATA_DIR = Path("data")` is not covered by `test_data_paths.py` (which only pins `harvest`, `web_utils`, `web_session`) and resolves relative to the process's current working directory rather than the repo root — it will silently diverge from every other module's `<repo>/data` if `event-harvester` is invoked from outside the repo root.

Files written under `<repo>/data/` (from the modules read for this doc):

| Path | Writer | Purpose |
|---|---|---|
| `data/.message_cache.json` | `pipeline/harvest.py` (`CACHE_FILE`) | 4-hour message harvest cache (`CACHE_MAX_AGE`) |
| `data/.playwright_state.json` | `sources/web_session.py` (`STATE_FILE`) | Saved Playwright browser storage state (cookies/login) |
| `data/.web_cooldowns.json` | `sources/web_utils.py` (`_COOLDOWN_FILE`) | Per-URL last-fetch timestamps for the 1h default cooldown |
| `data/web_sources.json` | not written by config code; hand-maintained / `event-harvester web add` | The 30-entry web-source list read by `web_utils.py:_WEB_SOURCES_FILE` |
| `data/profiles.json` | hand-edited only (§4) | Named profiles |
| `data/.server_events.json` | `ui/server.py` (`_EVENTS_FILE`, under its own possibly-CWD-relative `_DATA_DIR`) | UI server event cache |
| `data/rejects.ini` | `cli/commands/harvest.py:_print_rejects` | `--show-rejects` dump, not part of the config subsystem proper |
| `data/.web_traces/<source>/<run-id>.jsonl` (+ PNGs/GIFs) | `observability/navigation_trace.py`, `screenshot_capture.py` | Debug trace output when `debug_trace`/`debug_screenshots` are on (see `04 - Observability.md`) |

## Verification

```
pixi run event-harvester mcp --transport stdio
```
then, from an MCP client (or the `config_summary` tool directly), call `config_summary`. It returns `_summarize_config(load_config())` (`servers/mcp_server.py:163-193`) — `days_back`, all five `sources.*` toggles, all six `caps.*` values plus `no_cap`, `configured.{telegram,gmail,llm,ticktick}` booleans, and `llm.{backend,model}`. This proves: (a) `load_config()` runs cleanly end-to-end against the current `.env`, (b) the env-var → dataclass wiring in §1/§2 is live, and (c) each `is_configured` property reflects the real credential state without needing to run a harvest. To additionally verify profile application, pass `--profile <name>` to `harvest`/`serve` and diff `config_summary`'s `caps`/before-vs-after — `sources` and `days_back` must be unchanged (§4's "cannot touch" list); `caps` and (indirectly, via a harvest run) `web.selected_names` must reflect the profile.
