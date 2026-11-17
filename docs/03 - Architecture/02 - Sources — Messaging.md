# Sources — Messaging (non-web)

**Status:** CURRENT · **Date:** 2026-08-17 (Discord cache found dead; scheduled-events path added) · **Applies to:** `src/event_harvester/sources/__init__.py`, `src/event_harvester/sources/discord.py`, `src/event_harvester/sources/discord_events.py`, `integrations/discord-betterdiscord-plugin/`, `src/event_harvester/sources/telegram.py`, `src/event_harvester/sources/gmail.py`, `src/event_harvester/sources/signal.py`, `src/event_harvester/sources/chromium_cache.py`, `scripts/auth_telegram.py`, `tests/test_discord.py` · **Depends on:** `src/event_harvester/config.py`, `src/event_harvester/pipeline/harvest.py`

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

> **The cache is dead — measured 2026-08-16.** A live scan found **0 of 4,257**
> cache entries matching `/channels/{id}/messages`: Discord no longer writes
> message responses to the desktop HTTP cache (they arrive over the gateway
> websocket and stay in memory). So the scraping described below now yields
> nothing on a current client, and the source's real output comes from
> **`discord_events.py`** (guild *scheduled events*, via a client-mod export).
> `read_discord_messages` was refactored to a thin merge of both halves so a
> dead cache no longer early-returns before the scheduled events are read.

**Domain:** Local Chromium HTTP-cache scraping for Discord's own web client (Discord's desktop app is Electron/Chromium under the hood, so its message-fetch responses land in the same on-disk cache format Chrome uses). The format is detected per cache: BlockFile (Windows) or SimpleFile (Linux, verified 2026-11-17 on smallblackbox).
**Runs at:** Sync function, invoked from `pipeline/harvest.py:_run_discord` via `asyncio.to_thread(read_discord_messages, ...)` — off the event loop, in a worker thread.
**Inputs:** No credentials. `cache_path` (optional direct `Path`), `override_path` (str, from `DISCORD_CACHE_PATH` env via `cfg.discord.cache_path`), `cutoff: datetime`.
**Outputs:** message dicts, see schema table below.

### Internal structure

- `find_discord_cache()` — platform dispatch: WSL (`Linux` + `/mnt/c/Users` exists) → `_find_cache_wsl()` scans every user directory under `/mnt/c/Users` (skipping `Public`/`Default`/`Default User`/`All Users`) for `AppData/Roaming/discord/Cache/Cache_Data`; Windows → `%APPDATA%\discord\Cache\Cache_Data`; else → `~/.config/discord/Cache/Cache_Data`.
- `_decompress()` — tries gzip magic bytes, then raw zlib, then raw-deflate (`-zlib.MAX_WBITS`), then gives up and returns the input unchanged.
- `_parse_message_blobs()` — finds the first `[` or `{` in the decoded text (skips leading HTTP-cache header junk), `json.loads`s from there, and keeps only dict entries that have both `id` and `content` keys. Covered by `tests/test_discord.py` (see below).
- `read_discord_messages()` — copies the whole cache dir to `tempfile.mkdtemp()` (avoids the lock a running Discord process holds on the live cache), picks the reader with `ccl_chromium_reader.ccl_chromium_cache.guess_cache_class` (BlockFile or SimpleFile; an unrecognised format logs a warning and returns empty), iterates all cache keys, filters URLs matching `_MSG_URL_RE = r"/channels/(\d+)/messages"`, decompresses + parses each cache entry's buffers, dedupes by message `id`, applies the `cutoff` filter, and always closes the cache and removes the temp dir in a `finally` block. Before 2026-11-17 it hard-coded `ChromiumBlockFileCache`, which failed with an invalid-magic error on every Linux SimpleFile cache. **Measured 2026-11-17:** the Linux cache parses (1,118 keys) but holds 0 message entries, consistent with the 2026-08-16 finding in `.env.example` that current clients no longer cache message JSON; the scheduled-events plugin is the live Discord path.

### Hard constraints / failure modes

