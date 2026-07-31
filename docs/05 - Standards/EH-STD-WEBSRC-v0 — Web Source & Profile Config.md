**Standard ID:** EH-STD-WEBSRC-v0 v0
**Title:** Web Source & Profile Config
**Status:** Normative (v0, DRAFT — derived from as-built code 2026-07-29)
**Applies to:** `data/web_sources.json`, `data/profiles.json`, and the code that reads them: `src/event_harvester/sources/web_utils.py`, `src/event_harvester/sources/web_fetch.py`, `src/event_harvester/sources/web_modes/*.py`, `src/event_harvester/profiles.py`, `src/event_harvester/config.py:WebConfig`, `src/event_harvester/cli/commands/web.py`
**Depends on:** `EH-STD-MSG-v0` (each web source ultimately produces message records)

## 0. Scope (what IS / IS NOT covered)

IS covered: every field a web-source config object may carry in `data/web_sources.json`, which of those fields is actually read by code vs. silently ignored, the `mode` value vocabulary and auto-detection fallback order, cooldown/scroll defaults, and the `data/profiles.json` schema plus what a profile can and cannot override on `AppConfig`.

IS NOT covered: the message records each mode produces (see `EH-STD-MSG-v0` §3.2 "Web — mode …" rows) and the LLM event-extraction step downstream of harvesting.

## 1. Normative Goals (numbered MUST / MUST NOT / SHOULD clauses)

1. `data/web_sources.json`, when present, MUST be a top-level JSON array of source objects; when absent, `web_utils._load_web_sources()` MUST fall back to the 7-entry `_DEFAULT_SOURCES` constant baked into `web_utils.py:71-80` (read directly, `web_utils.py:83-88`).
2. Every source object MUST contain `url` (str) — every mode handler accesses `src["url"]` by direct indexing, not `.get()` (`web_modes/calendar.py:129`, `feed.py:59`, `raw.py:41`, `search.py:46`, `ics.py:67`, and `web_fetch.py:207`), so a source object missing `url` MUST raise `KeyError` at harvest time — this is enforced by omission, not by an explicit validator.
3. `name` (str) SHOULD be present; every mode handler that reads it uses `src.get("name", <mode-specific fallback>)` (`"web"` for calendar/raw/feed/search, `"ics"` for ics, `"chat"` for chat) — absence degrades logging/dedup-cache-key quality but does not fail the run.
4. `mode` MUST be one of the literal strings in §3.1, or absent/`"auto"`, which triggers runtime auto-detection (`web_fetch.py:220-240`, `web_utils._detect_mode`). Read directly, `web_fetch.py:178,184`.
5. `scroll_seconds` (int) is OPTIONAL. Its default value is mode-dependent (see §3.2) and is NOT a single global constant despite `DEFAULT_SCROLL_SECONDS = 5` existing in `web_utils.py:252` — `web_modes/feed.py:61` hardcodes its own independent default of `30`, ignoring `DEFAULT_SCROLL_SECONDS` entirely (see §4).
6. `cooldown_hours` (int) is OPTIONAL, default `-1` (cooldown disabled) as read by `web_fetch.py:fetch_web_sources` line 123 (`src.get("cooldown_hours", -1)`) — a source is skipped only if `cooldown_hours >= 0` AND the last successful fetch (per `data/.web_cooldowns.json`, keyed by URL) is within that many hours, AND the caller did not pass `no_cooldown=True`.
7. `native_chrome` (bool) is OPTIONAL, default falsy. When `True`, the source MUST be routed to the single serial "native Chrome" worker group rather than the `parallel`-way standard `UCBrowser` pool, specifically to keep request rates low against Cloudflare-protected sites (`web_fetch.py:132-133,300-301`).
8. `api_pattern` (str, regex) is OPTIONAL, `feed`-mode-only, default `"graphql|api"` (`web_modes/feed.py:60`) — used to filter which intercepted XHR/fetch responses are treated as API payloads worth parsing for posts.
9. `api_only` (bool) is OPTIONAL, `feed`-mode-only, default falsy — when `True`, `_do_feed_inner` MUST skip the event-link/detail-page extraction path entirely and rely solely on intercepted API responses (`web_modes/feed.py:137`).
10. `query` (str) is OPTIONAL, meaningful to `search` mode (search-bar text) and `chat` mode (chatbot prompt) — `search` mode without a `query` set MUST skip the search-fill step and fall through to scroll+extract only (`web_modes/search.py:47,65`); `chat` mode without a `query` set MUST fall back to a hardcoded default prompt (`web_modes/chat.py:41`).
11. `max_pages` (int) is OPTIONAL, `calendar`-mode-only, default `0` (no pagination follow) (`web_modes/calendar.py:198`).
12. `timeout_s` (int) is OPTIONAL, meaningful to `chat` mode (default `45`, `chat.py:42`) and `ics` mode (default `20`, `ics.py:69`) — these are two independently-defaulted knobs that happen to share a field name.
13. `category` (str) — present on 24 of 31 entries in the current `data/web_sources.json` (all with value `"venues"`) — is `unread by code`: no file under `src/event_harvester/` reads the key `"category"` from a source dict (verified by repo-wide grep, zero matches). It is inert metadata.
14. `data/profiles.json`, when present, MUST be a JSON object with a top-level `"profiles"` array; each entry MUST have a non-empty, run-unique `"name"` string — `profiles.py:load_profiles` raises `ProfileError` (a `ValueError` subclass) for malformed JSON, a non-list `"profiles"` value, a non-object entry, a missing/empty `"name"`, or a duplicate `"name"` (`profiles.py:49-69`).
15. A profile MUST NOT alter `AppConfig` fields other than `cfg.web.selected_names` (set verbatim from `profile.web_sources`) and `cfg.caps` (merged via `apply_caps_to_config`) — `profiles.py:apply_profile` docstring and implementation (lines 111-132) explicitly scope this; top-level source toggles, `days_back`, and behavior flags are left untouched by design.
16. A profile's `caps` dict MAY contain any of the fixed `CapConfig` field names (`discord`/`telegram`/`gmail`/`signal`/`web`/`total`/`no_cap`) plus arbitrary per-web-sub-source names (e.g. `"luma": 25`) — `CapConfig` has no `__setattr__` guard, so `apply_caps_to_config` can add attributes dynamically; `CapConfig.get(platform)` (`config.py:188-196`) falls back to `self.web` for any name that isn't a declared dataclass field, which is what makes ad-hoc sub-source caps like `luma=25` resolvable at scoring time.

