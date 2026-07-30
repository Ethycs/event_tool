# Sources — Messaging (non-web)

**Status:** CURRENT · **Date:** 2026-07-29 · **Applies to:** `src/event_harvester/sources/__init__.py`, `src/event_harvester/sources/discord.py`, `src/event_harvester/sources/telegram.py`, `src/event_harvester/sources/gmail.py`, `src/event_harvester/sources/signal.py`, `src/event_harvester/sources/chromium_cache.py`, `scripts/auth_telegram.py`, `tests/test_discord.py` · **Depends on:** `src/event_harvester/config.py`, `src/event_harvester/pipeline/harvest.py`

Four independent message sources — Discord, Telegram, Gmail, Signal — each emit a list of message dicts in a loosely shared schema that `pipeline/harvest.py` gathers concurrently and concatenates. A fifth module, `chromium_cache.py`, is a generic Chromium-cache reader that is exported from the package but, as far as this repo's own code goes, has no caller — Discord has its own duplicate cache-reading logic instead. The five modules disagree on what `channel`, `author`, and `is_sent`/`pinned`-style flags mean, which matters because everything downstream (`filter_read_sent`, the LLM extractor) reads the merged list as if it were one schema.

## Hard constraints

1. Discord messages only exist in the harvester's view if the Discord desktop/web client actually rendered them recently — this is Chromium's HTTP cache, not a message store, and is subject to Discord's own cache eviction regardless of `DAYS_BACK`.
2. Telegram requires a pre-existing `.session` file, or an interactive phone+code login via `scripts/auth_telegram.py` run first — `TelegramClient.start()` blocks on stdin if no session exists and none is supplied.
3. Gmail requires either a valid `token.json`/`GMAIL_TOKEN_JSON` with a refresh token, or a one-time interactive OAuth consent flow (`InstalledAppFlow.run_local_server`) that opens a local browser — this cannot complete headless, and since 2026-07-30 it is **not attempted** headless: the flow is gated on `sys.stdin.isatty()` or `GMAIL_OAUTH_INTERACTIVE=1`, so a credential-less run skips Gmail rather than blocking on a consent click nobody will make.
4. Signal Desktop must be installed and already linked/synced so `%APPDATA%\Signal\sql\db.sqlite` (or the macOS/Linux equivalents) exists; key extraction goes through `sigexport.crypto.get_key`, which is itself platform-specific (Windows DPAPI, macOS Keychain, Linux libsecret).
5. All four sources are given one shared UTC cutoff, `datetime.now(utc) - timedelta(days=cfg.days_back)` (`DAYS_BACK` env, default 7), computed once in `pipeline/harvest.py:harvest_messages`. No source module reads `DAYS_BACK` itself.

## Source comparison

