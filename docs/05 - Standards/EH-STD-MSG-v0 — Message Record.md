**Standard ID:** EH-STD-MSG-v0 v0
**Title:** Message Record
**Status:** Normative (v0, DRAFT — derived from as-built code 2026-07-29)
**Applies to:** every `dict` emitted by a source reader in `src/event_harvester/sources/` and consumed by `src/event_harvester/pipeline/{harvest,analysis,weights,dedup,event_match}.py`, `src/event_harvester/sources/__init__.py:filter_read_sent`, and `src/event_harvester/pipeline/classifier.py`
**Depends on:** none (this is the base record of the pipeline)

## 0. Scope (what IS / IS NOT covered)

IS covered: the shape of the message `dict` produced by each of the five wired producers (`discord.py`, `telegram.py`, `gmail.py`, `signal.py`, `web_fetch.py`/`web_modes/*.py`), the JSON on-disk cache shape written by `pipeline/harvest.py`, and how `pipeline/analysis.py`, `pipeline/weights.py`, and `sources/__init__.py:filter_read_sent` read that dict.

IS NOT covered: the event-candidate dict produced by LLM extraction (see `EH-STD-EVENT-v0`), the on-disk `data/web_sources.json` / `data/profiles.json` config shape (see `EH-STD-WEBSRC-v0`), and `sources/chromium_cache.py` functions `read_cache_messages` / `read_cached_pages` — these are exported from `sources/__init__.py` but **not called anywhere else in the codebase** (verified by repo-wide grep; `inferred` dead/unwired code). They are noted only in §4 because they claim to emit this record's shape but do so incorrectly.

## 1. Normative Goals (numbered MUST / MUST NOT / SHOULD clauses)

1. A message record MUST be a flat JSON-serializable `dict` (no nested objects except where explicitly noted) with at minimum the keys `platform`, `id`, `timestamp`, `author`, `channel`, `content`. Read directly from every producer in §3.
2. `platform` MUST be one of the values in the vocabulary in §3.1. This is violated in practice — see §4 BUG-MSG-1.
3. `timestamp` MUST be an ISO-8601 string. Producers reading from platforms that supply UTC epoch/UTC-aware sources (Discord, Telegram, Signal, web/ICS synthetic timestamps) MUST produce a timezone-aware (`+00:00`/`Z`-normalized) ISO string. This is violated by one dead code path — see §4 BUG-MSG-2.
4. `id` MUST be a string, unique within a single harvest run for a given `platform`+`channel` pair (producers use it for `seen_ids` de-duplication — `discord.py:132`, `telegram.py:81`, `gmail.py` implicitly via Gmail message id, `chromium_cache.py:332`).
5. Every producer MUST sort its returned list ascending by `timestamp` string before returning (`discord.py:190`, `telegram.py:147`, `gmail.py:231`, `signal.py:161`; web-mode producers do not sort — each source returns messages in discovery order, and `fetch_web_sources` concatenates per-source lists without a final sort — `inferred` from absence of a `.sort(` call in `web_fetch.py`/`web_modes/*.py`).
6. `content` MUST be a `str`. It MAY be truncated by the producer (web modes truncate to 5000 or 8000 chars — see §3.3) but MUST NOT be `None`.
7. `pinned` (bool) is OPTIONAL and, when present, MUST be `True` (producers only ever set it truthy, never `False`; absence means "not known to be pinned"). Only `discord.py` and `telegram.py` set it.
8. `is_sent` (bool) is OPTIONAL and, when present, MUST be `True`. Set by `gmail.py` and `signal.py` only.
9. `is_read` (bool) is OPTIONAL, Gmail-only. MUST be present on every Gmail message record (`gmail.py:218`, always set, never omitted) — this is the one boolean flag that is unconditionally emitted rather than omit-if-false.
10. `filter_read_sent` (`sources/__init__.py` → `sources/gmail.py:240`) MUST only act on records where `platform == "gmail"`; all other platforms MUST pass through unchanged. This means `is_sent` on Signal records is set but structurally inert for this filter — see §4 Non-conformance.
11. Consumers (`pipeline/weights.py`, `pipeline/analysis.py`) MUST treat `pinned`, `is_sent`, `is_read` as optional/absent-means-false via `.get(key)` / `.get(key, False)` — never direct key access. Read directly (`weights.py:263,272,310`; `analysis.py:135`).
12. The on-disk message cache (`pipeline/harvest.py`, `CACHE_FILE = data/.message_cache.json`) MUST wrap the message list in `{"cached_at": <ISO ts>, "sources": [<active source names>], "messages": [...]}` and MUST be considered stale/discarded if `cached_at` is older than 4 hours (`CACHE_MAX_AGE`) or if the active source-name set changed since the cache was written (`harvest.py:96-99`).
13. Web-platform messages with `content` longer than 2000 chars SHOULD be decimated (`web_utils._decimate_text`) before being written to the cache, to bound cache size (`harvest.py:322-324`) — this decimation is cache-write-only and does NOT mutate the in-memory `messages` list returned to the caller for the current run.