- **BUG:** `find_discord_cache()`'s non-Windows, non-WSL fallback branch (line ~45-48) uses `~/.config/discord/...` (the Linux path) for *any* other platform, including macOS — there is no Darwin-specific branch, unlike the sibling `chromium_cache.py:find_chrome_cache`, which does special-case `Darwin`. On a real Mac this will look in the wrong directory and silently return `None`.
- **known rough edge:** the module duplicates `_decompress`/cache-copy/BlockFileCache-iteration logic that already exists, more generically, in `chromium_cache.py` (`decompress`, `read_cache_entries`). The two implementations have already drifted (different URL-pattern handling, different platform coverage) — a bug fixed in one will not propagate to the other.
- Malformed cache entries (bad JSON, decode errors) are swallowed per-message inside a broad `except Exception: continue` — failures here are invisible unless you add logging.
- `tests/test_discord.py` exercises the pure function `_parse_message_blobs` (8 cases: array, single dict, mixed-type array, empty/None body, invalid JSON, missing-`id`, leading whitespace, UTF-8 content) and `TestSimpleFileCache`, which pins SimpleFile detection in `read_discord_messages` (fails against the old BlockFile-only code). **known rough edge:** `find_discord_cache`, `_find_cache_wsl` and `_decompress` remain untested.


## `discord_events.py` — guild scheduled events · **added 2026-08-17**

**Domain:** Guild *scheduled events* read from the running client's in-memory
`GuildScheduledEventStore`, which was never in the HTTP cache. Since the cache
scraping now returns nothing (banner above), this is the only Discord path that
produces anything.

**Runs at:** Pure Python, called from `read_discord_messages` on every Discord
harvest. No browser, no network, no credentials.

**Inputs:** A local JSON file a client-mod plugin writes —
`%APPDATA%\discord-harvester\scheduled-events.json` (Windows) or
`~/.config/discord-harvester/scheduled-events.json`, overridable with
`DISCORD_EVENTS_EXPORT`. The plugin lives in
`integrations/discord-betterdiscord-plugin/` and only reads the Flux store and
writes that one file — it sends no message and makes no network request.

**Outputs:** `discord` message records. A scheduled event is already structured
(exact start, title, external-event location), but it flows through the normal
pipeline as a message whose `content` **states the date in plain text**, so the
local model resolves it from the same slice it reads (the pre-dated-message
pattern). Keeping one pipeline beats saving one cheap LLM call.

### Rules that earn their place

- **UTC → local machine zone.** Discord stores `scheduled_start_time` in UTC;
  the reader converts to the machine's local zone before writing it into
  content, because the harvester runs on the viewer's machine. A 7pm event
  stored as next-day 02:00Z must not read as 2am.
- **`timestamp` is ingestion time, not the event start.** A future timestamp
  would confuse recency scoring and the days-back cutoff. The event's date is
  in `content`; the record is stamped *now*.
- **Dropped:** past events, and status ∉ {SCHEDULED(1), ACTIVE(2)} — a
  completed or cancelled event is not an invitation.
- **A missing/corrupt/absent file is a quiet empty**, and the 0-message source
  warning (`orchestrator._source_warnings`) covers the "Discord returned
  nothing" case. A stale export (>24h) is logged but still used — a scheduled
  event for next week is valid even if dumped two days ago.

### Why not a bot

The supported read is a bot with the `GUILD_SCHEDULED_EVENTS` intent, but it
needs *Manage Server* to invite. The client-mod path works on any guild you are
a member of. Client mods are technically against Discord's ToS (client
modification), unenforced for passive read-only use — see the plugin README for
the honest framing. Where you have admin, prefer the bot.

Covered by `tests/test_discord_events.py` (18 cases, all offline). The plugin
itself is **untested in a live client** at time of writing — authored against
Discord's documented Flux store and event names, defensively.

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

- `_get_credentials()` — builds an ordered candidate list, (1) `GMAIL_TOKEN_JSON` env, base64→JSON→`Credentials.from_authorized_user_info`, then (2) the `token.json` file, and hands each to `_use_token()`, which returns usable credentials or `None` so the loop moves on. A refreshed token is persisted back to `token_file` (note: refreshing an env-sourced token still gets written to the *file* path, not back to the env var — on a redeploy from env vars only, this refreshed token is lost unless something re-reads the file into the env). Only when every stored token is dead does it reach (3) fresh interactive OAuth via `GMAIL_CREDENTIALS_JSON` env (written to a temp file for `InstalledAppFlow`, deleted after) or `credentials.json` file, using `flow.run_local_server(port=0)` — **gated on `_oauth_flow_allowed()`**, i.e. a TTY or `GMAIL_OAUTH_INTERACTIVE=1`.
  **The loop is the point, and it is load-bearing.** Until 2026-07-30 the file was guarded by `if creds is None`, so an env token that merely *parsed* claimed the slot — and when its refresh failed, control skipped a working on-disk credential entirely. That combination (stale `.env` copy, live `token.json`) had Gmail silently returning nothing. Each candidate is now tried *to completion, refresh included*, before the next is considered.