| Source | Auth mechanism | On-disk / env dependency | Locked-resource handling | Failure mode |
|---|---|---|---|---|
| Discord (`discord.py`) | None — reads local Chromium HTTP cache, no login | `%APPDATA%\discord\Cache\Cache_Data` (Windows); `/mnt/c/Users/*/AppData/Roaming/discord/Cache/Cache_Data` (WSL); `~/.config/discord/Cache/Cache_Data` (fallback, see BUG below); optional `DISCORD_CACHE_PATH` override | Copies the whole cache dir to a temp dir before parsing, specifically to dodge the lock held by a running Discord client | Cache dir not found → logs info, returns `[]` (silent, not an error) |
| Telegram (`telegram.py`) | MTProto session (Telethon) | `TELEGRAM_API_ID`/`TELEGRAM_API_HASH`/`TELEGRAM_PHONE` env vars + `<TELEGRAM_SESSION>.session` file at repo root (`harvest_session.session` by default, confirmed present in this checkout) | N/A (network API, no local DB lock) | Per-dialog exceptions (flood-wait, restricted dialogs) are caught and logged, that dialog is skipped — partial results, not a hard failure |
| Gmail (`gmail.py`) | OAuth2, `gmail.modify` + `gmail.compose` scopes | Priority order: `GMAIL_TOKEN_JSON` env (base64 JSON) → `token.json` file → `GMAIL_CREDENTIALS_JSON` env / `credentials.json` file for a fresh `InstalledAppFlow`. Each stored token is tried **to completion, refresh included**, before the next — until 2026-07-30 a stale env token that merely parsed blocked the file from being read at all | N/A | No credentials found → logs info, returns `[]`; a token that cannot be refreshed logs a warning and moves to the next source; all tokens dead → interactive OAuth **only with a TTY** (or `GMAIL_OAUTH_INTERACTIVE=1`), else logs and returns `None` |
| Signal (`signal.py`) | OS-native key extraction (`sigexport.crypto.get_key`) unlocking a SQLCipher DB | `%APPDATA%\Signal\sql\db.sqlite` (Windows), `~/Library/Application Support/Signal/...` (macOS), `~/.config/Signal/...` (Linux); optional `SIGNAL_DB_PATH` override (the only name — `SIGNAL_DATA_PATH` appeared once, in a warning string, and was corrected 2026-07-29) | **None** — connects directly to `db.sqlite` with no temp-copy step, unlike Discord/`chromium_cache.py` (known rough edge, see below) | Key extraction or DB open failure → logs error, returns `[]` |
| `chromium_cache.py` (generic) | None | `find_chrome_cache`/`find_edge_cache` under `%LOCALAPPDATA%\...\Cache\Cache_Data` (Windows), plus a Darwin branch for Chrome only | Copies cache dir to temp before parsing (same pattern as `discord.py`) | Not wired to any pipeline caller in this repo — see dead-code note below |

## `discord.py`

**Domain:** Local Chromium BlockFile HTTP-cache scraping for Discord's own web client (Discord's desktop app is Electron/Chromium under the hood, so its message-fetch responses land in the same on-disk cache format Chrome uses).
**Runs at:** Sync function, invoked from `pipeline/harvest.py:_run_discord` via `asyncio.to_thread(read_discord_messages, ...)` — off the event loop, in a worker thread.
**Inputs:** No credentials. `cache_path` (optional direct `Path`), `override_path` (str, from `DISCORD_CACHE_PATH` env via `cfg.discord.cache_path`), `cutoff: datetime`.
**Outputs:** message dicts, see schema table below.

### Internal structure

- `find_discord_cache()` — platform dispatch: WSL (`Linux` + `/mnt/c/Users` exists) → `_find_cache_wsl()` scans every user directory under `/mnt/c/Users` (skipping `Public`/`Default`/`Default User`/`All Users`) for `AppData/Roaming/discord/Cache/Cache_Data`; Windows → `%APPDATA%\discord\Cache\Cache_Data`; else → `~/.config/discord/Cache/Cache_Data`.
- `_decompress()` — tries gzip magic bytes, then raw zlib, then raw-deflate (`-zlib.MAX_WBITS`), then gives up and returns the input unchanged.
- `_parse_message_blobs()` — finds the first `[` or `{` in the decoded text (skips leading HTTP-cache header junk), `json.loads`s from there, and keeps only dict entries that have both `id` and `content` keys. Covered by `tests/test_discord.py` (see below).
- `read_discord_messages()` — copies the whole cache dir to `tempfile.mkdtemp()` (avoids the lock a running Discord process holds on the live cache), opens it with `ccl_chromium_reader.ChromiumBlockFileCache`, iterates all cache keys, filters URLs matching `_MSG_URL_RE = r"/channels/(\d+)/messages"`, decompresses + parses each cache entry's buffers, dedupes by message `id`, applies the `cutoff` filter, and always removes the temp dir in a `finally` block.

### Hard constraints / failure modes

- **BUG:** `find_discord_cache()`'s non-Windows, non-WSL fallback branch (line ~45-48) uses `~/.config/discord/...` (the Linux path) for *any* other platform, including macOS — there is no Darwin-specific branch, unlike the sibling `chromium_cache.py:find_chrome_cache`, which does special-case `Darwin`. On a real Mac this will look in the wrong directory and silently return `None`.
- **known rough edge:** the module duplicates `_decompress`/cache-copy/BlockFileCache-iteration logic that already exists, more generically, in `chromium_cache.py` (`decompress`, `read_cache_entries`). The two implementations have already drifted (different URL-pattern handling, different platform coverage) — a bug fixed in one will not propagate to the other.
- Malformed cache entries (bad JSON, decode errors) are swallowed per-message inside a broad `except Exception: continue` — failures here are invisible unless you add logging.
- `tests/test_discord.py` only exercises the pure function `_parse_message_blobs` (8 cases: array, single dict, mixed-type array, empty/None body, invalid JSON, missing-`id`, leading whitespace, UTF-8 content). **known rough edge:** `find_discord_cache`, `_find_cache_wsl`, `_decompress`, and the `ChromiumBlockFileCache` integration in `read_discord_messages` are entirely untested.