## 2. Core Concept

A **message record** is the atomic unit the harvest pipeline operates on: one chat message, one email, one scraped web page/detail-page/API-post, or one calendar VEVENT, normalized into a single flat dict shape so that `pipeline/weights.py` (regex scoring), `pipeline/classifier.py` (ML actionability filter), and `pipeline/analysis.py` (LLM event extraction) can all consume any source's output identically. `platform` is the dispatch key almost every downstream consumer branches on (`filter_read_sent`, `prioritize_with_caps` bucketing, `classifier.py`'s email/chat split). Every producer function independently builds and returns `list[dict]` — there is no shared constructor or dataclass; conformance is convention-only, enforced by nothing at runtime.

## 3. Specification (field tables, value vocabularies, examples)

### 3.1 Core fields (all producers)

| Field | Type | Required | Producer(s) | Consumer(s) | Meaning |
|---|---|---|---|---|---|
| `platform` | str | yes | all | `filter_read_sent`, `weights.prioritize_with_caps`/`prioritize_grouped` (bucket key, lowercased), `analysis.build_prompt` (`.capitalize()`'d into chat header), `classifier.py` (email/chat split) | Source platform tag. See §3.1 vocabulary. |
| `id` | str | yes | all | producer-internal `seen_ids` dedup only; not read downstream | Per-run dedup key. |
| `timestamp` | str (ISO-8601) | yes | all | `weights._recency_score`, `weights.extract_events` (date resolution reference), `analysis.build_prompt` (`[:16]` truncated for display), cache staleness check | When the message was sent/posted, or (web synthetic) when it was harvested. |
| `author` | str | yes | all | `weights.extract_links`/`extract_events` (`author` field passthrough), report/obsidian output `@{author}` | Sender username/email/domain. |
| `channel` | str | yes | all | `analysis.build_prompt` grouping key (`"{platform}/{channel}"`), report output | Chat/channel/group/thread name, or (web) the URL. |
| `content` | str | yes | all | `weights.URL_RE`/`DATE_RE`/`TIME_RE` scanning, `analysis.build_prompt` (`[:400]` truncated), `classifier.py` featurization | Message body / page text / event detail text. |
| `pinned` | bool | optional (true-only) | `discord.py`, `telegram.py` | `weights.extract_links/extract_events` (score boost), `classifier.py` (`is_pinned` feature), `ui/display.py`, `outputs/report.py`, `outputs/obsidian.py` | Pinned-message signal. |
| `is_sent` | bool | optional (true-only) | `gmail.py`, `signal.py` | `filter_read_sent` (Gmail only — see §1.10), `classifier.py` (`sender_is_self` feature, both platforms) | "You sent this" flag. |
| `is_read` | bool | optional, Gmail-only, always emitted when platform=gmail | `gmail.py` | `filter_read_sent` | Gmail UNREAD-label inverse. |
| `thread_id` | str | optional, Gmail-only | `gmail.py` | none found in-repo (`inferred` unused downstream today) | Gmail thread id. |
| `mention_everyone` | bool | optional (true-only), Discord-only | `discord.py` | none found in-repo (`inferred` unused downstream today) | `@everyone`/`@here` flag. |

### 3.1.1 `platform` value vocabulary (as literally emitted)

| Literal value | Emitted by | Notes |
|---|---|---|
| `"discord"` | `sources/discord.py:169` | fixed literal |
| `"telegram"` | `sources/telegram.py:45` | fixed literal |
| `"gmail"` | `sources/gmail.py:211` | fixed literal |
| `"signal"` | `sources/signal.py:144` | fixed literal |
| `"web"` | `web_modes/calendar.py`, `raw.py`, `search.py`, `chat.py`, `ics.py`, and the link-extraction branch of `feed.py` | the *intended* uniform tag for all web sub-sources |
| `<source name>` (e.g. `"instagram"`, `"eventbrite"`, `"chatgpt"`) | `web_modes/feed.py:161` (page-text fallback) and `feed.py:269,292` (`_extract_posts_from_api`, e.g. Instagram GraphQL posts) | **BUG** — see §4 BUG-MSG-1: violates the `"web"` convention every other web mode follows |

`filter_read_sent`, `prioritize_with_caps`, and `CapConfig.get()` all branch/bucket on `platform` case-sensitively after `.lower()`-ing (consumers) but producers emit already-lowercase literals, so this is consistent in practice for the `"web"` case but means BUG-MSG-1's per-source platform tags create *extra* cap buckets (e.g. an `"instagram"` bucket keyed by `CapConfig.get("instagram")`, which falls back to `self.web` per `CapConfig.get()`'s docstring — this fallback is *intentional*, per `config.py:188-196`, specifically to handle web sub-source tags gracefully. The `"web"` vs `name` split is therefore only a documentation/consistency defect, not a functional break, because `CapConfig.get` was written to tolerate it.)