- `fetch_messages()` — builds query as `f"{cfg.query} after:{since:%Y/%m/%d}"`, paginates in batches of ≤100 up to `cfg.max_results`, and per message calls `messages().get(format="metadata")` (headers + snippet only). That yields `content = f"{subject}\n{snippet}"`, where the snippet is Gmail's preview text — **hard-capped by the API at 200 characters**. Since 2026-08-10 it then calls `hydrate_bodies()` unless `cfg.fetch_bodies` is off. Hydration happens *here*, inside the source, so `data/.message_cache.json` stores real bodies and the extra API calls are paid once per harvest rather than once per cache miss.
- `channel` is derived by picking the first label on the message that isn't a system prefix (`CATEGORY_`, `UNREAD`, `IMPORTANT`, `SENT`, `DRAFT`), falling back to `"INBOX"` — this is a Gmail *label*, semantically unlike every other source's `channel` (which means a chat/room name).
- `filter_read_sent(messages, *, drop_read=True)` — module-level filter (not called inside `fetch_messages`; called separately by `pipeline/orchestrator.py` after all sources are merged). Always drops `is_sent=True`; drops `is_read=True` only when `drop_read`. The **parameter default stays `True`** because that is what the recruiter-triage callers mean by "actionable"; `orchestrator.py` passes `cfg.gmail.drop_read`, which defaults to **False**. Non-Gmail messages pass through untouched.
- `fetch_full_bodies()` — `format="full"` fetch returning `dict[id, body]`; recursively walks MIME parts, prefers `text/plain`, falls back to HTML-with-tags-stripped via regex. Callers: `hydrate_bodies` and `cli/commands/recruiters.py`. **Sequential**, one `get` per message.
- `hydrate_bodies()` — **added 2026-08-10.** Replaces the 200-char snippet with the real body. See "Why the snippet was not enough" below.
- `ensure_label()` / `apply_label()` / `label_event_messages()` — **added 2026-08-10**, see "Labelling the mail that produced an event" below.
- `reply()`/`trash()`/`delete()`/`mark_read()`/`mark_unread()` — straightforward wrappers; `reply` threads via `In-Reply-To`/`References` headers pulled from the original message's metadata.
  **Only `reply` and `trash` have callers**, both in the recruiter-triage lane (`outputs/obsidian.py:382,396`, `cli/commands/recruiters.py:85`). `delete`, `mark_read` and `mark_unread` are not re-exported from `sources/__init__.py` and are called by nothing — dead code, and `delete` is an irreversible `messages().delete`. Tracked as roadmap 2.12.

### Why the snippet was not enough — and why content *order* is load-bearing

**Added 2026-08-10.** Until then the pipeline read `subject + snippet` and nothing else. Measured over a live 85-message window: median body **2505 chars** against a 200-char snippet cap, so **the pipeline was reading ~8% of every email**. 32 of those 85 reached the reranker carrying neither a date nor a link and were dropped for exactly that — while 4 of them held an Eventbrite or Luma URL that sat just past the cut. `fetch_full_bodies` had existed the whole time with a single caller in the recruiter lane; the harvest path never used it.

**Fetching the body is only half the fix, and the other half is the Instagram permalink-first lesson repeating.** `analysis.build_prompt` truncates every message to **400 characters** *and* derives its `dates=`/`times=` hints from that same slice — so a date past char 400 is invisible twice over. Concatenating a 5 KB newsletter would have handed the LLM a letterhead and an unsubscribe notice. `content` is therefore assembled in signal order:

```
<subject>
<up to 2 event-platform URLs>       ← eventbrite / lu.ma / meetup / partiful / dice.fm …
<body, decimated to its date-densest window>
```

- **Links before prose** because a ticketing URL is the single most reliable event signal an email carries, and its slug (`…-aug-6-2026-tickets-…`) is often a better date source than the surrounding text. Capped at 2: a tracking URL runs 150+ chars and three would crowd out the window.
- **Decimation reuses `web_utils._decimate_text`**, the web path's date-cluster centring. Two adjustments were needed. It no-ops on text already under its threshold, so the target is `_DIGEST_CHARS = 1500` rather than the 5000-char storage ceiling — sized at the ceiling, a typical 2.5 KB newsletter passes through whole and defeats the point. And its backward reach for a title was a hardcoded 300 chars, which would consume three quarters of the prompt window; it is now a `context_before` parameter, **default 300 so the web path is unchanged**, and Gmail passes 80.
- A body that will not fetch leaves that message on its snippet. Enrichment never costs a message.