## `telegram.py`

**Domain:** Telegram MTProto client (Telethon) — reads real-time API data, not a local cache.
**Runs at:** Native `async` coroutine, awaited directly on the harvester's event loop (not thread-offloaded) from `pipeline/harvest.py:_run_telegram`.
**Inputs:** `cfg: TelegramConfig` (`api_id`, `api_hash`, `phone`, `session` — from `TELEGRAM_API_ID`/`TELEGRAM_API_HASH`/`TELEGRAM_PHONE`/`TELEGRAM_SESSION` env vars, default session name `"harvest_session"`), `cutoff`, optional `channels_allowlist`/`channels_blocklist` (from `TELEGRAM_CHANNELS`/`TELEGRAM_EXCLUDE`), optional pre-built `client` (used by `watch.py`'s persistent-connection mode so the connection survives across polling cycles).
**Outputs:** message dicts, see schema table.

### Internal structure

- `_should_include_dialog()` — case-insensitive substring match: allowlist wins if non-empty, else blocklist excludes, else everything passes.
- `_msg_to_dict()` — only converts `telethon.tl.types.Message` instances with non-empty `.text` (media-only messages with no caption are dropped entirely — silent, no counter for how many were skipped). Author resolution: `sender.username` → `sender.first_name` → `"unknown"`.
- `read_telegram_messages()`:
  - If `cfg.is_configured` is false (`api_id`/`api_hash` unset), logs and returns `[]` immediately — no exception.
  - Owns and disconnects its own client only if the caller didn't pass one in (`own_client = client is None`), so `watch.py`'s persistent client is never torn down between polls.
  - For each dialog passing the allow/block filter: (1) if `include_pinned` (default `True`), fetches up to 10 pinned messages via `InputMessagesFilterPinned`, **regardless of `cutoff`** — old pinned messages always come through; (2) fetches recent messages via `iter_messages(offset_date=now, reverse=False, limit=None)`, relying on Telegram returning messages newest-first per dialog, and **`break`s the loop** the instant it hits a message older than `cutoff` — this assumes strict chronological ordering per dialog and no `limit`, so a single very active dialog can iterate arbitrarily far back in one call before it happens to cross the cutoff.
  - Per-dialog exceptions (pinned-fetch or main-fetch) are caught, logged, and that dialog is skipped — one broken dialog does not fail the whole harvest.

### Hard constraints / failure modes

- First run (or a deleted/expired session) causes `client.start(phone=...)` to prompt interactively for the login code on stdin — this will hang a non-interactive/CI run. `scripts/auth_telegram.py` exists specifically to do this once, ahead of time.
- No rate-limit backoff logic beyond Telethon's own default flood-wait handling; a `FloodWaitError` on one dialog is simply logged and that dialog's results are dropped for this run.

## `gmail.py`

**Domain:** Gmail REST API via `google-api-python-client`. Also the only messaging source with write actions (`reply`, `trash`, `delete`, `mark_read`, `mark_unread`).
**Runs at:** Sync functions; `fetch_messages` is called via `asyncio.to_thread` from `pipeline/harvest.py:_run_gmail`. `reply`/`trash` are called synchronously and directly from `outputs/obsidian.py` (user-approval flow), not from the async harvest path.
**Inputs:** `cfg: GmailConfig` (`credentials_file` default `credentials.json`, `token_file` default `token.json`, `query` default `""` from `GMAIL_QUERY`, `max_results` default 200 from `GMAIL_MAX_RESULTS` — the default was `"newer_than:7d"` until 2026-07-30, which won its AND against the `after:` filter and capped every run at seven days regardless of `--days-back`) plus env vars `GMAIL_TOKEN_JSON` / `GMAIL_CREDENTIALS_JSON` (base64-encoded JSON, preferred — "no loose files") and `GMAIL_CREDENTIALS_FILE`/`GMAIL_TOKEN_FILE`. `token.json` exists at repo root in this checkout (git-ignored).
**Outputs:** message dicts, see schema table; `fetch_full_bodies` returns a separate `dict[message_id, body_text]` (not part of the standard message shape).

### Internal structure

- `_get_credentials()` — priority chain: (1) `GMAIL_TOKEN_JSON` env, base64→JSON→`Credentials.from_authorized_user_info`; (2) `token.json` file; if still invalid but has a refresh token, `creds.refresh(Request())` and persist back to `token_file` (note: refreshing an env-sourced token still gets written to the *file* path, not back to the env var — on a redeploy from env vars only, this refreshed token is lost unless something re-reads the file into the env); (3) fresh interactive OAuth via `GMAIL_CREDENTIALS_JSON` env (written to a temp file for `InstalledAppFlow`, deleted after) or `credentials.json` file, using `flow.run_local_server(port=0)` — opens a local browser, requires a display.
- `fetch_messages()` — builds query as `f"{cfg.query} after:{since:%Y/%m/%d}"`, paginates in batches of ≤100 up to `cfg.max_results`, and per message calls `messages().get(format="metadata")` (headers + snippet only, **not the full body**). `content` is `f"{subject}\n{snippet}"` — the snippet is Gmail's short preview text, not the full email.
- `channel` is derived by picking the first label on the message that isn't a system prefix (`CATEGORY_`, `UNREAD`, `IMPORTANT`, `SENT`, `DRAFT`), falling back to `"INBOX"` — this is a Gmail *label*, semantically unlike every other source's `channel` (which means a chat/room name).
- `filter_read_sent()` — module-level filter (not called inside `fetch_messages`; called separately by `pipeline/orchestrator.py` after all sources are merged) that drops Gmail messages with `is_sent=True` or `is_read=True`, and passes every non-Gmail message through untouched.
- `fetch_full_bodies()` — separate on-demand call (`format="full"`) used by `cli/commands/recruiters.py`; recursively walks MIME parts, prefers `text/plain`, falls back to HTML-with-tags-stripped via regex.
- `reply()`/`trash()`/`delete()`/`mark_read()`/`mark_unread()` — straightforward wrappers; `reply` threads via `In-Reply-To`/`References` headers pulled from the original message's metadata.

### Hard constraints / failure modes

- No credentials at all → `_get_service` returns `None` → every function short-circuits to `[]`/`False`/`None`, all logged at `info`/`warning`, never raised.
- **unverified:** whether a fully headless run (no `token.json`, no env token, `credentials.json` present) actually fails cleanly or hangs on `run_local_server` — the code will attempt to open a browser and bind a local port regardless of TTY availability; not exercised by any test in this repo.

## `signal.py`

**Domain:** Signal Desktop's local encrypted SQLite (SQLCipher) database, read directly (no Signal API involved).
**Runs at:** Sync function, via `asyncio.to_thread(read_signal_messages, ...)` from `pipeline/harvest.py:_run_signal`.
**Inputs:** No API credentials — pulls the local SQLCipher decryption key via `sigexport.crypto.get_key(signal_dir, None)`, which itself is platform-specific (Windows DPAPI). `override_path` from `cfg.signal.db_path` (`SIGNAL_DB_PATH` env — despite the env var name, this overrides the *directory* passed to `_find_signal_dir`, not a literal `db.sqlite` file path).
**Outputs:** message dicts, see schema table.

### Internal structure

- `_find_signal_dir()` — checks `override_path` first, else Windows (`%APPDATA%\Signal`), macOS (`~/Library/Application Support/Signal`), Linux (`~/.config/Signal`), each gated on `(candidate / "sql" / "db.sqlite").exists()`.
- `_connect_db()` — lazy-imports `sigexport.crypto` and `sqlcipher3.dbapi2` (both present in `.pixi/envs/*/Lib/site-packages` in this checkout, pulled in transitively via the `signal-export` PyPI package declared in `pyproject.toml`), extracts the key, opens the DB with `PRAGMA KEY`, `cipher_page_size=4096`, `kdf_iter=64000`, `HMAC_SHA512`/`PBKDF2_HMAC_SHA512` — these PRAGMA values must match whatever Signal Desktop itself used to create the DB; **unverified against Signal Desktop version drift** (Signal has changed SQLCipher parameters across major versions historically).
- `read_signal_messages()` — loads all `conversations` (id → display name, `private` vs `group`) into memory first, then `SELECT ... FROM messages WHERE sent_at > ? AND body IS NOT NULL AND body != ''` (strict `>`, not `>=`, unlike every other source's inclusive-cutoff semantics — a message landing on the exact cutoff millisecond is dropped; negligible in practice but a real inconsistency). Author: `"You"` for `type == "outgoing"`, else `source` column or the conversation name.

### Hard constraints / failure modes

- **known rough edge — locked DB:** unlike `discord.py` and `chromium_cache.py`, this module does **not** copy the database to a temp location before opening it. If Signal Desktop is running and holds the DB open (SQLite default journal mode can tolerate concurrent readers, but WAL-mode or an in-progress write can still produce `database is locked`), `_connect_db`/the subsequent `SELECT` can fail. There is no retry/copy fallback — a locked DB surfaces as a caught exception, logged, and an empty result, indistinguishable in the logs from "Signal not installed."
- Key-extraction failure (`crypto.get_key` returns falsy) raises `RuntimeError`, caught by the caller's `try/except`, logged as an error, returns `[]`.
- `d["is_sent"] = True` is only set for outgoing messages — it is *absent* (not `False`) for incoming ones. Nothing in this codebase filters on Signal's `is_sent` the way `filter_read_sent` filters Gmail's — your own outgoing Signal messages flow straight through to LLM event extraction. **This is a real cross-source divergence** (see schema table below).

## `chromium_cache.py`

**Explicit determination:** this module does **neither** a Discord-specific read **nor** browser-cookie extraction for the web path. It is a *generic* Chromium **BlockFile HTTP-cache** reader (same on-disk cache format Discord's Electron shell uses, and the same `ccl_chromium_reader.ChromiumBlockFileCache` backend `discord.py` uses) intended to pull cached JSON API responses or rendered HTML pages out of Chrome's/Edge's own browsing cache, keyed by a URL regex — e.g., pulling a cached Eventbrite/Luma API response or page body without re-fetching it. It contains **no cookie-jar code at all** (`grep -i cookie` over the file returns nothing) and is not Discord-specific (it has no `/channels/.../messages` URL matching — that logic lives only in `discord.py`).

**Domain:** Generic Chromium cache scraping utility (cache discovery + decompression + JSON/HTML extraction + a bespoke MinHash text classifier).
**Runs at:** Sync, thread-safe by construction (copies to temp before parsing) — but see dead-code note.
**Inputs:** `cache_path: Path`, `url_pattern` (regex), plus `find_chrome_cache`/`find_edge_cache` do their own env-based discovery (`%LOCALAPPDATA%` on Windows; `~/Library/Application Support/Google/Chrome` on Darwin — Edge has **no** Darwin/Linux branch, Windows-only).
**Outputs:** message dicts (via `read_cache_messages`, field-mapping-configurable) or page-as-message dicts (via `read_cached_pages`).

### Internal structure

- `find_chrome_cache`/`find_edge_cache`/`find_browser_cache` — path discovery per platform (Chrome: Windows/Darwin/Linux; Edge: Windows only).
- `decompress`/`extract_json` — near-duplicates of `discord.py`'s `_decompress`/`_parse_message_blobs`, but generalized (no `id`+`content` requirement, accepts any dict).
- `is_readable_text()` + a hand-rolled trigram-MinHash classifier (`_trigrams`, `_minhash_signature`, `_jaccard_from_signatures`, six hardcoded reference strings) — a lightweight natural-language-vs-code/binary gate used inside `read_cached_pages`'s JSON branch.
- `read_cache_entries()` — copies cache dir to temp, opens with `ChromiumBlockFileCache`, filters cache keys by `url_pattern`, decompresses + JSON-extracts each buffer, returns `(url, [json_objects])` tuples.
- `read_cache_messages()` — wraps `read_cache_entries` with configurable field-name mapping (`id_field`, `timestamp_field`, `author_field` — supports nested tuple keys via `_get_nested`, `channel_field`, `content_field`) to produce standard message dicts, applying the same `cutoff` semantics as the other sources (`ts < cutoff: continue`, i.e. inclusive).
- `read_cached_pages()` — the "any webpage → message" path: for each matching cache entry, tries JSON first (gated through `is_readable_text`), then falls back to HTML text extraction (`extract_text_from_html`, strips `<script>`/`<style>` and tags) gated through **`gzip_event_score(text) > gzip_threshold`**.

### Hard constraints / failure modes

- **BUG:** `read_cached_pages`'s HTML fallback branch (around `chromium_cache.py:468`) calls `gzip_event_score(text)` and references `gzip_threshold` — **neither name is defined anywhere in this file**, nor imported. Any call path that reaches the HTML-extraction branch (i.e., any cached response that isn't valid JSON) will raise `NameError` at runtime. Only caught by the enclosing broad `except Exception: continue` at the per-buffer level, so it fails silently as a skipped page rather than crashing the run — but it means **the HTML-fallback path of `read_cached_pages` has never actually returned an HTML-derived message**, only JSON-derived ones (or it always no-ops on HTML).
- **dead code?** None of `find_browser_cache`, `find_chrome_cache`, `find_edge_cache`, `read_cache_entries`, `read_cache_messages`, `read_cached_pages` are referenced anywhere in `src/event_harvester` outside of `sources/__init__.py`'s re-export (confirmed via repo-wide grep). They are not called by `web_fetch.py`, `discord.py`, or any pipeline/CLI module found. This whole module appears to be exported API surface with no current caller — either scaffolding for a not-yet-wired feature, or a superseded predecessor to `discord.py`'s own (duplicated) cache-reading code.
- `read_cached_pages` fabricates `id` as `f"web:{hash(url + text[:100])}"` — Python's built-in `hash()` is **not stable across process restarts** (`PYTHONHASHSEED` randomization applies to strings by default), so these IDs cannot be used for dedup across separate runs, only within a single run's `seen_urls` set.
- `timestamp` in `read_cached_pages` output is `datetime.now().isoformat()` — **capture time, not message/page time** — see schema divergence below.

## `scripts/auth_telegram.py`

One-time interactive script. Loads `.env` via `python-dotenv`, reads `TELEGRAM_API_ID`/`TELEGRAM_API_HASH`/`TELEGRAM_PHONE`/`TELEGRAM_SESSION` (same env vars as `config.py:TelegramConfig`, default session name `"harvest_session"`), and calls `client.start(phone=phone)`, which prompts on stdin for the login code (and 2FA password if enabled) the first time. On success, Telethon writes `<TELEGRAM_SESSION>.session` (SQLite file) to the current working directory — `harvest_session.session` exists at the repo root in this checkout, confirming this script (or an equivalent manual login) has already been run here. This file is git-ignored (`*.session` in `.gitignore`) and must be run once, ahead of any headless/automated harvest, or `read_telegram_messages`/`create_persistent_client` will hang waiting for interactive input.

## Message dict schema — cross-source comparison

All four production sources (Discord, Telegram, Gmail, Signal) share the base shape `{platform, id, timestamp, author, channel, content}`. Fields beyond that diverge:

| Field | Discord | Telegram | Gmail | Signal | `chromium_cache` (generic) |
|---|---|---|---|---|---|
| `platform` | `"discord"` | `"telegram"` | `"gmail"` | `"signal"` | caller-supplied string |
| `id` | Discord snowflake (str) | Telegram message ID (str) | Gmail message ID (str) | Signal DB row `id` (str) | `read_cache_messages`: source's own ID field; `read_cached_pages`: **synthetic, non-stable** `hash()`-based string |
| `timestamp` | message's own ISO timestamp | message's own date (UTC) | `Date` header, parsed (falls back to raw header string on parse failure, or `""` if missing) | `sent_at` (ms epoch → UTC ISO) | `read_cache_messages`: source's own field; `read_cached_pages`: **`datetime.now()` — capture time, not authored time** |
| `author` | Discord `username` | Telegram `username`/`first_name`/`"unknown"` | raw `From:` header (`"Name <addr>"`, unparsed) | `"You"` (outgoing) or `source`/conv. name (incoming) | domain (`read_cached_pages`) or mapped field (`read_cache_messages`) |
| `channel` | numeric `channel_id` (str) | dialog title | **Gmail label** (e.g. a custom label, or `"INBOX"`) — semantically not a chat room | conversation display name | URL (truncated to 120 chars) or mapped field |
| `content` | full message text | full message text (`msg.text`) | **`"{subject}\n{snippet}"`** — truncated preview, not the full body (full body requires a separate `fetch_full_bodies` call) | full message body | JSON-dumped object (2000 chars) or extracted HTML text (2000 chars) |
| `pinned` | present (`True`) only if pinned; absent otherwise | present (`True`) only if pinned; absent otherwise | — (not emitted) | — (not emitted) | — |
| `mention_everyone` | present (`True`) only if set; absent otherwise | — | — | — | — |
| `thread_id` | — | — | **always present** (str, possibly empty) | — | — |
| `is_read` | — | — | **always present** (bool) | — | — |
| `is_sent` | — | — | **always present** (bool) | present (`True`) only if outgoing; **absent** for incoming | — |

**Divergence hazards for anything consuming the merged list:**

- `channel` means three different things (chat/dialog name for Discord/Telegram/Signal, a Gmail *label* for Gmail, a URL for the generic cache reader) — any downstream grouping/report "by channel" silently mixes incompatible concepts.
- `content` truncation is inconsistent: Gmail's default `content` is a short snippet unless a caller separately invokes `fetch_full_bodies`; every other source's `content` is the full message text. A length- or keyword-based filter tuned against Discord/Telegram/Signal content will behave very differently on Gmail.
- `is_sent`/`pinned`/`mention_everyone`/`thread_id`/`is_read` follow two different conventions: Gmail always sets its booleans explicitly (`True`/`False`); Discord/Telegram/Signal use "key present ⇒ true, key absent ⇒ false/unknown." Code using `.get(key)` (as `filter_read_sent` and `is_sent` checks do today) is safe either way, but `"key" in msg` would not be.
- `filter_read_sent()` only inspects `platform == "gmail"`; Signal's `is_sent` flag is real data but nothing reads it — your own outgoing Signal messages are not excluded from downstream LLM event extraction, unlike your own sent Gmail.
- `read_cached_pages`'s `timestamp` is harvest-time, not message-time — if this module's output were ever wired into a caller that also passes it through the standard `cutoff` filtering pattern used elsewhere (`ts < cutoff: continue`), it would trivially always pass, defeating the point of a days-back window.

## Verification

```
pixi run -e dev pytest tests/test_discord.py tests/test_gmail_credentials.py tests/test_chromium_cache.py -v
```

`test_discord.py` covers the 8 `_parse_message_blobs` cases (array/single/mixed-type parsing, empty/invalid input, missing-`id` rejection, whitespace and UTF-8 handling). It does **not** touch cache discovery, decompression, or the `ChromiumBlockFileCache` integration.

`test_gmail_credentials.py` (added 2026-07-30) covers **credential resolution only** — precedence between a stale env token and a working file token, refresh persistence, and the TTY gate on the OAuth flow. `fetch_messages`, `filter_read_sent`, `_extract_body`, and every write action (`reply`/`trash`/`delete`/`mark_*`) remain untested.

`telegram.py` and `signal.py` still have no test file at all.

To confirm the dead-code claim about `chromium_cache.py` independently:

```
grep -rn "read_cached_pages\|read_cache_messages\|read_cache_entries\|find_chrome_cache\|find_edge_cache\|find_browser_cache" src/event_harvester --include=*.py
```

Expect matches only inside `src/event_harvester/sources/chromium_cache.py` itself and its re-export in `src/event_harvester/sources/__init__.py` — no caller elsewhere in the package.
