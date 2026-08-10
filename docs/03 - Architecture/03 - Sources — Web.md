# Sources — Web

**Status:** CURRENT · **Date:** 2026-08-06 (added `instagram.py` + `mode: "instagram"`) · **Applies to:** `src/event_harvester/sources/web_fetch.py`, `uc_browser.py`, `web_session.py`, `web_utils.py`, `feed_probe.py`, `instagram.py`, `web_steps.py`, `web_explore.py`, `src/event_harvester/sources/web_modes/{__init__,calendar,feed,search,raw,ics,chat,steps,instagram}.py`, `src/event_harvester/web_primitives.py`, `src/event_harvester/observability/hints.py`, `data/web_sources.json`, `ext/helpers/extract_event_links.js` · **Depends on:** `docs/04 - Reference/Universal Controller Guide.md`

The web fetch path turns `data/web_sources.json` into a list of message dicts by driving one or more headed Chromium/Chrome instances through Universal Controller (UC), routing each source through a mode-specific handler in `web_modes/`. `web_fetch.fetch_web_sources` is the sole entry point: it splits sources into a parallel pool of "standard" browsers plus one serial pool for `native_chrome` (Cloudflare) sources, and retries Cloudflare-blocked standard sources through native Chrome afterward. Everything downstream (`pipeline/harvest.py`) treats the return value as a flat `list[dict]` of chat-shaped messages, regardless of which mode produced them.

## Hard constraints

| Constraint | Value | Where |
|---|---|---|
| Parallel standard browsers | `cfg.web.parallel`, default **3** (~200-300MB each) | `config.py:WebConfig.parallel`, env `WEB_PARALLEL` |
| Native-chrome (Cloudflare) sources | Always **1 serial group**, regardless of `parallel` | `web_fetch.py:fetch_web_sources` — `native_srcs` bucket |
| Per-source cooldown | Default **1 hour**; per-source override via `cooldown_hours` | `web_utils.py:_DEFAULT_COOLDOWN`, `_is_on_cooldown` |
| Detail-page parallel fetch | **5 tabs** at a time (batched) | `web_utils.py:_fetch_event_details_parallel(max_workers=5)` |
| Default scroll dwell | **5 seconds** unless `scroll_seconds` set per source | `web_utils.py:DEFAULT_SCROLL_SECONDS` |
| Page navigation timeout | `cfg.web.timeout_ms`, default **30000ms** | `config.py:WebConfig.timeout_ms` |
| Content truncation per message | Calendar/search/feed detail: 5000 chars; raw/calendar-fallback: 8000 chars; chat: 8000 chars; ICS description: 2000 chars | per-mode `content[:N]` slices |

## Module: `feed_probe.py` — find a feed before reaching for a browser

**Added 2026-07-30.** **Domain:** given a page URL, find a machine-readable calendar feed without launching a browser. **Runs at:** `web add`, `web test`, and `web probe` (never during a harvest). **Inputs:** a URL. **Outputs:** a ranked list of validated `FeedCandidate`s, or `[]`.

**Why it exists.** Small venues do not hand-roll their events page; they run a platform, and every one of those platforms already publishes iCalendar at a predictable URL. Two pre-existing sources prove it — `powerhouse` uses `powerhousebar.com/events/?ical=1`, which is not a Powerhouse fact but The Events Calendar WordPress plugin's standard export. **Recognising the platform is worth more than recognising the site.** A hit converts a browser-driven `raw`/`calendar` source into an `ics` one: faster, more reliable, immune to Cloudflare, and already a supported mode.

**Internal structure.** Four discovery lanes in decreasing order of trustworthiness, each its own span:

| Lane | Origin tag | What it looks at |
|---|---|---|
| 1 | `link-rel` | `<link rel="alternate" type="text/calendar">` — the standard |
| 2 | `embed` | iframes pointing at a calendar platform; a Google Calendar embed carries the calendar id in `?src=`, which *derives* the ICS URL rather than guessing it |
| 3 | `anchor` | visible links ending `.ics` or carrying `ical=1` / `format=ical` |
| 4 | `platform` | HTML fingerprint (Tribe/The Events Calendar, Events Manager, Tockify, Squarespace, generic WordPress) → that platform's known export URLs |

Blind probes of four conventional paths run **only when the first four lanes found nothing**, so a site that declares its feed is never speculatively poked.

**Validation is the filter, and that is the load-bearing design choice.** Nothing is trusted for matching a pattern — every candidate is fetched and parsed, and counts only if `icalendar` parses it and it holds ≥1 VEVENT. That is what makes the speculative pattern table safe: a wrong guess costs one request and is discarded, so the table can be optimistic without lying to the caller. A WordPress soft-404 is a 200 with HTML, so there is also a cheap `BEGIN:VCALENDAR` check before the parser is handed anything.