**Measured on the same 63-message window** (`--only gmail --days 7 --dry-run`):

| | reranker "no date/link" drops | events |
|---|---|---|
| snippets, read mail dropped *(before)* | 27 of 44 — **61%** | 8 |
| full bodies, read mail dropped | 9 of 34 — **26%** | 11 |
| full bodies, read mail kept *(shipped)* | 18 of 59 — 31% | **21** |

Three of the four body-only links became events (`65labs Unconference`, `Vibe Space Interview`, `Founder-Led GTM`). **The read filter turned out to be the larger effect of the two** — worth knowing before tuning the body path further.

### Following the link — schema.org from the event page

**Added 2026-08-10, immediately after the above**, because the body carries the invitation and the *page* carries the facts. "You are invited to Vibe Space" tells the extractor nothing about when.

`expand_event_links()` fetches each event-platform URL found in a body and reads its schema.org JSON-LD, reusing `feed_probe._fetch_page` (browser-free, sets a UA) and `web_utils._extract_structured_data` (the same reader the web calendar path uses). **No new fetching or parsing machinery.**

**Measured 2026-08-10 over 14 live links: 12 returned a full `Event`** with exact `startDate`, `endDate` and a resolved postal address. **Luma serves JSON-LD server-side despite being a React SPA** — that is the fact that made this cheap, and it is not safe to assume of the next platform. The two misses render event data client-side and degrade to the og: tags already in the body text.

The result goes at the **front** of `content`, ahead of even the links:

```
<subject>
EVENT: Networking Event … | WHEN: 2026-08-26 18:00 | WHERE: Bar Darling, 2263 Chestnut Street, …
<links>
<decimated body>
```

Same reasoning as everywhere else in this file — `build_prompt` keeps 400 chars, and these ~90 are exact where every other 90 are suggestive.

Fetches are pooled (`expand_workers`, default 8) and **deduplicated across the whole harvest before fetching**, because the same event is routinely linked from several emails — that keeps the request count near the number of *events*, not the number of mails. Ceiling `expand_max` (default 40); overflow is logged, never silent. `GMAIL_EXPAND_LINKS=0` disables.

**Effect on field coverage**, live run: every extracted event carried a date **and** a time, and all but one a resolved venue. Before expansion, 5 of 22 had no date at all and roughly half had no location. Event *count* barely moved (18 vs 21) — this raises quality, not recall, and those are separate axes.

`known rough edge`: more text also means more newsletters read as events. Roughly 3–4 of the 22 extracted are not real events (`Top AI Demos #39`, `Final workflow shape`) against 8 total before, so precision fell while recall rose a lot. No reject bucket distinguishes them.

### Labelling the mail that produced an event

**Added 2026-08-10.** Harvest applies a Gmail label (`GmailConfig.events_label`, default `"events"`, empty disables) to every message that produced an event. **This is the only part of the harvest that writes to a mailbox**, and the design is shaped by that:

- **Additive.** `batchModify` with `addLabelIds` only — it never removes a label and never archives, so a labelled message stays in the inbox and re-running is a no-op rather than a second edit. It adds a label; it does not *move* mail.
- **`--dry-run` prints the pairings, not a count.** "Would tag 16 messages" is not something a reader can check; `event title <- message subject` is, and a wrong pairing is obvious on sight. That output is what caught the dedup false positive recorded below.
- **Runs before the TickTick sink**, which returns early when TickTick is unconfigured. Labelling your own mail should not depend on a third-party task service being set up.
- **Confident links only.** See `EH-STD-EVENT-v0 §3.2a` — the `_source_ids` back-reference resolves within the LLM batch, requires a verbatim link or an author-address match, and yields nothing on ambiguity. Events with no resolved source are reported and skipped, never guessed at.

`ensure_label` is case-insensitive on lookup: Gmail rejects a new label differing from an existing one only by case, so asking for `events` when `Events` exists must adopt it.

**Measured 2026-08-10**, first live run: created label `events`, tagged **12 messages** from 13 events, 3 events unresolved. Verified against the API (`q=label:events` → 12).