### 3.2 Producer-specific fields

**Discord** (`sources/discord.py`): base fields + optional `pinned`, `mention_everyone`. `channel` = numeric channel_id as string. `id` = Discord snowflake as string. Cache path: Chromium BlockFile cache under `%APPDATA%/discord/Cache/Cache_Data` (Windows) — reads via `ccl_chromium_reader`, not the network API.

**Telegram** (`sources/telegram.py`): base fields + optional `pinned`. `author` resolution order: `username` → `first_name` → `"unknown"`. `channel` = dialog title/first_name/`"unknown"`. Requires `TelegramConfig.is_configured` (`TELEGRAM_API_ID`/`TELEGRAM_API_HASH`); pinned messages are fetched separately (last 10 per dialog, `include_pinned=True` by default) regardless of the `cutoff` date filter.

**Gmail** (`sources/gmail.py:fetch_messages`): base fields + `thread_id`, `is_read`, `is_sent` (both always present). `content` = `f"{subject}\n{snippet}"` (Gmail API snippet, not full body — full body requires the separate `fetch_full_bodies()` call, which is not part of the standard message record). `channel` = first non-system label on the message, else `"INBOX"`. `timestamp` = parsed `Date` header via `email.utils.parsedate_to_datetime`; falls back to the raw header string if parsing fails (`gmail.py:131` — meaning `timestamp` is NOT guaranteed ISO-8601 in this one failure path; `inferred` edge case, not observed to trigger in normal operation).

**Signal** (`sources/signal.py`): base fields + optional `is_sent`. Reads Signal Desktop's local SQLCipher DB directly (key extraction via `sigexport.crypto`). `author` = `"You"` for outgoing, else `source` column or channel name. `timestamp` built from `sent_at` (ms epoch) via `datetime.fromtimestamp(ms/1000, tz=timezone.utc)` — always tz-aware.

**Web — mode `calendar`** (`web_modes/calendar.py`): `id` = `f"web:{hash(url)}"` (Python `hash()` — process-local, NOT stable across runs; see §4). `timestamp`: synthetic — either `f"{date_hint}T00:00:00+00:00"` for inline calendar events with a known date, or `datetime.now(timezone.utc).isoformat()` for everything else (detail-page fetches, raw-text fallback). `content` truncated to 5000 (detail pages) or 8000 chars (raw-text fallback).

**Web — mode `feed`** (`web_modes/feed.py`): three distinct content shapes under one mode — (a) event-link detail-page text (`platform: "web"`, 5000-char cap), (b) whole-page-text fallback when no event links found (`platform: <source name>` — BUG-MSG-1, 5000-char cap), (c) parsed API/GraphQL posts via `_extract_posts_from_api` (`platform: <source name>` — BUG-MSG-1, `id: f"ig:{media_id}"`, 2000-char cap, `author: f"@{username}"`, `channel: "instagram-feed"` hardcoded regardless of actual source name).

**Web — mode `search`** (`web_modes/search.py`): event-link detail pages (`platform: "web"`, `id: f"web:{hash(...)}"`) or, if no event links found, raw feed-item text (`platform: "web"`, `id: f"web:{hash(item_text[:100])}"`, `channel: f"{name}-feed"`).

**Web — mode `raw`** (`web_modes/raw.py`): single message per source, full-page text, `content[:8000]`.

**Web — mode `chat`** (`web_modes/chat.py`): single message, `id: f"chat:{name}:{hash(query)}"`, `author: f"chat:{name}"`, `channel: f"chat-{name}"`, `content[:8000]` = the chatbot's raw text response.

**Web — mode `ics`** (`web_modes/ics.py`): one message per `VEVENT`. `id` = `f"ics:{name}:{uid}"` if the VEVENT has a `UID`, else a stable `sha1(summary|start_iso)[:16]` digest — this is the only web-mode `id` that is stable across runs (not `hash()`-based). `content` is a fixed line-delimited pack: `summary`, `Start: <iso>`, optional `End: <iso>`, optional `Location:`, optional `URL:`, optional description (`[:2000]`). `timestamp` = the event's own start time if it has a time component (`"T" in start_iso`), else "now" (`datetime.now(timezone.utc).isoformat()`) — deliberately, per the code comment, "so date-only events don't confuse downstream time-based filters."