**Hard constraints:** ≤12 validations per probe (a venue matching every pattern would otherwise fire ~15 requests at a small business's server); 2MB streamed cap on the page fetch; `event-harvester/1.0 (+feed-probe)` UA.

**`known rough edge`:** HTML scanning is regex-based — the project has no HTML-parser dependency and pulling one in for four attribute lookups was not worth it. Regex over HTML is fragile; it is tolerable *here* only because every candidate is validated by fetching it, so a missed match degrades to "no feed found", never to a bad source entry.

**Failure mode found by running it — per-event exports.** Squarespace puts an "add to calendar" link at every event's permalink, and each one is valid iCalendar containing exactly that event. Probing `studsf.com/calendar` first returned
`/calendar/2026/7/30/140-olivia-lauren-amp-friends?format=ical` — one event. Adopting it would have replaced a working calendar source with a feed pinned to one night in July. Two rules now reject these:

1. **Dated permalinks** (`/2026/7/30/`) are dropped at *discovery*, so they never cost a request.
2. **Undated opaque slugs** (`/calendar/h4wr7zbsakm3fye-…?format=ical`) have nothing in the URL to match on. They are dropped after validation when a candidate is both nested under the probed page's path *and* contains ≤1 event — a real collection feed is rarely both. A genuine `/calendar/all.ics` with several events survives.

Accepted cost: a venue whose only feed legitimately holds one upcoming event reads as "none". Reporting nothing beats adopting a feed that will be permanently stale.

**Measured result** (`web probe` over all 27 non-ICS sources, 2026-07-30): **1 hit** — `sf-eagle`, 27 upcoming events via `webcal://wp.sf-eagle.com/events/?ical=1`. The 11 `raw` venues are bare homepages for bars that mostly use Instagram or bespoke designs rather than a calendar platform, so the platform-fingerprint payoff is much smaller here than the Powerhouse/DNA Lounge precedent suggested. The probe is cheap and correct; the SF gay-bar segment simply is not WordPress-shaped.

## Module: `instagram.py` — record the query, then ask it yourself

**Added 2026-08-06.** **Domain:** given a handle, return that account's recent posts. **Runs at:** harvest (`mode: "instagram"`), plus a browser-free discovery half at `web instagram`. **Inputs:** a UC browser with a logged-in session, and a list of accounts. **Outputs:** message records.

**Why it exists.** §4.2 of the roadmap: the 11 bare-homepage `raw` venues do not run a calendar platform, so `feed_probe` cannot reach them — SF gay bars publish their calendar to Instagram. The pre-existing Instagram source could not serve them either, because it read the **home feed**: Meta ranks it, so "did we see this week's Oasis lineup" had no answer, only a probability. Naming the accounts is what turns Instagram from a lottery into a source.

**The GraphQL half.** Instagram's web client talks to `/graphql/query` with *persisted queries* — a numeric `doc_id`, a JSON `variables` blob, and a dozen session parameters (`lsd`, `jazoest`, `__csr`, `av`). Nothing here is hardcoded. `QueryRecorder` attaches to the page **before** navigation, records the requests the real client issues, and `GraphQLQuery.with_cursor` replays one with `variables.after` patched to the cursor found in the last response — which is precisely what the page does when you scroll, minus the scrolling.

**Recording rather than hardcoding is the load-bearing choice.** Every hand-rolled Instagram client dies the day Meta rotates a `doc_id` or adds a required parameter. Here those parameters are whatever the page just sent, because the recorded body is reused verbatim and exactly one field is changed. A rotation costs one run's worth of staleness, not a rewrite. The replay runs as `fetch(..., {credentials: "include"})` **inside the page** rather than through a Python HTTP client, so cookies, TLS fingerprint and origin belong to the session that was already there.

**Two walkers were replaced by one, and the old one had a silent hole.** The pre-existing extractor keyed on *container* shape — `edges` and `items` — and recursed only through dict values, **never through lists**. A payload nesting posts one array deeper (which is what the deferred timeline does) yielded zero posts and no error, indistinguishable from a venue that posted nothing. `iter_media` keys on the *object* instead — a dict carrying a caption and an id is a post, wherever it sits — and walks lists. It yields in document order, because a timeline arrives newest-first and every cap downstream trusts that.

| Hard constraint | Value | Why |
|---|---|---|
| Replayed pages per account | `max_pages` (default 1), hard cap `MAX_PAGES_HARD_CAP = 8` | Each is a request Instagram did not ask for; a config typo should not become a hundred |
| Accounts per run | `max_accounts`, default **25** | 25 accounts is already ~3 min of paced loading |
| Delay between requests | `request_delay_s` **2.5s**, `account_gap_s` **3s** | Rate discipline is the design constraint, not throughput |
| Posts kept per account | `max_posts`, default 24 | Trimmed again by `max_events` — see below |
| Caption floor | 10 chars | Below that it is an emoji, not an announcement |

**A login wall raises rather than returns empty**, and stops the whole source. Empty and logged-out are the same `[]` downstream, and only one of them is fixed by `web login`; walking twenty profiles into the same wall is also the pattern that gets a session challenged.

**`max_events` (30) against 17 venues is the interesting cap.** Concatenate-and-truncate would spend the entire budget on the first two accounts in config order, so the same two venues would be the only ones ever seen and the other fifteen would look like they never post. `_interleave` takes every venue's newest post before any venue's second. The shipped config therefore sets `max_pages: 0` — a profile load already carries ~12 posts and the trim keeps ~2, so a replayed page would be fetched and discarded. Pagination earns its keep on a *narrow* account list (one venue, a fortnight of history), which is what the knob is for.

**`unverified`: the replay path has not been run against a live logged-in session** — that needs credentials this repo does not hold. It is written to degrade to the recorded first page (the old behaviour, parsed better) on a non-200, a non-JSON body, or a missing cursor, and `tests/test_instagram.py` pins that degradation with a fake page. The recording half, the walker, the interleave, and discovery are all exercised; the live `fetch` is not.

### Handle discovery — `discover_handles`, and why it cannot validate

The sibling of `feed_probe`, for the segment `feed_probe` measured itself unable to serve. Venues link their own Instagram from their own footer, so the account list is **read off the venues, never guessed** — and guessing is the specific thing to avoid, because Oasis is `@theoasissf`, not `@sfoasis`, and a wrong handle harvests a stranger's posts forever without ever erroring.

The structural difference from `feed_probe` is that **validation is impossible here**. A feed candidate is proven by fetching and parsing it; a handle cannot be. Checked 2026-08-06: logged out, `instagram.com/<anything>/` returns **200 with an identical JS shell** for a real account and for `zzzznotarealhandle999` alike. Ranking therefore has to carry the weight validation carries there:

- **Reserved-path and shape filters** drop `/p/`, `/explore/`, and CDN fragments like `t51.2885` that match the same regex.
- **Affinity** — the longest run of characters shared with the venue's name or domain, punctuation-insensitive — is the ranking key, and `MIN_AFFINITY = 5` is the adoption threshold.

**Frequency alone would have been wrong**, which is the finding that set the threshold: `beauxsf.com` links `@beauxdragbrunch` 18 times and its own `@lesbeauxsf` twice. Affinity picks the venue; count picks the party it promotes.

**Measured** (`web instagram --category venues`, 2026-08-06): **17 of 23** venue sites name an account clearly enough to adopt, against `feed_probe`'s 1 of 27 on the same segment. The six misses (`hi-tops`, `badlands`, `the-edge`, `wild-side-west`, `mothership`, `powerhouse`) link no handle in server-rendered HTML.

`known rough edge`: like `feed_probe`, the scan is regex over HTML with no parser dependency. Unlike `feed_probe`, a bad match cannot be caught by validation — only by the affinity threshold and a human reading the `unsure` lines. `--apply` adopts confident matches only and prints the rest.

## Modes

**The modes are** `calendar`, `feed`, `search`, `raw`, `ics`, `chat`, `steps` (2026-07-30), and `instagram` (2026-08-06). Each has its own file, OTel span, and dispatch branch in `fetch_web_sources`; `web_modes/__init__.py` re-exports them and `__all__` is the count.

*Two places once said "five" and were fixed 2026-07-29. The durable half of that fix was deleting the number from the `web_modes/__init__.py` docstring and pointing at `__all__` — this section is now on its fourth count, which is the argument.*

**`steps` is the mode the other browser modes are special cases of.** `calendar`, `feed` and `search` each encode one fixed walk in Python; `steps` reads its walk from the source config (`web_steps.replay`), so a site matching none of the three no longer needs a new module — it needs a route. It is never auto-detected, and `web_utils._detect_mode` is unchanged: a route only runs when someone configured one.

| Mode | Domain / what it does | When picked | Runs at | Key tunables |
|---|---|---|---|---|
| `calendar` | Load page, extract date↔link pairs (DOM-graph or Calcium HTML), fetch each detail page in parallel | `mode: "calendar"`, or auto-detect finds ≥3 event links | Browser page (Chromium+UC or native Chrome) | `scroll_seconds`, `max_pages` (pagination), `native_chrome`, `cooldown_hours` |
| `feed` | Re-open page with response interception, scroll while capturing XHR/GraphQL, parse posts from captured JSON and/or extracted links | `mode: "feed"` | Browser page, own re-opened tab (`open_with_intercept`) | `api_pattern` (regex), `scroll_seconds`, `api_only` (skip link-extraction path) |
| `instagram` | Open each configured account's profile, replay the recorded GraphQL query for further pages, normalise posts | `mode: "instagram"` — **never auto-detected** | Own page per account (recorder attached pre-navigation); dispatched **before** the shared page-open | `accounts`, `max_pages`, `max_posts`, `max_accounts`, `request_delay_s`, `account_gap_s`, `include_home_feed` |
| `search` | Detect search bar, fill `query`, scroll, extract event links from results | `mode: "search"`, or auto-detect finds a search box + `query` set | Browser page | `query`, `scroll_seconds`, `max_events` |
| `raw` | Load page, scroll, strip HTML to text — last-resort fallback | `mode: "raw"`, or auto-detect finds nothing else | Browser page | `scroll_seconds` |
| `ics` | Fetch an iCalendar feed over plain HTTP(S) via `requests`, parse `VEVENT`s with `icalendar`, one message per event | `mode: "ics"`, or URL is `webcal://`/`webcals://`/`*.ics` (shortcut in `_detect_mode`) | **No browser** — plain HTTP in the worker thread | `timeout_s` (default 20) |
| `chat` | Open an AI chat UI, send `query`, anchor-lock and poll for the response | `mode: "chat"` | Browser page | `query`, `timeout_s` (default 45) — requires prior `--web-login` |

Auto-mode detection (`web_utils.py:_detect_mode`) order: ICS URL shortcut → `_extract_event_links` ≥3 → `uc.detect(page, "search")` + `src.get("query")` → `uc.get_feed_items` ≥3 items >30 chars → fallback `raw`. A saved UC "signature" (`mode:<name>`) from a prior successful run is checked *before* auto-detection runs (`web_fetch.py:220-236`).

**Undocumented-mode finding:** `ics` doesn't need a browser at all, but `fetch_web_sources`'s outer dispatch still enters the `_process_group`/`UCBrowser` context and iterates the source inside it — the `ics.py` module docstring itself flags this: "~1s of wasted setup per ICS source until we refactor to skip browser allocation for browser-free modes" (`ics.py:8`). `chat` sources are dispatched the same way — before the page-open branch — so they also skip the shared `_wait_cloudflare`/auto-detect path, but unlike `ics` they still open a page via `uc.open(...)` inside `_do_chat`. `instagram` (2026-08-06) joins that group for a third reason: it opens one page *per account* with a recorder attached before navigation, so a shared pre-opened page would be a page it immediately closes — which is exactly what `feed` mode still does and documents as interface-consistency overhead.

## Parallel-fetch model and why native-chrome stays serial

`fetch_web_sources` (`web_fetch.py:85-317`):

1. Load sources (explicit list or `data/web_sources.json` via `_load_web_sources`).
2. Drop sources on cooldown (`_is_on_cooldown`), unless `no_cooldown=True`.
3. Split into `standard_srcs` (no `native_chrome` flag) and `native_srcs` (`native_chrome: true`).
4. `_chunk_sources(standard_srcs, parallel)` round-robins standard sources into up to `parallel` chunks (empty chunks dropped) — round-robin exists specifically so slow feed/calendar sources don't all land on one worker.
5. A `ThreadPoolExecutor` runs one `_process_group` call per chunk, **plus one more** for the native group if non-empty — so total workers = `len(standard_chunks) + (1 if native_srcs else 0)`.
6. Each `_process_group` opens exactly **one `UCBrowser`** (one Playwright instance, one Chromium/Chrome process) and iterates its sources **serially** inside that `with UCBrowser(...) as uc:` block.
7. Any standard-group source that hits a Cloudflare "just a moment" page (and isn't already running with `native_chrome=True`) is queued into `cf_retries` instead of failing — closed without processing.
8. After all parallel groups finish, `cf_retries` (deduplicated per group, then flattened) are retried **once**, serially, in a fresh `native_chrome=True` `_process_group` call.

Native Chrome (`BrowserMode.NATIVE_CDP` in `ext/universal_controller/uc_browser/browser.py:_start_native_chrome`) launches the user's actual `chrome.exe` via `subprocess.Popen` with `--remote-debugging-port` and connects Playwright over CDP — it is **not** a second Playwright-launched browser process pool. Because there is one real Chrome window/profile involved (`data/.uc_chrome_profile`), and because native Chrome exists specifically to look like a real user to Cloudflare, the code deliberately keeps this group to a single serial worker — running several native-Chrome instances concurrently from one profile would both contend for the profile lock and defeat the "looks like one real user" premise that lets it pass Cloudflare's challenge in the first place. `parallel` only ever affects the standard-source chunking; the native bucket size is hardcoded to 1 group (`fetch_web_sources` never multiplies native workers by `parallel`).

## `data/web_sources.json` entry schema

One JSON array of source objects (`_load_web_sources` in `web_utils.py:83-88`, fallback `_DEFAULT_SOURCES` at `web_utils.py:71-80` if the file is absent). Fields actually read by the code, by consumer:

| Field | Type | Read by | Meaning |
|---|---|---|---|
| `url` | str | `fetch_web_sources`, all modes | Source URL. `webcal://`/`webcals://` and `*.ics` trigger ICS auto-detect. |
| `name` | str | everywhere (logging, message `author`/`channel`, span attrs) | Display/source name; falls back to `url` if absent. |
| `mode` | str \| omitted/`"auto"` | `fetch_web_sources` dispatch | One of `calendar`/`feed`/`search`/`raw`/`ics`/`chat`, or auto-detected. |
| `scroll_seconds` | int | `calendar`, `search`, `raw` (via `_scroll_for_seconds`), `feed` (own dwell loop) | Wall-clock scroll dwell in seconds. Default `DEFAULT_SCROLL_SECONDS = 5` for calendar/search/raw; `feed` defaults to `30` internally (`feed.py:61`) if unset. |
| `api_pattern` | str (regex) | `feed` only (`uc.open_with_intercept`) | Response-URL regex to intercept as JSON. Default `"graphql|api"` if unset. |
| `api_only` | bool | `feed` only | Skip the `_extract_event_links` fallback path — used when links on the page aren't event pages (e.g. Instagram profile links). |
| `query` | str | `search` (search box fill), `chat` (message sent) | Search query / chat prompt text. |
| `native_chrome` | bool | `fetch_web_sources` grouping | Routes the source into the serial native-Chrome bucket instead of the standard parallel pool. |
| `cooldown_hours` | number (default `-1`, i.e. cooldown check skipped in the `_is_on_cooldown` call path *unless* `>= 0`) | `fetch_web_sources` (pre-filter step) | Per-source override of the 1-hour default cooldown window. `-1` (absent) effectively disables cooldown filtering for that source — see honesty marker below. |
| `max_pages` | int (default `0`) | `calendar` only | Number of pagination "next" clicks to follow after the first page. |
| `timeout_s` | int | `ics` (HTTP timeout, default 20), `chat` (response wait, default 45) | Per-mode timeout override; unrelated to the shared `timeout_ms` browser-nav timeout. |
| `accounts` | list[str \| dict] | `instagram` only (`web_modes/instagram.py:_normalize_accounts`) | Handles to walk. Accepts `"studsf"`, `"@studsf"`, a profile URL, or `{"handle": …, "name": …}` — the dict form keeps the venue's name, which `@theoasissf` alone would not tell you. Written by `web instagram --apply`. |
| `max_posts` / `max_accounts` / `request_delay_s` / `account_gap_s` / `include_home_feed` / `home_max_pages` | int / int / float / float / bool / int | `instagram` only | Per-account post cap (24), account-list ceiling (25), delay between replays (2.5s) and between accounts (3s), whether to also read the ranked home feed (defaults to *true only when `accounts` is empty*), home-feed page count (2). |
| `category` | str | `cli/commands/web.py:_web_instagram` — **`--category` scan filter only**; still unread on the harvest path | Present on ~24 of 31 entries (all `"venues"`). Since 2026-08-06 it has exactly one reader, and it is a CLI filter rather than harvest behaviour: `web instagram --category venues` scopes the scan so a ticketing platform's corporate account is not adopted as if it were a venue's calendar. |

**BUG (confirmed by `tests/test_web_fetch_helpers.py:test_load_web_sources_when_file_missing_returns_empty_dict`):** `_load_web_sources()` calls `load_json(_WEB_SOURCES_FILE, default=None)`. `event_harvester.utils.load_json` internally does `return default if default is not None else {}` on a missing/corrupt file — so passing `default=None` actually makes a missing file return `{}`, never `None`. `_load_web_sources`'s own fallback check (`return sources if sources is not None else list(_DEFAULT_SOURCES)`) therefore never fires: a missing `data/web_sources.json` silently yields an **empty dict**, not the 7-source `_DEFAULT_SOURCES` list the docstring promises. Currently latent because the file exists in the repo; would bite anyone who deletes/renames it.

Also note: the live `data/web_sources.json` (31 entries) diverges from the in-code `_DEFAULT_SOURCES` fallback (7 entries) — e.g. `erobay`'s URL differs (full Calcium query string in the JSON file vs. bare root in the fallback) and several venue sources (`mothership`, `dnalounge`, `powerhouse`, `stud`, ...) exist only in the JSON file. The fallback list is stale relative to the real config; that's expected (it's a bootstrap default) but worth knowing it won't reproduce current behavior if you're debugging from `_DEFAULT_SOURCES` alone.

## Session / cookie storage locations

| Path | Written by | Used by |
|---|---|---|
| `data/.playwright_state.json` | `web_session.py:web_login` (`context.storage_state(...)`) after manual login in a real, non-automated Chrome | `BrowserMode.HEADLESS` (`_start_headless`, loads as `storage_state`) and `BrowserMode.CHROMIUM_EXT` (`_start_chromium_ext`, reads `state["cookies"]` and injects via `context.add_cookies`) |
| `data/.chrome_profile/` | `web_login`'s `launch_persistent_context` (real Chrome channel) | `BrowserMode.CHROME` (`_start_chrome`) — persistent logged-in profile |
| `data/.uc_chromium_profile/` | `_start_chromium_ext`'s `launch_persistent_context` | `BrowserMode.CHROMIUM_EXT` — Playwright's bundled Chromium + UC extension, separate from `.chrome_profile` |
| `data/.uc_chrome_profile/` | `_start_native_chrome`'s `subprocess.Popen(chrome.exe --user-data-dir=...)` | `BrowserMode.NATIVE_CDP` — a clean profile (only the KeePassXC-Browser extension staged in, not a full copy of the user's real Chrome profile — see `_copy_chrome_auth`'s docstring noting this was deliberately changed) |
| `data/.keepassxc_extension/` | `_ensure_keepassxc_extension` (staged copy of the user's installed KeePassXC-Browser extension, `_metadata` stripped) | Loaded via `--load-extension` in native-Chrome mode only, for KeePassXC autofill |

`uc_browser.py` in `src/event_harvester/sources/` is a **backward-compat shim** — it just re-exports `UCBrowser`/`BrowserMode` from the installed `uc_browser` package (`ext/universal_controller/`, editable pip install). All real browser-launch logic lives in `ext/universal_controller/uc_browser/browser.py`. `web_session.py` similarly re-exports `get_chrome_cookies`/`cookies_to_playwright` from `uc_browser.chrome_cookies` for legacy callers; the module's own remaining job is `extension_args()`, `wait_for_uc`, `apply_uc_patterns`, and the `web_login()` interactive flow.

## Per-source cooldown

`web_utils.py:_load_cooldowns`/`_save_cooldown`/`_is_on_cooldown` read/write `data/.web_cooldowns.json` — a flat `{url: iso_timestamp}` map of last-successful-fetch times. `fetch_web_sources` filters sources against this **before** dispatch (`cd_hours = src.get("cooldown_hours", -1)`; skipped only if `cd_hours >= 0` and within the window — so a source with no `cooldown_hours` key is *never* cooldown-filtered, only ones that explicitly opt in with a non-negative value). A cooldown timestamp is only saved on a *successful* fetch (`if msgs: _save_cooldown(src["url"])`) inside `_process_group`, for every mode including `chat` and `ics`. `no_cooldown=True` (CLI/config flag) bypasses the whole filter regardless of per-source settings.

## L1 primitive verbs (`web_primitives.py`)

A typed vocabulary (`Element` dataclass + `find_*`/action/extract verb functions) wrapping UC's pattern detection and Playwright actions, so callers never touch raw selectors directly.

~~**`dead code?`** — the only importer is `tests/test_web_primitives.py`~~ — **no longer true as of 2026-07-30.** `sources/web_steps.py` is the production caller its docstring always anticipated: `resolve_target` dispatches five `find_*` verbs by role name, and `_run_one` calls `fill_input`, `click`, `scroll`, `dismiss_modal`, `extract_links` and `extract_text`. The `_do_*` handlers still do not call it — they remain hardcoded walks — but `mode: "steps"` reaches the page *only* through this vocabulary.

Still true: it re-implements things the `_do_*` handlers do inline (`find_pagination`'s heuristic duplicates the pagination JS in `calendar.py:_do_calendar_inner`). Collapsing the older modes onto routes would remove that duplication and is not scheduled.

Verbs, by category:

| Category | Verbs | Notes |
|---|---|---|
| Discovery | `find_search_box`, `find_event_cards`, `find_pagination`, `find_login_wall`, `find_modal`, `find_feed_container`, `find_chat_input` | All return `Element \| None` (or `list[Element]` for cards); go through `_arbitrate` (currently just picks highest `confidence` — VLM-arbiter fallback is stubbed, `CONFIDENCE_THRESHOLD = 0.5` unused pending `event_harvester.llm.vision_completion` hookup per the `TODO(phase-2)` in `_arbitrate`). |
| Action | `click`, `fill_input`, `scroll`, `dismiss_modal`, `wait_for_navigation` | Thin Playwright wrappers; all swallow exceptions and return bool/int rather than raising. |
| Extraction | `extract_text`, `extract_event`, `extract_links`, `screenshot` | `extract_links`/`find_event_cards` both delegate to `_extract_event_links` (the same DOM-graph walker used by the live modes). |

## Routes: authoring vs replay (`web_steps.py`, `web_explore.py`)

Added 2026-07-30. The two halves of web scraping have opposite economics, so they get opposite mechanisms.

| | Authoring (`web_explore.py`) | Replay (`web_steps.py`) |
|---|---|---|
| Runs | once per site | every harvest, every source |
| Driver | an agent, over 9 MCP tools | `web_modes/steps.py` |
| Browser | held open across calls | opened by the normal dispatch |
| LLM | yes — that is the point | **no**, unless a step fails |
| Output | a `steps` array | messages |

`ExploreSession` keeps a `UCBrowser` entered manually (not via `with`) and reaped after `IDLE_TIMEOUT_S = 900`, capped at `MAX_SESSIONS = 3`. **Only successful steps are recorded** — replay cannot distinguish an attempt from an event, so a route made of attempts would be a liability. The opening `goto` is recorded as step 0, which means a walk with no further actions crystallises into exactly the pre-2026-07-30 schema shape: `{url, name, mode}`. That is the erobay case, and the reason erobay looked "crystallised" while Luma did not.

`replay()` stops at the first non-`optional` failure and attaches a live diagnosis (below). Partial results are kept: a route that broke on its last step still returns the links the earlier steps found.

## Live page diagnosis (`observability/hints.py`)

Added 2026-07-30. Distinct from `nav_quality.py`, which scores a *finished* run from a trace on disk; this diagnoses an *open* page while there is still something to do about it.

Two layers, deliberately ordered. `collect_signals` runs **one** `page.evaluate` returning text length, link/input/dialog/iframe counts, a password-field flag, next-control count, and scroll geometry; `rule_hints` turns that into ranked `Hint`s, each carrying a `suggested_step` in the `web_steps` vocabulary where one exists. Only when the top rule sits at or below `VLM_ESCALATION_THRESHOLD = 0.6` (or the caller passes `use_vlm=True`) is a screenshot taken and one `vision_completion` call made. A VLM call per page would be affordable while authoring and ruinous during a harvest.

When the picture and the DOM disagree, the picture wins — that asymmetry is the reason for taking it — but the rules' `suggested_step` is kept underneath, because that is the actionable half.

`known rough edge`: the rules are regex-and-threshold heuristics over page text, and two of them were wrong in ways only a live page revealed (see `07 - Status Reports/2026-07-30_routes-and-hints.md`). Treat a rule verdict as a lead, not a fact.

## Extracted content → message dicts

Every mode produces the same message shape consumed by the downstream pipeline:

```python
{
    "platform": "web",       # or per-mode variants: name for feed page-text, "web" for chat/ics
    "id": "web:<hash-or-uid>",
    "timestamp": "<iso8601>",
    "author": "<domain-or-name>",
    "channel": "<url-or-name>[:120]",
    "content": "<text>[:N]",
}
```

Two extraction paths feed `content`, tried in this order wherever a detail page is fetched (`_fetch_event_detail`/`_fetch_event_details_parallel` in `web_utils.py`, and inline in `_do_ics_inner`):

1. **Structured data first** — `_extract_structured_data` looks for `schema.org` `Event` JSON-LD (`<script type="application/ld+json">`, including `@graph`-wrapped arrays and list-valued `@type`), pulling `title`/`date`/`time`/`location`/`link`/`details` and formatting them as newline-joined `key: value` lines. No LLM involved.
2. **Fallback to raw text** — `_extract_text` strips `<script>`/`<style>` blocks then all remaining tags, collapses whitespace. Used whenever JSON-LD is absent, or for whole-page raw/calendar-fallback content. `_decimate_text` (used elsewhere in the pipeline, not inline in these fetchers) centers a window around the densest date/time-anchor cluster when text exceeds `max_chars`, so long pages don't drown the eventual LLM extraction step in nav/footer junk.

Dead/removed event pages are filtered via `_is_dead_page` (regex over the first 3000 chars — "deleted", "cancelled", "no longer available", "404", "event not found") before either extraction path runs; a hit returns `None` and the link is dropped rather than turned into a message.

Before detail pages are even fetched, `_filter_known_links` drops links whose fingerprint (`event_harvester.pipeline.event_match`) is already `approved` or `declined` — matched by exact URL first, then a fuzzy title+date score ≥2. Fingerprints with no `status` (or `status: "auto"`) are deliberately **not** treated as "known" — `_save_link_fingerprint` is now a documented no-op (`web_utils.py:115-127`) specifically because eagerly fingerprinting every fetched page used to hide 90%+ of a calendar's events after the first run; fingerprinting now only happens in the approve/decline/ticktick-push paths elsewhere in the pipeline.

Feed and Instagram modes share a third content path with no JSON-LD/text-extraction step at all: captured response bodies are parsed as JSON and walked by `instagram.normalize_posts`, emitting one message per post with `id: "ig:<media_id>"`. `feed._extract_posts_from_api` is a thin wrapper over it since 2026-08-06 (it owned the walker before that). Three things changed in the move, all of which affect `feed` sources too:

1. **Lists are walked.** The old walker recursed through dict values only, so posts nested inside an array were invisible — silently, since the result was an empty list rather than an error.
2. **`content` leads with the post permalink** (`https://www.instagram.com/p/<code>/`), then location, then the caption. `analysis.build_prompt` truncates content to 400 chars, so a link appended after a long caption is a link the LLM never sees — and an Instagram event nobody can click is an event nobody can check.
3. **Multi-chunk bodies parse.** Deferred GraphQL arrives as newline-delimited JSON objects; `json.loads` on the whole body raised `JSONDecodeError`, which the caller caught and dropped — so the responses carrying the deferred timeline were exactly the ones discarded. `loads_multi` falls back to line-by-line.

Injected ads (`ad_id`/`is_ad`/`injected`) are dropped, and a `seen` set spans every response in a run because a feed re-serves the same media in a prefetch and again in the timeline query.

## Failure modes (by module)

| Module | Failure mode | Handling |
|---|---|---|
| `web_fetch.py` | Cloudflare "just a moment" on a standard-group source | Page closed, source queued to `cf_retries`, retried once serially under `native_chrome=True` |
| `web_fetch.py` | Exception anywhere inside one source's processing | Caught per-source in `_process_group`'s `try/except`; logged, OTel span records the exception, loop continues to the next source in that group — one bad source never aborts the whole run |
| `web_modes/calendar.py` | Cloudflare still blocking after `_wait_cloudflare` | Page closed, `[]` returned (no retry queued from inside a mode handler — retry only happens for the *initial* page-open Cloudflare check in `fetch_web_sources` itself) |
| `web_modes/feed.py` | `page` param passed in but feed needs interception from first load | Pre-opened page is closed and a fresh one opened via `open_with_intercept` — documented in the module docstring as "interface consistency" overhead |
| `web_modes/feed.py` | Captured response body isn't valid JSON | Tried as one object, then line-by-line (`loads_multi`); a body that parses as neither yields no posts and is skipped |
| `web_modes/instagram.py` | Login wall on any account | `InstagramLoginRequired` raised by `harvest_account`, caught by the mode, **whole source stops** — remaining accounts are not visited, and the log names the fix (`web login`) |
| `web_modes/instagram.py` | One account fails (private, renamed, timeout) | Caught per-account, logged, the walk continues — a renamed venue must not cost the other sixteen |
| `sources/instagram.py` | Replay returns non-200, non-JSON, or no cursor | Cursor is cleared *before* the request, so the loop ends rather than re-asking; the recorded first page is still returned. `paginate` returns the count of pages that actually landed |
| `sources/instagram.py` | No GraphQL recorded at all for an account | Warns that the profile may be private, renamed, or served from cache — distinguishes "asked and got nothing" from "never asked" |
| `web_modes/search.py` | Login wall detected (`uc.has_login_wall`) | Logged warning, page closed, `[]` returned immediately — no partial scrape attempted |
| `web_modes/ics.py` | HTTP fetch fails (non-2xx, network error) or `Calendar.from_ical` raises | Logged, `[]` returned; **`anti-bot dependent`: N/A** — plain HTTP, no browser, so this mode is immune to Cloudflare/bot-detection failures that affect every other mode |
| `web_modes/chat.py` | Login wall, or response `< 50` chars | `[]` returned with a warning; a *short but real* answer under 50 chars (e.g. "no events found") is indistinguishable from a failed/empty response — `fragile` |
| `web_modes/steps.py` | `mode: "steps"` with no `steps` array | Warning, `[]` returned — never falls back to another mode, since guessing at a route is worse than declining to run one |
| `web_modes/steps.py` | A step fails mid-route | Walk stops, partial links are still fetched and returned, and the failure is logged *with* a page diagnosis. `expect_min_events` (if set) warns separately when the yield drops |
| `sources/web_explore.py` | Session cap reached | `RuntimeError` naming the open sessions — raised *before* a browser is launched |
| `uc_browser` (`browser.py`) `_start_native_chrome` | `chrome.exe` not found, or CDP port never comes up after 15×1s retries | Falls back to Playwright `channel="chrome"` launch (if binary missing) or gives up and leaves `_context = None` (if CDP connect fails) — a `None` context will raise on first use downstream, `unverified` whether callers handle this gracefully |
| `web_utils.py` `_fetch_event_detail(s)` | Any exception during a detail-page fetch (timeout, navigation error, etc.) | Caught broadly, page closed if possible, `None` returned for that link — link is silently dropped, not retried |
| `web_utils.py` `_load_web_sources` | Missing/corrupt `data/web_sources.json` | **BUG** — see schema section above; returns `{}` instead of the documented `_DEFAULT_SOURCES` fallback |

## Verification

```bash
pixi run event-harvester web test https://lu.ma/discover
```

This drives the same auto-detect + dispatch path as a live harvest (minus cooldown writes) for one source and prints which mode was chosen and how many messages it produced — proves `_detect_mode`, the chosen `_do_*` handler, and `_extract_event_links`/structured-data extraction are wired together correctly without waiting for a full multi-source parallel run. For the ICS-specific short-circuit (no browser at all), run it against an `.ics`/`webcal://` source and confirm no Chromium window opens:

```bash
pixi run event-harvester web test webcal://cdn.dnalounge.com/calendar/dnalounge.ics
```

For the Instagram source, the two halves verify separately — deliberately, since only one of them needs a session:

```powershell
# Discovery: browser-free, no session, safe to run any time.
pixi run event-harvester web instagram --category venues

# Harvest: needs a logged-in session, opens one page per account.
pixi run event-harvester web login
pixi run event-harvester --only web --web-source instagram --no-cooldown
```

`unverified`: neither command was executed as part of writing this document (read-only research pass); behavior above is derived from static reading of `web_fetch.py`, `web_utils.py`, `web_modes/*.py`, `browser.py`, and the test suite (`tests/test_web_fetch_helpers.py`, `tests/test_web_fetch_parallel.py`, `tests/test_web_primitives.py`, `tests/test_web_ics_diag.py`), not from running the harvester.