`known rough edge`: `dedup_events` unions `_source_ids` when it collapses a duplicate — right in general, but it faithfully propagates dedup's own false positives. One pairing in 19 was wrong (`SkyPilot AI Infra Meetup` absorbed a `SUPERNOVA 2026` email; both fell on 2026-08-18 and scored 2). That is the non-transitive-matching hazard in `00 - System Overview.md` §5.4 becoming visible, not a new defect.

### Hard constraints / failure modes

- No credentials at all → `_get_service` returns `None` → every function short-circuits to `[]`/`False`/`None`, all logged at `info`/`warning`, never raised.
- **Labelling needs the `gmail.modify` scope**, already in `SCOPES`. A token minted before this feature still carries it — no re-consent was needed.
- **Hydration costs one extra `messages().get(format="full")` per message, sequentially.** 63 messages added roughly a minute to the run. `GMAIL_FETCH_BODIES=0` turns it off. Batching or threading it is unclaimed work — the `googleapiclient` service object is not thread-safe, so it needs `new_batch_http_request` rather than a naive pool.
- A stored token that cannot be refreshed logs a warning and is skipped, not raised. `_get_service` is called **once per operation**, not once per run, so a stale-but-present token costs a failed network round-trip on every single Gmail call.
- ~~**unverified:** whether a fully headless run … hangs on `run_local_server`~~ — **resolved 2026-07-30**: it would have. The flow is now gated on `_oauth_flow_allowed()` and a headless run logs an actionable error and returns `None` instead. Pinned by `tests/test_gmail_credentials.py`.

### Against the first-party Claude Gmail MCP connector

Worth knowing before anyone proposes replacing this module with the connector, or spinning it out into one. Compared 2026-07-30 against the connector's live tool schemas:

| | This module | Claude Gmail MCP (16 tools) |
|---|---|---|
| Unit of work | Message | Thread |
| Bulk read | ≤100/batch to `max_results` (default 200), normalized to `EH-STD-MSG-v0` | `search_threads`, 20/page, **max 50**, thread-shaped |
| Query | `after:` + `GMAIL_QUERY` passthrough | Full documented operator set |
| Bodies | `text/plain`, else regex-stripped HTML | `plaintextBody` **and** `htmlBody`, parsed |
| Attachments | ✗ | ✓ metadata + retrieval |
| Send | ✓ `reply()` really sends | ✗ **drafts only** |
| Trash / delete | ✓ both | ✗ neither |
| Labels | read-only + `UNREAD` toggle | full CRUD |
| Unattended | ✓ own token, scheduled | ✗ needs a claude.ai session |

**They are complementary, not competing.** The connector is the better interactive reader — real HTML bodies and attachments, which this module cannot see at all. But it **cannot send and cannot trash**, which is the entire recruiter-triage loop, so it cannot replace the write half. And it cannot run unattended, so it cannot replace the harvest half either.

The connector's omissions are also a design signal: a first-party integration deliberately ships drafts-and-labels with no permanent delete. This module has one, unused and untested (2.12).

`inferred` — this comparison is from the connector's tool schemas, not from running it.

## `signal.py`

**Domain:** Signal Desktop's local encrypted SQLite (SQLCipher) database, read directly (no Signal API involved).
**Runs at:** Sync function, via `asyncio.to_thread(read_signal_messages, ...)` from `pipeline/harvest.py:_run_signal`.
**Inputs:** No API credentials — pulls the local SQLCipher decryption key via `sigexport.crypto.get_key(signal_dir, None)`, which itself is platform-specific (Windows DPAPI). `override_path` from `cfg.signal.db_path` (`SIGNAL_DB_PATH` env — despite the env var name, this overrides the *directory* passed to `_find_signal_dir`, not a literal `db.sqlite` file path).
**Outputs:** message dicts, see schema table.

### Internal structure