## 2. Core Concept

`data/web_sources.json` is a flat list of independently-configured scrape targets; each entry picks a **mode** (a `web_modes/*.py` handler) that determines both how the page is driven (scroll-and-scrape vs. search-and-scrape vs. calendar-link-walk vs. direct ICS fetch vs. chatbot-query) and what shape of message it emits. There is no schema file and no runtime validator beyond "code either reads a key or it doesn't" — conformance is entirely a function of which keys each `web_modes/*.py` handler happens to call `.get()` on. `data/profiles.json` is an orthogonal, optional named-preset layer on top of this: a profile narrows `web_sources.json` down to a subset by `name` (via `cfg.web.selected_names`, applied in `pipeline/harvest.py:_run_web`'s `filter_names` resolution, lines 218-253) and/or overrides per-source or per-platform caps — it never edits `web_sources.json` itself.

## 3. Specification (field tables, value vocabularies, examples)

### 3.1 `mode` value vocabulary

| Value | Handler | Auto-detected when |
|---|---|---|
| `"calendar"` | `web_modes/calendar.py:_do_calendar` | `_extract_event_links(page)` finds ≥3 date-linked links (`web_utils._detect_mode:199-201`) |
| `"feed"` | `web_modes/feed.py:_do_feed` | no search bar detected AND ≥3 feed items found (`web_utils._detect_mode:207-209`) |
| `"search"` | `web_modes/search.py:_do_search` | a search-bar UI element is detected AND the source config has a `query` set (`web_utils._detect_mode:203-205`) |
| `"raw"` | `web_modes/raw.py:_do_raw` | fallback when none of the above match (`web_utils._detect_mode:212`) |
| `"chat"` | `web_modes/chat.py:_do_chat` | never auto-detected — must be explicitly configured; dispatched before page-open in `web_fetch.py:187-193` since it needs no pre-opened page |
| `"ics"` | `web_modes/ics.py:_do_ics` | shortcut auto-detected purely from the URL shape (`webcal://`, `webcals://`, or a `.ics`-suffixed path) — bypasses all browser-based signal detection (`web_utils._detect_mode:194-197`) and is dispatched before page-open in `web_fetch.py:196-203` |
| `"steps"` | `web_modes/steps.py:_do_steps` | **never auto-detected** — must be explicitly configured, and requires a `steps` array (§3.2). **Added 2026-07-30.** Replays a recorded route through the L1 verbs. The other five browser modes are each one hardcoded walk; this one reads its walk from the config, which is what lets a site that matches none of them be added without a new module |
| absent / `"auto"` | — | triggers the detection chain above, in the fixed order: ICS-shortcut → saved signature (`uc.load_signatures`) → calendar → search → feed → raw (`web_fetch.py:219-240`) |

Once a mode succeeds with ≥1 message, `web_fetch.py:253-257` saves it as a per-page "signature" (`uc.save_signature(page, f"mode:{mode}")`) so future auto-mode runs against the same page skip re-detection.

### 3.2 Full field table (`data/web_sources.json` entries)

| Field | Type | Required | Default | Read by | Meaning |
|---|---|---|---|---|---|
| `url` | str | **yes** | — | all mode handlers (direct `src["url"]` index — `KeyError` if missing) | Target URL. `webcal://`/`webcals://` are normalized to `https://` inside `web_modes/ics.py:_normalize_url` (webcal is a subscribe-hint, not a real transport). |
| `name` | str | no | mode-specific (`"web"` / `"ics"` / `"chat"`) | all mode handlers, `web_fetch.py` (cooldown/signature keys), `pipeline/harvest.py` (`web_sources`/`web_source` filter matching, case-insensitive) | Identifies the source in logs, cooldown filtering, and `--web-source`/profile scoping. |
| `mode` | str | no | `"auto"` (triggers detection, §3.1) | `web_fetch.py:fetch_web_sources` dispatch | Handler selector. |
| `scroll_seconds` | int | no | **`5`** for `calendar`/`raw`/`search` (`web_utils.DEFAULT_SCROLL_SECONDS`); **`30`** for `feed` (hardcoded independently, `feed.py:61` — see §4); n/a for `chat`/`ics` | `web_modes/{calendar,raw,search,feed}.py` | Wall-clock seconds to scroll before extracting. |
| `cooldown_hours` | int | no | `-1` (disabled) | `web_fetch.py:fetch_web_sources:123-124` | Skip re-fetching this URL within N hours of last success. `-1` or absent = never skip on cooldown. |
| `native_chrome` | bool | no | `false` | `web_fetch.py:fetch_web_sources:132-133,286,300-301` | Route to the serial native-Chrome worker group (Cloudflare bypass path) instead of the parallel `UCBrowser` pool. |
| `api_pattern` | str (regex) | no | `"graphql\|api"` | `web_modes/feed.py:60` | Feed-mode XHR/fetch URL filter for API-response interception. |
| `api_only` | bool | no | `false` | `web_modes/feed.py:137` | Feed-mode: skip event-link/detail-page extraction, rely only on intercepted API JSON. |
| `query` | str **or list[str]** | no | none (`search`); hardcoded event-discovery prompt (`chat`, `chat.py:41`) | `web_modes/search.py:_normalize_queries`, `web_modes/chat.py:41` | Search-bar text or chatbot prompt. **Changed 2026-07-30:** `search` mode accepts a list as well as a string. |
| `queries` | list[str] | no | — | `web_modes/search.py:_normalize_queries` | **Added 2026-07-30.** Preferred spelling when a source needs more than one search. `search` mode runs one pass per query, reloading the base URL between passes, and unions the results by URL. A single query is one keyhole into a large site — `meetup` shipped as `"query": "AI meetup"` and saw only that, not because the site was hard but because the schema could hold one string. `query` and `queries` are equivalent; `queries` wins if both are set. |
| `max_pages` | int | no | `0` | `web_modes/calendar.py:198` | Calendar-mode pagination-follow limit. |
| `timeout_s` | int | no | `45` (`chat`, `chat.py:42`); `20` (`ics`, `ics.py:69`) | `web_modes/chat.py:42`, `web_modes/ics.py:69` | Per-mode operation timeout; two independent defaults sharing one field name. |
| `category` | str | no | — | **unread by code** (grep-verified, zero readers) | Free-text grouping tag (`"venues"` on 23/29 current entries). Inert. |
| `steps` | list[dict] | **yes when `mode: "steps"`** | — | `web_modes/steps.py:_do_steps` → `web_steps.replay` | **Added 2026-07-30.** An ordered route. Each entry is `{"verb": …}` from the vocabulary in §3.5; `web_steps.validate_steps` is the normative checker and is browser-free, so a route can be validated at write time. This is the first non-scalar field in the schema, and the reason it exists: every other key can hold a *destination*, none could hold a *route*. |
| `expect_min_events` | int | no | — | `web_modes/steps.py:_do_steps_inner` | **Added 2026-07-30.** Replay-rot tripwire. A route yielding fewer than this logs a warning, because a stale selector returning zero is otherwise indistinguishable from a quiet week. Warns only — it never fails the run. |
| `source_page` | str | no | — | **unread by code** — written only, like `category` | **Added 2026-07-30.** The human-facing page a feed URL was discovered from, written by `web add`/`web probe --apply` when `feed_probe` resolves a page to a different feed URL. Provenance for a reader: without it, an entry whose `url` is `webcal://wp.sf-eagle.com/events/?ical=1` or a `tockify.com` path gives no hint which venue it belongs to. |

Fields read at the `fetch_web_sources` orchestrator level but not per-source (global, from `WebConfig`/CLI, not from the JSON entry): `max_events` (int, default `30`, caps detail-page fetches per source), `timeout_ms` (int, default `30000`, Playwright navigation timeout), `parallel` (int, default `3`, `UCBrowser` worker count for non-`native_chrome` sources).

### 3.2a `steps` entry vocabulary (`sources/web_steps.py`) — **Added 2026-07-30**

Normative checker: `web_steps.validate_steps`. It rejects unknown verbs, missing required keys, **unexpected keys** (so a typo'd `"second"` fails loudly instead of being silently ignored at replay), unknown target roles, and out-of-bounds `repeat`/`seconds`. `MAX_STEPS = 40`, `MAX_REPEAT = 20`.

| verb | required | optional | Effect |
|---|---|---|---|
| `goto` | `url` | `wait_ms` | Navigate. |
| `dismiss` | — | — | Close cookie banners / modals. **Finding nothing is success** — a route keeps this step because the banner appears only sometimes. |
| `fill` | `target`, `text` | `submit` (default `true`), `wait_ms`, `optional` | Type into a resolved element. |
| `click` | `target` | `repeat`, `wait_ms`, `optional` | Click. With `repeat`, stops early when the target stops resolving — pagination running out is the end of the list, not an error. |
| `scroll` | — | `seconds` (default 8), `target`, `step_ms` | Scroll window or container. |
| `wait` | — | `ms` (default 2000) | Fixed pause. |
| `extract` | — | `what` (`links` \| `text` \| `feed_items`, default `links`), `optional` | Collect. `links` dedupes by URL across repeated extracts. |

**`target` MUST be a role, or the `selector:<css>` escape hatch.** Roles — `search`, `pagination`, `modal`, `feed`, `chat` — resolve through the matching `web_primitives.find_*` verb *at replay time*, so a site that moves its own furniture keeps working. `selector:` bypasses discovery entirely and is the brittle option by design: routes that use it are the ones that rot first.

Any step MAY carry `"optional": true`, which lets it fail without ending the route. Every other failure stops the walk, because continuing past a step that did not happen produces results that look real and are not.

### 3.3 `WebConfig` (`config.py:125-147`) — env-var-driven, applies to the whole harvest run, not per-source

| Field | Env var | Default |
|---|---|---|
| `sources_file` | `WEB_SOURCES_FILE` | `"data/web_sources.json"` |
| `max_events` | `WEB_MAX_EVENTS` | `30` |
| `timeout_ms` | `WEB_TIMEOUT_MS` | `30000` |
| `no_cooldown` | `WEB_NO_COOLDOWN` | `False` |
| `selected_names` | `WEB_SOURCE_NAMES` (CSV) | `[]` (all sources) — also settable by a profile or the CLI `--web-source`/`--web-sources` flags |
| `parallel` | `WEB_PARALLEL` | `3` |
| `debug_trace` | `WEB_DEBUG_TRACE` | `False` |
| `debug_screenshots` | `WEB_DEBUG_SCREENSHOTS` | `False` (implies `debug_trace=True` when set, `config.py:337-338`) |

Note: `sources_file` is read into `WebConfig.sources_file` from the env var, but `web_utils._load_web_sources()` — the actual function every mode-dispatch path calls — reads a **module-level constant** `_WEB_SOURCES_FILE = _DATA_DIR / "web_sources.json"` (`web_utils.py:68`), computed once at import time from the repo-root-relative `_DATA_DIR`, and never consults `WebConfig.sources_file` / `WEB_SOURCES_FILE` at all. See §4 — this env var is currently inert for the primary load path.

### 3.4 `data/profiles.json` schema

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

| Field | Type | Required | Meaning |
|---|---|---|---|
| `name` | str | yes, unique within file | Profile identifier, looked up case-sensitively (`profiles.py:find_profile:79-84`). |
| `description` | str | no (default `""`) | Display-only. |
| `web_sources` | list[str] | no (default `[]` = all sources) | Sets `cfg.web.selected_names` verbatim, used as a name filter in `harvest.py:_run_web`. **A filter that matches nothing now selects nothing** (fixed 2026-07-30). The literal `"__none__"` (used by the shipped `"no-web"` profile) is still not special-cased — it works because no real source is named `__none__` and a zero-match filter is now honoured as zero. Note the distinction the code relies on: `sources=None` means "load them all", `sources=[]` means "none". |
| `caps` | dict | no (default `{}`) | Merged into `cfg.caps` via `apply_caps_to_config`; keys may be any `CapConfig` field name or an arbitrary web-sub-source name. |

`apply_profile_by_name(cfg, name)` (`profiles.py:87-108`) is the only entry point that resolves a profile by name and applies it; it returns `False` and logs (does not raise) if the profile or file is missing.

## 4. Non-conformance observed (as-built deviations, each with file:function)

- ~~**BUG-WEBSRC-1 — the `"no-web"` profile does not actually disable web harvesting.**~~ ✅ **Fixed 2026-07-30.** `harvest.py:_run_web` treated `cfg.web.selected_names` as a filter, found zero matches for `__none__`, logged a "not found" warning, and — critically — **never set `web_kw["sources"]`**, so `fetch_web_sources` fell back to its `sources=None` default meaning *all* configured sources. The accompanying `"caps": {"web": 0}` kept those messages out of the LLM stage, which is why nobody noticed: the browser automation still ran against every configured URL and the results were then discarded.

  The filter result is now assigned even when empty. The blast radius was larger than the profile: **any typo'd `--web-source` did the same thing** — `--web-source lumaa` ran a full 29-source web harvest instead of erroring or doing nothing. Pinned by `tests/test_harvest_async.py::TestWebSourceFilter`, which asserts the empty case explicitly rather than just the matching one.

- **Non-conformance — `WEB_SOURCES_FILE` / `WebConfig.sources_file` is inert for the actual load path.** `config.py:WebConfig.sources_file` (default `"data/web_sources.json"`, overridable via `WEB_SOURCES_FILE` env var) is set into `AppConfig` by `load_config()` but is never read by `web_utils._load_web_sources()`, which instead resolves its own independent module-level `_WEB_SOURCES_FILE` constant from `Path(__file__).resolve().parent.parent.parent.parent / "data" / "web_sources.json"` (repo-root-anchored, `web_utils.py:28,68`). Every call site that loads web sources (`web_fetch.py:fetch_web_sources`, `pipeline/harvest.py:_run_web`, `cli/commands/web.py`) goes through `_load_web_sources()`, not through `cfg.web.sources_file`. Setting `WEB_SOURCES_FILE` in the environment currently has no effect on which file is actually read.

- **Non-conformance — `scroll_seconds` has two silently different defaults.** `web_utils.DEFAULT_SCROLL_SECONDS = 5` (`web_utils.py:252`) is imported and used as the fallback by `calendar.py`, `raw.py`, and `search.py`. `feed.py:_do_feed_inner` (line 61) instead does `scroll_secs = src.get("scroll_seconds", 30)` — a locally hardcoded `30`, not a reference to `DEFAULT_SCROLL_SECONDS` — so a `feed`-mode source with no explicit `scroll_seconds` scrolls for 6x longer than a `calendar`/`raw`/`search` source with no explicit value, with no comment in the code explaining the discrepancy is intentional (feed's own docstring only explains *why feed keeps its own dwell loop*, not why the numeric default differs).

- **Non-conformance — `category` is fully inert.** 24 of the 31 entries currently in `data/web_sources.json` carry `"category": "venues"`; a repo-wide grep for the literal string `"category"` under `src/event_harvester/` returns zero matches. The field is written but has no reader anywhere in the codebase today — `unread by code` per the task's own vocabulary.

- **Non-conformance — `_DEFAULT_COOLDOWN` constant is effectively dead.** `web_utils.py:30` defines `_DEFAULT_COOLDOWN = timedelta(hours=1)` as the default parameter value for `_is_on_cooldown(url, cooldown=_DEFAULT_COOLDOWN)`. The only real call site, `web_fetch.py:fetch_web_sources:124`, always passes an explicit `timedelta(hours=cd_hours)` computed from `src.get("cooldown_hours", -1)` and gates the call behind `cd_hours >= 0` — so `_is_on_cooldown`'s own default parameter is never actually exercised in the wired code path; the *effective* default cooldown behavior is "disabled" (`-1`), not the 1-hour value the constant's name suggests.
