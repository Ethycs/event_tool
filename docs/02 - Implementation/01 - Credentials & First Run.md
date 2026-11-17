# Credentials & First Run

**Status:** CURRENT · **Date:** 2026-07-29 · **Applies to:** per-service credential setup, first run, and reset, on the local workstation

All values below come from `.env.example` and the source that reads them — never from `.env` itself. Copy the scaffold first if you haven't:

```powershell
Copy-Item .env.example .env
```

## 1. Service inventory

| Service | Env vars | Where to get them | First run | Cached credential file |
|---|---|---|---|---|
| Telegram (MTProto) | `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_PHONE`, `TELEGRAM_SESSION` | [my.telegram.org](https://my.telegram.org) → API Development Tools | `interactive — cannot be automated` (SMS/app OTP, once) | `<TELEGRAM_SESSION>.session` at repo root (default `harvest_session.session`) |
| Gmail | `GMAIL_CREDENTIALS_FILE`/`GMAIL_CREDENTIALS_JSON`, `GMAIL_TOKEN_FILE`/`GMAIL_TOKEN_JSON` | Google Cloud Console → APIs & Services → Credentials (OAuth client, Desktop app type) | `interactive — cannot be automated` (browser OAuth consent, once; auto-refreshes silently after) | `token.json` at repo root (or `GMAIL_TOKEN_JSON`, base64) |
| Discord | `DISCORD_CACHE_PATH` (optional override) | n/a — no token, no login flow in this tool | none in-tool, but **requires** the Discord desktop client already installed, logged in, and having opened the channels you want harvested | none written by this tool — reads the Electron client's own cache in place |
| Signal | `SIGNAL_DB_PATH` (optional override) | n/a — no token | none in-tool, but **requires** Signal Desktop already installed and logged in | none written by this tool — reads Signal Desktop's own encrypted DB in place |
| TickTick | `TICKTICK_CLIENT_ID`, `TICKTICK_CLIENT_SECRET`, `TICKTICK_REDIRECT_URI`, `TICKTICK_USERNAME`, `TICKTICK_PASSWORD`, `TICKTICK_PROJECT` (optional) | [developer.ticktick.com](https://developer.ticktick.com) → Manage Apps → + App Name; redirect URL `http://127.0.0.1:8080` | `interactive — cannot be automated` (browser consent **plus** manually pasting the redirected URL back into the terminal — see §2.5) | `data/.token-oauth` — gitignored; pinned explicitly since 2026-07-29, see §3 |
| OpenAI | `OPENAI_API_KEY` | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) | none — plain key | n/a (env var only) |
| Anthropic | `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) | none — plain key, used as fallback when `OPENAI_API_KEY` is unset (`config.py` `load_config()`) | n/a (env var only) |

`LLM_BACKEND=litellm` (default) picks whichever of the two keys above is set; `LLM_MODEL` selects the model in litellm format (`gpt-4o-mini`, `anthropic/claude-3.5-haiku`, ...). The `local` backend (RyzenAI NPU/GPU/CPU) is a separate path — see the commented block in `.env.example` — and isn't covered here since it has no external credential.

## 2. Per-service procedure

### 2.1 Telegram

1. Set `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_PHONE` in `.env`.
2. Run the standalone auth script (this is the intended one-time path — `scripts/auth_telegram.py`):
   ```powershell
   pixi run python scripts/auth_telegram.py
   ```
   `interactive — cannot be automated`: Telethon prompts for the SMS/app OTP code (and a 2FA password if you have one enabled) on stdin.
3. On success it prints `Session saved to <TELEGRAM_SESSION>.session` — that file (default `harvest_session.session`) now lives at the repo root and is reused by every subsequent `event-harvester` invocation (`config.py` `TelegramConfig.session` → `TelegramClient(session, ...)`, resolved relative to CWD).
4. **Reset:** delete `harvest_session.session` and rerun step 2. `TelegramConfig.is_configured` only checks `api_id`/`api_hash` are set — an invalid/expired session surfaces as a runtime auth error on the next harvest, not a config warning.

### 2.2 Gmail

1. In Google Cloud Console, create an OAuth client (Desktop app), download it as `credentials.json`.
2. Either:
   - **File path (default):** drop `credentials.json` at the repo root — matches `GmailConfig.credentials_file` default.
   - **Env, no loose files:** base64-encode it into `GMAIL_CREDENTIALS_JSON` (`.env.example` has the one-liner: `python -c "import base64; print(base64.b64encode(open('credentials.json').read().encode()).decode())"`).
3. First call that needs Gmail (`sources/gmail.py` `_get_credentials`) triggers `InstalledAppFlow.run_local_server(port=0)`. `interactive — cannot be automated`: opens a browser tab for OAuth consent once. **This is gated on a TTY** (as of 2026-07-30) — a browser prompt is a recovery at a terminal and a hang under the stdio MCP server or a scheduled harvest, so without an attached terminal it logs what to do and returns `None`, and Gmail skips for that run. `GMAIL_OAUTH_INTERACTIVE=1` forces it.
4. Token is written to `token.json` at the repo root (`GmailConfig.token_file` default) regardless of whether you supplied credentials via file or `GMAIL_CREDENTIALS_JSON` — the flow always persists to the file path, not back to an env var. Subsequent runs try `GMAIL_TOKEN_JSON` first, then `token.json`, and silently refresh via the stored refresh token.
   **Each candidate is tried to completion, refresh included, before the next.** That ordering is load-bearing and was wrong until 2026-07-30: a stale `GMAIL_TOKEN_JSON` that merely *parsed* used to block `token.json` from being read at all, so a dead env token took the whole source down while a working credential sat on disk. If you keep both, expect a `could not be refreshed — trying the next source` warning per call whenever the env copy goes stale; that is the fallback working, but it costs a round-trip every time, so refresh or drop the stale one.
5. **Reset:** delete `token.json` (and unset `GMAIL_TOKEN_JSON` if you'd set one) and rerun; step 3 repeats.
6. **Search window:** the harvest derives `after:<date>` from `--days-back`. `GMAIL_QUERY` adds terms ANDed with it, and defaults to empty. Do not put a `newer_than:` in there unless you mean it to override `--days-back` — that is exactly what the old hardcoded `newer_than:7d` default did, silently capping every run at a week.

### 2.3 Discord

No token, no OAuth. `sources/discord.py` reads directly from the Electron client's on-disk HTTP cache via `ccl_chromium_reader`, detecting BlockFile (Windows) or SimpleFile (Linux) format. Current clients cache no message JSON, so live Discord data comes from the scheduled-events plugin in `integrations/discord-betterdiscord-plugin/`.

1. Prerequisite outside this tool: have the Discord desktop app installed, logged in, and have actually opened the channels you want harvested (the cache only contains what the client has fetched — gaps are possible).
2. `find_discord_cache()` auto-detects `%APPDATA%/discord/Cache/Cache_Data` on native Windows, or the WSL-mounted equivalent under `/mnt/c/Users/<you>/...` when running from WSL. Override with `DISCORD_CACHE_PATH` if it picks the wrong profile or you're on a nonstandard install.
3. If Discord is running and holding a lock on the cache, `read_discord_messages` copies it to a temp dir first — no need to close Discord.
4. **Reset:** nothing to reset — there's no cached credential, just the live Discord client state.

### 2.4 Signal

No token. `sources/signal.py` opens Signal Desktop's SQLCipher database directly, extracting the encryption key via `sigexport.crypto.get_key` (DPAPI on Windows, Keychain on macOS, equivalent on Linux).

1. Prerequisite outside this tool: Signal Desktop installed and logged in (`%APPDATA%/Signal/sql/db.sqlite` on Windows must exist).
2. Auto-detected; override with `SIGNAL_DB_PATH` if needed. `SignalConfig.is_configured` always returns `True` — Signal is treated as "configured" by presence, so a missing/unreadable DB fails at harvest time with a logged error, not a startup warning.
3. The override env var is `SIGNAL_DB_PATH` (`config.py:281`). *Fixed 2026-07-29: the not-found warning in `sources/signal.py:23` used to print `"SIGNAL_DATA_PATH=%s does not exist"` — a variable name that appears nowhere else in the codebase — which sent anyone debugging a bad override looking for the wrong thing. It now prints `SIGNAL_DB_PATH`.*
4. **Reset:** nothing to reset — no cached credential, just the live Signal Desktop state.

### 2.5 TickTick

1. Set `TICKTICK_CLIENT_ID`, `TICKTICK_CLIENT_SECRET`, `TICKTICK_REDIRECT_URI` (`http://127.0.0.1:8080`, matching the app's registered redirect), plus your normal `TICKTICK_USERNAME`/`TICKTICK_PASSWORD`.
2. First call to `outputs/ticktick.py` `get_ticktick_client()` constructs a `ticktick.oauth2.OAuth2`, which (per the vendored `ticktick-py` package, `.pixi/envs/default/Lib/site-packages/ticktick/oauth2.py`) does the following if no valid cached token exists:
   - Opens your default browser to TickTick's authorize URL (`webbrowser.open`).
   - **Then prompts on stdin**: `"Enter the URL you were redirected to: "` — you must copy the full redirected URL (including the `code=...&state=...` query string) from the browser's address bar and paste it back into the terminal. `interactive — cannot be automated`, and more manual than a typical "click Allow" OAuth flow — there is no local callback listener despite the redirect URI being `127.0.0.1:8080`.
3. The resulting token dict is written by `CacheHandler` to **`data/.token-oauth`**. The library's default is `.token-oauth` relative to the process's CWD — i.e. wherever `event-harvester` was launched from — so `get_ticktick_client()` passes an explicit `cache_path` anchored to the repo root (fixed 2026-07-29). It also calls `_migrate_legacy_token_cache()` first, which moves a pre-fix root-level `.token-oauth` into `data/` once so you are not re-prompted. `unverified`: no TickTick credentials exist in this checkout, so neither the pinned path nor the migration has been exercised end-to-end.
4. **Reset:** delete `data/.token-oauth` and rerun; step 2 repeats. The token has no refresh token in this flow (`ticktick-py`'s `validate_token` just re-requests a full new token via the same interactive prompt once `expire_time` passes), so expect to redo this periodically, not just once per machine.

### 2.6 OpenAI / Anthropic

No first-run interaction. Set `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` in `.env`; `config.py` `load_config()` prefers `OPENAI_API_KEY` and falls back to `ANTHROPIC_API_KEY` (also used for `sk-ant-` auto-detection when picking a default vision model — see `VLM_MODEL` in `README.md` "Web debugging features").

## 3. What lives at the repo root and must never be committed

Cross-referencing `.gitignore` against every credential file this doc names:

| File | Written by | In `.gitignore`? |
|---|---|---|
| `.env` | you (from `.env.example`) | Yes — `.env` |
| `harvest_session.session` (or `$TELEGRAM_SESSION.session`) | `scripts/auth_telegram.py` / Telethon | Yes — `*.session` |
| `credentials.json` | you (Google Cloud Console download) | Yes — `credentials.json` |
| `token.json` | Gmail OAuth flow | Yes — `token.json` |
| `client_secret_*.json` | (alternate Google download naming) | Yes — `client_secret_*.json` |
| `data/.token-oauth` | TickTick OAuth flow (`ticktick-py`'s `CacheHandler`) | Yes — `.token-oauth` (no leading slash, so it matches at any depth), and `data/*` covers it too |

**~~BUG~~ — fixed 2026-07-29, in two layers.** `.token-oauth` — the TickTick access-token cache — had no `.gitignore` entry. It did not exist in this checkout, but the first TickTick OAuth run from the repo root would have dropped a live access token there as an untracked, **unignored** file, and `git add -A` would have staged a real credential. Both layers are now in place: `.gitignore` matches `.token-oauth` at any depth (the guard), and `outputs/ticktick.py` passes an explicit `cache_path` under `data/` so it never lands at the repo root in the first place (the fix). Keep both — the ignore rule still protects anyone running an older checkout or a differently-configured client.

No other credential-bearing file was found untracked-but-unignored — `data/*` (with the `web_sources.json` carve-out) covers `data/.playwright_state.json`, `data/.chrome_profile/`, and the labeled-message caches; `.ssh/` is separately ignored.

## Verification

```powershell
pixi run python scripts/mcp_probe.py call config_summary
```

Success looks like a JSON/text summary listing each source (`telegram`, `discord`, `gmail`, `signal`, `ticktick`, `llm`) with a ready/not-ready state reflecting exactly the files and env vars set up above — no live harvest, no TickTick task creation, safe to run repeatedly while setting up credentials.