- `_find_signal_dir()` — checks `override_path` first, else Windows (`%APPDATA%\Signal`), macOS (`~/Library/Application Support/Signal`), Linux (`~/.config/Signal`), each gated on `(candidate / "sql" / "db.sqlite").exists()`.
- `_connect_db()` — lazy-imports `sigexport.crypto` and `sqlcipher3.dbapi2` (both present in `.pixi/envs/*/Lib/site-packages` in this checkout, pulled in transitively via the `signal-export` PyPI package declared in `pyproject.toml`), extracts the key, opens the DB **read-only** (`file:…?mode=ro` URI, since 2026-11-17 — the harvester must never write to Signal's store) with `PRAGMA KEY`, `cipher_page_size=4096`, `kdf_iter=64000`, `HMAC_SHA512`/`PBKDF2_HMAC_SHA512` — these PRAGMA values must match whatever Signal Desktop itself used to create the DB; **unverified against Signal Desktop version drift** (Signal has changed SQLCipher parameters across major versions historically). Verified decrypting on Linux 2026-11-17. A decryptable DB with 0 `messages` and no registration item in `items` means Signal Desktop is not linked to a phone, not that the source is broken.
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
| `content` | full message text | full message text (`msg.text`) | **assembled, since 2026-08-10**: `subject` + a schema.org `EVENT:` facts line (when a linked event page answered) + up to 2 event-platform URLs + the body decimated to its date-densest window, capped at 5000. Was a ≤200-char `"{subject}\n{snippet}"` preview. Falls back to that shape if the body will not fetch. **Part order is normative** — see EH-STD-MSG-v0 | full message body | JSON-dumped object (2000 chars) or extracted HTML text (2000 chars) |
| `pinned` | present (`True`) only if pinned; absent otherwise | present (`True`) only if pinned; absent otherwise | — (not emitted) | — (not emitted) | — |
| `mention_everyone` | present (`True`) only if set; absent otherwise | — | — | — | — |
| `thread_id` | — | — | **always present** (str, possibly empty) | — | — |
| `is_read` | — | — | **always present** (bool) | — | — |
| `is_sent` | — | — | **always present** (bool) | present (`True`) only if outgoing; **absent** for incoming | — |

**Divergence hazards for anything consuming the merged list:**

- `channel` means three different things (chat/dialog name for Discord/Telegram/Signal, a Gmail *label* for Gmail, a URL for the generic cache reader) — any downstream grouping/report "by channel" silently mixes incompatible concepts.
- `content` truncation is inconsistent, though less so since 2026-08-10. Gmail's `content` was a ≤200-char snippet; it is now a subject + links + date-decimated-body pack capped at 5000. It is still the only source whose `content` is *assembled* rather than quoted, and the only one that is lossy by design — a length- or keyword-based filter tuned against Discord/Telegram/Signal will still behave differently on Gmail.
- `is_sent`/`pinned`/`mention_everyone`/`thread_id`/`is_read` follow two different conventions: Gmail always sets its booleans explicitly (`True`/`False`); Discord/Telegram/Signal use "key present ⇒ true, key absent ⇒ false/unknown." Code using `.get(key)` (as `filter_read_sent` and `is_sent` checks do today) is safe either way, but `"key" in msg` would not be.
- `filter_read_sent()` only inspects `platform == "gmail"`; Signal's `is_sent` flag is real data but nothing reads it — your own outgoing Signal messages are not excluded from downstream LLM event extraction, unlike your own sent Gmail.
- `read_cached_pages`'s `timestamp` is harvest-time, not message-time — if this module's output were ever wired into a caller that also passes it through the standard `cutoff` filtering pattern used elsewhere (`ts < cutoff: continue`), it would trivially always pass, defeating the point of a days-back window.

## Verification

```
pixi run -e dev pytest tests/test_discord.py tests/test_gmail_credentials.py tests/test_chromium_cache.py -v
```

`test_discord.py` covers the 8 `_parse_message_blobs` cases (array/single/mixed-type parsing, empty/invalid input, missing-`id` rejection, whitespace and UTF-8 handling) and SimpleFile cache detection in `read_discord_messages`. It does **not** touch cache discovery or decompression.

`test_gmail_credentials.py` (added 2026-07-30) covers **credential resolution only** — precedence between a stale env token and a working file token, refresh persistence, and the TTY gate on the OAuth flow. `fetch_messages`, `filter_read_sent`, `_extract_body`, and every write action (`reply`/`trash`/`delete`/`mark_*`) remain untested.

`telegram.py` and `signal.py` still have no test file at all.

To confirm the dead-code claim about `chromium_cache.py` independently:

```
grep -rn "read_cached_pages\|read_cache_messages\|read_cache_entries\|find_chrome_cache\|find_edge_cache\|find_browser_cache" src/event_harvester --include=*.py
```

Expect matches only inside `src/event_harvester/sources/chromium_cache.py` itself and its re-export in `src/event_harvester/sources/__init__.py` — no caller elsewhere in the package.