### 3.3 Example (real, reconstructed from `sources/telegram.py:_msg_to_dict` + `sources/discord.py`)

```json
{
  "platform": "telegram",
  "id": "48213",
  "timestamp": "2026-07-28T21:04:33+00:00",
  "author": "alice_sf",
  "channel": "SF Queer Events",
  "content": "Reminder: rooftop mixer this Saturday 6pm at The Stud, RSVP https://lu.ma/xyz123",
  "pinned": true
}
```

Gmail example (`sources/gmail.py:fetch_messages`):

```json
{
  "platform": "gmail",
  "id": "18d2f1a9b3c4e5f6",
  "thread_id": "18d2f1a9b3c4e5f6",
  "timestamp": "2026-07-27T09:15:02-07:00",
  "author": "events@sfchamber.com",
  "channel": "INBOX",
  "content": "Weekly Networking Mixer\nJoin us this Thursday for...",
  "is_read": false,
  "is_sent": false
}
```

## 4. Non-conformance observed (as-built deviations, each with file:function)

- **BUG-MSG-1 — `platform` vocabulary violated by feed mode.** `src/event_harvester/sources/web_modes/feed.py:161` (page-text fallback) and `:269,292` (`_extract_posts_from_api`) set `"platform": name` / `"platform": platform` (the source's *configured name*, e.g. `"instagram"`) instead of the fixed `"web"` literal every other web mode uses (`calendar.py`, `raw.py`, `search.py`, `chat.py`, `ics.py` all hardcode `"platform": "web"`). Functionally tolerated because `CapConfig.get()` (`config.py:188-196`) falls back to `self.web` for unknown platform names — but any consumer that branches on `platform == "web"` specifically (there is none today, but `filter_read_sent` demonstrates the pattern exists) would silently skip Instagram-sourced messages.

- **BUG-MSG-2 — naive (non-tz-aware) timestamp in a dead code path.** `src/event_harvester/sources/chromium_cache.py:449,475` (`read_cached_pages`) emits `"timestamp": datetime.now().isoformat()` — no `timezone.utc`, unlike every wired producer, which is either explicitly UTC-aware or (Gmail) parses an explicit offset. This function is exported from `sources/__init__.py` but a repo-wide grep found **zero call sites** outside its own module and `__init__.py`'s re-export — it and its sibling `read_cache_messages` (correctly UTC-aware) are unwired/dead. `read_cached_pages` additionally references undefined names `gzip_event_score` and `gzip_threshold` at line 468 (no such names exist anywhere in the file or its imports) — the HTML-fallback branch of this function would raise `NameError` if ever invoked with a page not returning JSON.

- **Non-conformance — `is_sent` filtering is Gmail-only despite being emitted for two platforms.** `sources/__init__.py` → `sources/gmail.py:filter_read_sent` (`gmail.py:252-253`) only inspects `is_sent`/`is_read` when `m.get("platform") == "gmail"`; all other platforms pass through unfiltered. `sources/signal.py:152` sets `is_sent = True` on outgoing messages, but nothing in the pipeline ever filters on it — it is read only by `pipeline/classifier.py:98` as a scoring feature (`sender_is_self`), never as a hard exclude. This is a real behavioral asymmetry, not obviously intentional: Gmail messages you sent are dropped before the LLM ever sees them; Signal messages you sent are kept and can be extracted as "events."

- **Non-conformance — web-mode `id` values are not stable across runs.** Every web mode except `ics` builds `id` from Python's built-in `hash()` (e.g. `f"web:{hash(lnk['url'])}"`, `calendar.py:178`) which is randomized per-process (PYTHONHASHSEED) for strings unless explicitly seeded — `inferred`, not verified at runtime in this pass, but this is documented CPython behavior. This means the per-run `seen_ids` dedup within a single harvest works, but `id` cannot be relied on as a stable cross-run identity key the way Discord/Telegram/Gmail/Signal/ICS ids are (numeric snowflakes, message ids, or the `ics:` sha1 digest).

- **Non-conformance — web messages are not timestamp-sorted before merge.** §1.5: `pipeline/harvest.py:harvest_messages` concatenates `discord + telegram + gmail + signal + web` in that fixed source order (`harvest.py:310-313`) without a final chronological sort; each non-web producer pre-sorts its own list, but web-mode handlers never sort within `fetch_web_sources`, and the multi-threaded per-chunk dispatch (`ThreadPoolExecutor`, `as_completed`) means web message order is non-deterministic run-to-run in addition to being unsorted.
