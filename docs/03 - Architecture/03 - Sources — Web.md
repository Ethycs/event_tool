# Sources — Web

**Status:** CURRENT · **Date:** 2026-11-23 (browser access consolidated in `uc_browser.WebBrowser`; before that 2026-08-06, `instagram.py` + `mode: "instagram"`) · **Applies to:** `src/event_harvester/sources/web_fetch.py`, `uc_browser.py`, `web_session.py`, `web_utils.py`, `feed_probe.py`, `instagram.py`, `web_steps.py`, `web_explore.py`, `src/event_harvester/sources/web_modes/{__init__,calendar,feed,search,raw,ics,chat,steps,instagram}.py`, `src/event_harvester/web_primitives.py`, `src/event_harvester/observability/hints.py`, `data/web_sources.json`, `ext/helpers/extract_event_links.js` · **Depends on:** `docs/04 - Reference/Universal Controller Guide.md`

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

**The modes are** `calendar`, `feed`, `search`, `raw`, `ics`, `chat`, `steps` (2026-07-30), `instagram` (2026-08-06), `rss` (2026-08-07), `luma` + `partiful` (2026-08-10), and `jsonld` (2026-11-24). Each has its own file, OTel span, and dispatch branch in `fetch_web_sources`; `web_modes/__init__.py` re-exports them and `__all__` is the count.

**Five of the twelve are browser-free** — `ics`, `rss`, `luma`, `partiful`, `jsonld` — and that group keeps growing for one reason: a platform that publishes structured data does not need to be scraped, and scraping it is *worse*, not merely more expensive. See below.

**`jsonld` reads what a listing page says about itself** (`web_modes/jsonld.py`, 2026-11-24): the schema.org `Event` items in the page's JSON-LD, one message each, dropping events that have ended. Its expectation is its own: no Event item at all is a deviation; all of them over is `empty`. *Measured 2026-11-24:* `midway`, which `calendar` mode in native Chrome reported `deviated` (2 dated links in the visible listing), publishes 47 `MusicEvent` items over plain HTTP, 19 of them current. `makerfaire`, repointed from the site's home page to `/bay-area/`, publishes one Event, the September 2026 faire, which has ended: 0 messages, `empty`, until the page lists the next one.

## Modules: `luma.py` and `partiful.py` — read what the platform already publishes

**Added 2026-08-10.** **Domain:** turn an event platform's own structured payload into one message per event. **Runs at:** harvest, dispatched before any page is opened. **Inputs:** a URL and (Partiful) a city. **Outputs:** message records.

**Why they exist.** `luma` was already configured — `mode: "calendar"` against `lu.ma/discover` — and measured 2026-08-10 it produced **1 message and 1 event**, from a page whose own directory reads "San Francisco 80 Events". This is precisely the failure `rss` was written for three days earlier, recurring on the largest event platform in the city: **one blob in, one event out, however many the page lists.**

Three findings came from probing rather than reasoning, and each changed the design:

| Probed | Found |
|---|---|
| `lu.ma` | 301s to `luma.com`. The old URL worked, at a redirect per fetch |
| `luma.com/discover` | server-renders **zero** events — its `__NEXT_DATA__` `data` block is empty. The city pages render 20. *The URL you are given is not the page with the events*, again |
| `api.lu.ma/discover/get-paginated-events` | a **keyless** JSON API behind the city page, returning `entries` / `has_more` / `next_cursor` — **45 SF events to one unauthenticated GET** |

So `luma` paginates the API rather than parsing a 400 KB page, and resolves the opaque `place_api_id` from the human-readable city URL by default, because a hardcoded id is a thing that silently rots.

`partiful` has no API — Partiful is invitation-shaped — but `/explore` inlines its trending events into the Next.js props, bucketed by city. **`city` is the load-bearing field:** the live sections are `NYC`, `LA`, `SF` **in that order**, so a mode that took the first would have filled an SF calendar with New York. That is the `ma.to` trap from `10 - Blueprints/01`, and it is why a missing city logs the available ones and returns nothing rather than falling back.

**Both emit local wall-clock, converted from UTC · fixed 2026-09-05.** Both APIs give `start_at`/`startDate` in **UTC** (`…Z`); an 8pm-Pacific event is stored `03:00Z` the *next* day. Both modes previously took the raw string's date/time slice and labelled it `UTC`, so the event surfaced at **3am on the wrong day** — the same defect `ics.py` had. `luma` now converts through the event's own reliable `timezone` field (falling back to Pacific); `partiful`'s per-event `timezone` is unreliable (it read `America/New_York` for SF and LA events alike), so `partiful` converts through the **configured city**'s zone (`_CITY_TZ`) instead. This is the third source of this bug class — any new structured source must convert before emitting a date/time.

**Measured 2026-08-10**, end-to-end through the real pipeline:

| Source | Before | After |
|---|---|---|
| `luma` | 1 message → **1 event** | 60 messages → **13 events** |
| `partiful` | *(did not exist)* | 4 messages → **4 events** |

`known rough edge`: **`luma.com/sf` is Bay Area-wide, not San Francisco.** The 13 events included Fremont, Stanford, Santa Clara, Mountain View and Saratoga. That is Luma's own definition of the place, not a bug here, but a city-proper calendar wants a distance filter that does not exist yet.

`known rough edge`: 60 messages yielding 13 events is a lower conversion than `rss` achieves (105 → 65) and has not been diagnosed. Candidates: virtual events the prompt is told to ignore, dedup collapsing recurring series, and per-batch extraction variance.

`unverified`: neither API is documented or contractual. Luma's `next_cursor` shape and Partiful's `trendingSections` path are both observed, not promised, and either can change without notice. Both modes degrade to an empty list with an error log rather than an exception.

*Two places once said "five" and were fixed 2026-07-29. The durable half of that fix was deleting the number from the `web_modes/__init__.py` docstring and pointing at `__all__` — this section is now on its fourth count, which is the argument.*

**`steps` is the mode the other browser modes are special cases of.** `calendar`, `feed` and `search` each encode one fixed walk in Python; `steps` reads its walk from the source config (`web_steps.replay`), so a site matching none of the three no longer needs a new module — it needs a route. It is never auto-detected, and `web_utils._detect_mode` is unchanged: a route only runs when someone configured one.

| Mode | Domain / what it does | When picked | Runs at | Key tunables |
|---|---|---|---|---|
| `calendar` | Load page, extract date↔link pairs (DOM-graph or Calcium HTML), fetch each detail page in parallel. Calcium grids are walked `calendar_months` months forward (default 6) | `mode: "calendar"`, or auto-detect finds ≥3 event links | Browser page (Chromium+UC or native Chrome) | `scroll_seconds`, `max_pages` (pagination), `calendar_months` (Calcium), `native_chrome`, `cooldown_hours` |
| `feed` | Re-open page with response interception, scroll while capturing XHR/GraphQL, parse posts from captured JSON and/or extracted links | `mode: "feed"` | Browser page, own re-opened tab (`open_with_intercept`) | `api_pattern` (regex), `scroll_seconds`, `api_only` (skip link-extraction path) |
| `instagram` | Open each configured account's profile, replay the recorded GraphQL query for further pages, normalise posts | `mode: "instagram"` — **never auto-detected** | Own page per account (recorder attached pre-navigation); dispatched **before** the shared page-open | `accounts`, `max_pages`, `max_posts`, `max_accounts`, `request_delay_s`, `account_gap_s`, `include_home_feed` |
| `search` | Detect search bar, fill `query`, scroll, extract event links from results | `mode: "search"`, or auto-detect finds a search box + `query` set | Browser page | `query`, `scroll_seconds`, `max_events` |
| `raw` | Load page, scroll, strip HTML to text — last-resort fallback | `mode: "raw"`, or auto-detect finds nothing else | Browser page | `scroll_seconds` |
| `ics` | Fetch an iCalendar feed over plain HTTP(S) via `requests`, parse `VEVENT`s with `icalendar`, one message per event. Timed events are converted from the feed's UTC into the venue's local zone (`tz`, default `America/Los_Angeles`) *before* the date/time is taken; only events within `[today, today+forward_days]` survive; noise summaries (`#REF!`, `Event Name`, `RESERVED`/`TBD`/…) and epoch/1970 rows are dropped; each message carries a `url` (the `VEVENT` `URL:` if present, else the source's `link`) and emits `date:`/`time:`/`location:` lines so the analysis row-bypass turns it straight into an event (no LLM) | `mode: "ics"`, or URL is `webcal://`/`webcals://`/`*.ics` (shortcut in `_detect_mode`) | **No browser** — plain HTTP in the worker thread | `timeout_s` (default 20), `forward_days` (default 90), `link`, `tz` |
| `chat` | Open an AI chat UI, send `query`, anchor-lock and poll for the response | `mode: "chat"` | Browser page | `query`, `timeout_s` (default 45) — requires prior `--web-login` |

**The listing's date has to travel with the detail page · fixed 2026-08-29.** `calendar` mode learns an exact date per link *from the listing* — the DOM-graph pairs each link with its date cell, and Calcium puts it in the URL as `Date=2026%2F9%2F5` (`_parse_calcium_links` skips any block without one, so it is total). The detail page it then fetches is prose written for someone who already knows what day they clicked, and frequently never restates the date. Both detail-fetch branches were building the message from that prose alone and dropping `date_hint` on the floor; `rerank_messages` then binned the message as **"no date/link"** before extraction saw it. Measured on erobay: `124 calcium links to fetch` → `124 messages` every run for weeks, **24 events on the page**, with the loss showing up only in the merged funnel line `Reranker filter: 2882 -> 731 (dropped 0 past, 2151 no date/link)` — nothing per source. `_hint_header` now prepends `date:` / `time:` / `location:` in both branches, matching the shape the inline branch always emitted. This is the second bug of this exact shape in `calendar` mode; the first was `_save_link_fingerprint` + `_filter_known_links` hiding 90%+ of a calendar, pinned by `test_filter_known_links_ignores_auto_fingerprints`. **A source whose fetch count is healthy and whose event count is not is the signature; check the reranker's drop line, not the fetch log.**

**A Calcium row is one occurrence · fixed 2026-11-02.** Checked against the live erobay calendar, a harvest kept **105 of ~198** upcoming events in the two grids it reads. Three causes, all fixed:

- **Dedup merged across dates** (100 rows) — not in this layer. `event_match` scored title + location as a match on any date, so each recurring event kept only its next instance and similarly-named classes swallowed each other. It now treats two dates as two occurrences (`00 - Theory/00 §4.2`).
- **`_calcium_messages` deduped the two grids by URL** (8 rows). One ticket URL serves every date of a series (Vox Body's monthly jams on buytickets.at), so the later dates were dropped. Meanwhile the same Calcium ID and day arrive from the two grids under *different* URLs (`DoneURL=` names the grid), so the URL key also failed to collapse the duplicates it was written for. Both the parser and `_calcium_messages` now key on `_calcium_row_key`: the ID (or, for an external link, the URL) plus the day. Message ids include the day too.
- **Titles with markup were unreadable** (4 rows). `Tangle! <br>Mission Control…` and `<strong>AURA</strong><br/>…` defeated `[^<]+`. The anchor regexes now take any text and tags short of a nested anchor, and `_anchor_text` flattens the result. The same fix corrected a row that had been reaching the feed titled `8` (a day-number link picked up in place of "THREE RING CIRCUS").

After the fix, the same rows give **199** events, with no title appearing twice on one day. **A recurring series that shows only its next date is this bug's signature**, because as each instance passes, the next one appears. Pinned by `TestCalciumRowsBecomeMessages`, `test_parse_calcium_links_reads_a_*` and `test_event_match.py`.

**Only two months were ever read · fixed 2026-11-05.** The fetch took the base (rolling) grid plus **one** next-month grid, so everything from the third month on was dropped. Measured against the live erobay calendar on 2026-11-05 with a real browser: current + next month = **198** unique upcoming, but **314+** span the next six months (29 still in month six, and more beyond) — over a third missed, and worse later in a month as the rolling grid collapses to a few remaining days. The parser and the rest of the pipeline were *not* the loss: the November grid alone holds ~147 events and parse → bypass → `dedup_events` keeps 114 → 111. The fetch now walks `calendar_months` grids (default `DEFAULT_CALENDAR_MONTHS` = 6) via `_calcium_forward_months`; the forward months ride the first month's Cloudflare clearance, so the extra grids are cheap. A Cloudflare block stops the walk. **A calendar healthy in the near term but empty months out is this bug's signature — count the months fetched, not the links on one grid.**

Auto-mode detection (`web_utils.py:_detect_mode`) order: ICS URL shortcut → `_extract_event_links` ≥3 → `uc.detect(page, "search")` + `src.get("query")` → `uc.get_feed_items` ≥3 items >30 chars → fallback `raw`. A saved UC "signature" (`mode:<name>`) from a prior successful run is checked *before* auto-detection runs (`web_fetch.py:220-236`).

**Undocumented-mode finding:** `ics` doesn't need a browser at all, but `fetch_web_sources`'s outer dispatch still enters the `_process_group`/`UCBrowser` context and iterates the source inside it — the `ics.py` module docstring itself flags this: "~1s of wasted setup per ICS source until we refactor to skip browser allocation for browser-free modes" (`ics.py:8`). `chat` sources are dispatched the same way — before the page-open branch — so they also skip the shared `_wait_cloudflare`/auto-detect path, but unlike `ics` they still open a page via `uc.open(...)` inside `_do_chat`. `instagram` (2026-08-06) joins that group for a third reason: it opens one page *per account* with a recorder attached before navigation, so a shared pre-opened page would be a page it immediately closes — which is exactly what `feed` mode still does and documents as interface-consistency overhead.

**The wasted browser is gone as of 2026-11-23, by routing.** `web_fetch.HTTP_MODES` names the modes read over plain HTTP (`ics`, `rss`, `luma`, `partiful`, and from 2026-11-24 `jsonld`). `fetch_web_sources` routes those sources to the `http` group (`_http_work`), which holds no browser, and their handlers no longer take a `uc` argument. The technique a mode uses is stated in that table; it is not inferred from whether a handler happened to touch the browser. *Measured 2026-11-23:* a run of `dnalounge`, `bottom-of-the-hill`, `luma` and `partiful` produced 179 messages with no `WebBrowser` constructed; `the-mix` (`raw`), `stud` (`calendar`) and `dnalounge` together produced 70 messages and one launch.

## Parallel-fetch model and why native-chrome stays serial

`fetch_web_sources` (`web_fetch.py`):

1. Load sources (explicit list or `data/web_sources.json` via `_load_web_sources`).
2. Drop sources on cooldown (`_is_on_cooldown`), unless `no_cooldown=True`.
3. **Route by access technique.** `http_srcs` are the sources whose mode is in `HTTP_MODES`. The rest are browser sources, split into `standard_srcs` (no `native_chrome` flag) and `native_srcs` (`native_chrome: true`).
4. `_chunk_sources(standard_srcs, parallel)` round-robins standard sources into up to `parallel` chunks (empty chunks dropped) — round-robin exists specifically so slow feed/calendar sources don't all land on one worker.
5. Each group runs in its own daemon thread (`_Group`): one `_browser_work` group per chunk, one more for the native group if non-empty, and one `_http_work` group if there are HTTP sources. Every source, of either kind, runs through `_run_source`: its span, its trace pair, its error boundary, its cooldown write, its recorded outcome.
6. Each `_browser_work` group holds exactly **one `WebBrowser`** (one Playwright instance, one Chromium/Chrome process, launched on entering the `with` block) and iterates its sources **serially**. Inside it, `chat` and `instagram` open their own pages; every other mode shares one page-open and bot-check wait. After each source, in a `finally`, `uc.close_pages()` closes any tab the mode left open.
7. Any standard-group source whose page is still a bot check after `_wait_cloudflare` (`web_utils.is_bot_check`) is queued into `cf_retries` instead of failing — closed without processing. In a native-Chrome group there is nowhere further to go: the source raises `SourceBlocked` and is recorded as `blocked`. The challenge page is never scraped.
8. After all parallel groups finish, `cf_retries` (deduplicated per group, then flattened) are retried **once**, serially, in a fresh `native_chrome=True` group.
9. `_report` logs the sources needing attention and appends the run to `data/.web_source_report.json`.

### Per-source outcomes and the deadline (2026-11-24)

Every source ends a run in exactly one state, recorded as a `SourceOutcome`:

| State | Meaning |
|---|---|
| `ok` | at least one message, and at least `expect_min_events` if the source sets it |
| `low` | fewer messages than `expect_min_events` (any mode; it was a `steps`-only tripwire before) |
| `empty` | ran cleanly and found nothing |
| `blocked` | a bot check stood where the page should be and did not clear; it needs a person at the browser |
| `deviated` | the page is not the page the source's handler was written for; `detail` says what was expected and what the page looks like instead |
| `failed` | raised |
| `hung` | ran past its deadline; its thread and browser were abandoned |
| `skipped` | queued behind a hung source in the same browser, so it never ran |

**The deadline.** Each group is a daemon thread (`_Group`), and `_await_groups` watches the source each one is on. A source that runs longer than `SOURCE_DEADLINE_S` (20 minutes; per source: `deadline_s`) is declared `hung`. The run stops waiting for that group, keeps every message the group had already produced, marks the sources behind it `skipped`, and returns. A thread cannot be killed, so the hung thread and its browser are left behind; if its call ever returns, the result is discarded and the thread closes its browser. The longest honest source measured took eleven minutes (about 350 detail pages).

**The report.** `data/.web_source_report.json` holds the last 30 runs: per source its state, message count, seconds and a detail string. One `WARNING` line per run lists the sources that are `blocked`, `deviated`, `hung`, `skipped`, `failed` or `low`; one `INFO` line lists the `empty` ones. The run total (`Web sources: N messages from M sources.`) is unchanged, and is not a health signal: see `docs/08 - Analysis/03 - Nightly Harvest Health 2026-11.md`.

**The access log** (`sources/web_access_log.py`, 2026-11-24). Some things about a source cannot be read off it and can only be observed: how long a passed bot check lasts is the case that prompted this (the clearance cookie says a year; the site decides). So every outcome is compared, as it happens and in the client, with the last outcome for that source **by the same access method**, and appended to `data/.web_access_log.jsonl`.

**Methods do not share verifications.** The method is the route `web_fetch` took: `http`, `standard` (Chromium with the UC extension), `native` (installed Chrome that the run launches, with the automation attached from the first request), `native:open-browser` (each source's page is first loaded in a Chrome already open on the native port, `uc_browser.NATIVE_CDP_PORT` = 9222, with nothing attached, and the run attaches only once that page is through), or `service:<window>` (a window of the UC service; off unless `EH_UC_SERVICE_URL` and `WEB_UC_NATIVE_WINDOW` are both set). State is kept per `(source, method)` pair, every line and every report entry carries `method`, and a standard-browser source handed to the native retry leaves two lines: `blocked` by `standard`, then whatever the native route found. `bot_check_wait_s` on a source sets how long a check is given to clear (default 15 s; `erobay` is set to 120).

*Measured 2026-11-24 on `erobay` (Cloudflare), owner at the KVM, all on the native profile:*

| Method | Runs | Result |
|---|---|---|
| plain Chrome, debugging port open, nothing attached; a person clicks | 02:15, 03:43 | a box was offered and the check cleared, both times |
| harvest attaches to that open browser afterwards (`native:open-browser`) | 02:16, 03:43 | no challenge; 273 messages, both times |
| Chrome launched with the automation attached (`native`) | 02:30, 03:18, 03:31, and two unattended | never cleared: 'Performing security verification' with a spinner for up to 120 s, no box offered, title unchanged (screenshot under `data/.web_hints/`) |
| same, one run | 02:33 | cleared 15 s into the wait. Unexplained |
| a later, separately launched Chrome on the same profile | 02:30, 15 minutes after the 02:15 clearance | challenged again; the clearance cookie was no longer in the profile |
| harvest attaches to the same open browser again, nobody at the KVM | 06:53, 3 h 10 min after the 03:43 clearance | challenged; did not clear in 120 s with the client attached: `blocked` |
| the URL opened in the open browser over HTTP (`/json/new`), nothing attached, nobody at the KVM, **X screen blanked** | 06:54 (120 s), 06:56 (43 s) | title stayed 'Just a moment...' throughout |
| the same, **screen no longer blanked**, nobody at the KVM, nobody clicked | 06:59 | through within 8 s |
| harvest attaches afterwards | 07:01, 07:05 | no challenge; 273 messages, both times |
| the same unattached load, nobody at the KVM, 10 h 45 min later | 17:49 (120 s), 17:51 (40 s) | not through. The page showed Cloudflare's 'Verify you are human' checkbox |
| UC service's native window (`service:native`): page loaded in a quiet reservation, the checkbox clicked once through the KVM | 18:20 | through 2 s after the click; harvest attached; 273 messages |

What the runs have in common: the check finishes when its page loads with **no automation attached** and is actually **being drawn**. Sometimes it then finishes by itself (06:59) and sometimes it puts up a checkbox that takes one click (17:49, 18:20); nothing measured says which. With a client attached from the start it cleared once in seven runs. A clearance lasted less than 3 h 10 min, so it does not carry from one night's harvest to the next and has to be obtained in the same run. `unverified`: that an erobay tab left open keeps a clearance alive (the owner's expectation; the tab he cleared at 03:43 stayed open all day in a window that was not repainting, and loads at 06:53 and 17:49 were challenged regardless).

**'Being drawn' is its own condition, and it failed three ways on 2026-11-24.** (1) The X screen was blanked until 06:57. (2) Chrome windows on this desktop (LXDE, Openbox, no compositor) stopped repainting: at 17:53 the windows of the native Chrome still showed their 07:00 contents until a real mouse click landed on one, and after that browser was stopped its windows stayed on screen until `xrefresh`. (3) The UC service's Chrome window was found iconified at 18:16 (`WM_STATE` Iconic, unmapped); what iconified it is not known, and nothing in the service or in event_tool does. *Measured on `ra-sf`, same route, same 21 detail links:* window iconified, no result in 1200 s (`hung`, 17:56–18:16); window mapped and on top, 20 messages in 81 s (18:21). One run each side. The NanoKVM's screenshots ran seconds behind the real screen all evening; a grab of the X display (`PIL.ImageGrab`) is the reference for what is on it.

**The minimized case is now refused before it costs anything** (`sources/native_window.py`, called first in `uc_browser.prepare_native`). event_tool finds the route's browser from outside it — debugging port from the route (the service's window info, or the `cdp_endpoint`), process from `/proc`, window state from the X server through `xprop` — and when every window that browser has is iconified it raises `NativeWindowNotDrawn`: the source is `failed` with that reason and no page is opened. It never maps or focuses the window; the desktop is the owner's. Whatever cannot be read (no `DISPLAY`, no `xprop`, browser not running yet) is `None` and refuses nothing. *Verified live 2026-11-24:* the service's window read `drawn`, `minimized` after it was iconified, `drawn` after it was restored. `not implemented`: detecting cases (1) and (2), a window that is mapped but not being repainted.

**The route in use: the UC service's native window** (`service:native`, cut over 2026-11-24 17:54–18:23 UTC). event_tool's `.env` names the service (`EH_UC_SERVICE_URL`) and the window (`WEB_UC_NATIVE_WINDOW=native`). The service launches and owns a plain Google Chrome on `data/.uc_chrome_profile` (no engine injected, no connection of its own to it; controller `5f55401`). `uc_browser.native_route()` is the one place that decides how native work gets its browser, for the harvest, its native retry, the explore tool and the CLI replay alike, in this order: the configured service window; else a Chrome answering on port 9222 (`cdp_endpoint`); else the controller launches Chrome. With the service window configured nothing else is tried.

A harvest source on this route has two steps (`web_fetch._open_browser_source`). First `uc_browser.prepare_native` loads the source's URL with nothing attached: `register_client(window, label, unattached=True)` (a quiet reservation, refused with `busy` while anything else is registered on or connected to the window), `start_window`, `new_page(url=)`, then `page_info` once a second, with the page and the reservation closed in a `finally`. The service reports the title; whether the page is through is event_tool's rule (`_wait_through`): the title has stopped being the address and is not a bot-check title for two reads. Only then does the harvest attach (`UCBrowser(service=, window=, label=)`), for that source alone, and detach. A page that is not through is not attached to, and `not_through` reports it by what it last showed: a bot-check title still up after the source's `bot_check_wait_s` is `blocked`; a page that had not rendered after `LOAD_WAIT_S` (30 s) is `deviated` (`not_loaded`). The two had one clock until 2026-11-24 18:39: `ra-sf` shows `ra.co` as its title while it loads, for 1–5 s on three runs and for more than its 15 s on a fourth (18:36), which was logged `blocked` by a check that was never on the page. A `busy` window is `failed`, not a reason to attach or launch. The explore tool and the CLI replay prepare their one address the same way (`native_browser`). A hung source calls `revoke(label)`; on the hung `ra-sf` run the service closed 5 pages for 1 client and the window came back with none.

*Verified live 2026-11-24, through the harvest service:* `erobay` ok, 273 messages (18:20, with the one click); `ra-sf` ok, 20 messages (18:22).

**A page kept open** (`keep_page_open` on a source; controller `d855385`; set on `erobay`, at the owner's direction: once its check is cleared an erobay page should stay open). The service's reaper closes every page that no client holds, every 5 s, keeping one blank tab, so a tab could not simply be left behind (measured before the change: one opened directly on the browser's own HTTP endpoint was gone within 5 s). With the option, `uc_browser._keep_page` gives the page that just got through the window's lifetime (`set_page_lifetime(window, target, client, "window")`) before the reservation is released; the service then holds it as `window`, counts it as nobody's client, and does not attach to it. The older kept page of the same site is closed after the new one is kept, so there is one per site and never none. event_tool stores nothing about kept pages: it reads them back from the window by holder and URL. A page that did not get through is never kept. While a client is attached to the window, Playwright is attached to the kept page as well; it is unattached only between clients. It is not restored after the browser or the service restarts (the owner's choice). *Verified live 2026-11-24:* 18:55, after the service restart the check was back; one click; the page was held by the window with no client 3 s later and still at 34 s; 273 messages. 18:57, second run: through in 1 s with no check, the new page kept, the first closed, one erobay tab in the window 12 s on; 273 messages.

*What the kept page did, measured 2026-11-25 to 26.* It stayed: the tab kept at 18:57 was still in the window, held by `window`, 29 h 5 min later (read 00:01 UTC on 11-26, just before a service restart closed it), through every client in between and the whole 11-25 harvest. It did not keep the clearance: at 06:11:38 UTC on 11-25, with that tab open, erobay's fresh quiet page showed the check for its full 120 s and the source was logged `blocked`. From the access log the clearance lasted somewhere between 37 min and 11 h 51 min with the tab kept (last `ok` 18:57, next attempt 06:11); the day before, without one, between 4 min and 10 h 50 min. One reading each, and the two ranges overlap, so nothing here shows the tab lengthening a clearance. erobay is not harvested unattended: a night on which the check is up needs the box clicked during the 120 s wait.

**Where the service listens** (controller `358c8ba`, since 2026-11-26 00:13 UTC). The UC service listens on `127.0.0.1:4010` and on this host's ZeroTier address `10.158.218.150:4010`, with no authentication, by the owner's decision that ZeroTier membership is the boundary; the Grok proxy likewise on port 4000. Neither listens on the LAN address (measured: refused). event_tool still calls loopback (`EH_UC_SERVICE_URL=http://127.0.0.1:4010`) and nothing in it changed. Two consequences for the harvest. Another host on the ZeroTier network can now reach this window's routes (pages, cookies, fetch), so a quiet reservation can be refused `busy` because of a caller that is not event_tool; that is reported as `failed`, as before. And the service requires every configured address when it starts: if the ZeroTier address is not up then, it does not start at all, loopback included, and the unit retries every 3 s (`Restart=on-failure`). So the native route depends on ZeroTier being up when the service starts, though not afterwards. `unverified`: a start with ZeroTier down has not been observed on this host.

**The fallback: a Chrome left open on 9222.** `pass_check_unattached` does the unattached step through the browser's own HTTP endpoints (`/json/new`, `/json/list`, `/json/close`) and the harvest attaches with `UCBrowser(cdp_endpoint=...)` (controller `04b200f`). This was the route from 07:05 to 17:54 on 2026-11-24, on the user unit `uc-native-window` (`~/.config/systemd/user/uc-native-window.service`), now stopped and disabled; the unit file is kept. To go back: have the service stop its native window, unset `WEB_UC_NATIVE_WINDOW`, start the unit. Never both: they are two owners of one profile.

History of the controller's part: at `99390a5` it attached to whatever answered on 9222; at `f3eee6a` it stopped and refused a held profile, so every native source failed (06:40, one run); `04b200f` added the explicit `cdp_endpoint`; `5f55401` the quiet native window. The desktop's screen locker and blanking were turned off the same morning (`~/.config/autostart/light-locker.desktop` hidden; `xset s off`, `s noblank`, `-dpms` in `~/.config/lxsession/LXDE/autostart`).

| Line | Written when | Carries |
|---|---|---|
| `change` | the state differs from last time | `was`, `was_since`, and how long the old state held as two bounds: `held_min_s` (it was last *seen* in the old state that long after it began) and `held_max_s` (it had changed by now). The gap between them is how often we looked |
| `fail` | the state is a failing one and has not changed | `since`, `detail`. One line per access, so a source that fails nightly leaves a line nightly |

`data/.web_access_state.json` holds each pair's current state, when it began and when it was last seen. A `skipped` source is logged as a `fail` without moving its state: nothing was learned about the site. A change also logs one `WARNING` (`erobay via native: access state changed ok → blocked (was ok for between 4h50m and 28h50m)`). Every line is written by `fetch_web_sources` as a source finishes; nothing is entered by hand. With one harvest a night the bounds are a day apart; nothing probes between runs. `not implemented`: accesses outside `fetch_web_sources` (explore sessions, `web test`, `web replay`) are not logged.

**Deviation is detected by the handler.** Every source here is a program for a known page, so the handler is the thing that knows what the page should be, and it checks at the point where its own expectation fails. It raises `SourceDeviated` (`web_utils.py`) and emits nothing; it does not fall back to scraping whatever is there. `page_deviation(page, expected)` fills in what the page looks like instead from the rule layer of `observability/hints.py` (one `page.evaluate`, no vision call), which already named login walls, bot checks, cookie banners, error pages and empty results but was reached only by step routes and explore sessions. A deviation whose diagnosis is a bot check is a `SourceBlocked`, reported as `blocked`.

| Handler | What it was written for | When that fails |
|---|---|---|
| `calendar` | a Calcium grid, or a listing with at least 3 dated links | deviation, with the count found. *Before 2026-11-24:* one message of the page's raw text |
| `instagram` | signed-in profile pages that issue GraphQL as they load | deviation of kind `login_wall` if a login wall was met, else a deviation saying no account returned a post. *Before:* `[]`, indistinguishable from a quiet night |
| `search` | a page that answers a query with event links | deviation when the page showed no links at all, unless the page itself says the search matched nothing (`empty`). Links already held are not a deviation |
| `steps` | its recorded route | deviation when the route broke and left nothing, carrying the route's own diagnosis. A partial walk that produced messages still returns them |
| `chat` | a reply of at least 50 characters | deviation |
| `raw`, `feed` | any text; any captured responses | no expectation, so nothing to deviate from. `not implemented`: a per-source statement of what a `raw` page must contain. `badlands` (`raw`) returned a parked page ('The domain has expired', ParkLogic) every night and was reported `ok`; the browser stays on the source's own host, so comparing hosts would not have caught it (measured 2026-11-24). The source was removed from `data/web_sources.json` that day |

Nothing here uses Universal Controller's `has_login_wall`: it tests a `blocking` key the engine never emits, so it has always returned false (`ext/universal_controller`, Engine Gaps roadmap). The two calls to it, in `search` and `chat`, were removed.

*Measured 2026-11-24 against the live sites, in a fresh native Chrome:* `erobay` → `blocked`; `midway` → `deviated`, 2 dated links found, page looks like `cookie_banner`; `makerfaire` → `deviated`, 0 dated links, page looks like ordinary `content` (its URL is the site's home page, which lists no events); `stud` → `ok`, 44 messages. Those three were the only calendar sources to reach the raw-text fallback on any night from 2026-11-17 to 2026-11-23.

`not implemented`: nothing tells a person that a source is `blocked`. A state that needs a human should park the tab in a long-lived, visible browser and notify; today the browser closes with its group. That is the Universal Controller service's job (`ext/universal_controller`, design in its own `docs/`).

Native Chrome (`BrowserMode.NATIVE_CDP` in `ext/universal_controller/uc_browser/browser.py:_start_native_chrome`) launches the user's actual `chrome.exe` via `subprocess.Popen` with `--remote-debugging-port` and connects Playwright over CDP — it is **not** a second Playwright-launched browser process pool. Because there is one real Chrome window/profile involved (`data/.uc_chrome_profile`), and because native Chrome exists specifically to look like a real user to Cloudflare, the code deliberately keeps this group to a single serial worker — running several native-Chrome instances concurrently from one profile would both contend for the profile lock and defeat the "looks like one real user" premise that lets it pass Cloudflare's challenge in the first place. `parallel` only ever affects the standard-source chunking; the native bucket size is hardcoded to 1 group (`fetch_web_sources` never multiplies native workers by `parallel`).

**The native group runs after the standard browsers have closed · changed 2026-11-26.** Until then every group started together. Both kinds of browser are windows on the one screen, and the standard one (Playwright's Chromium, 1288×851) opens over most of the native one (945×1034). Measured on the harvest of 2026-11-26: both browsers were launched at 06:54:47; `erobay`, the first native source, showed its check as a checkbox; at 06:55:14 the native window, raised by hand, was on top with the box visible; at 06:56:26 the standard browser's window was over it again, a second raise at 06:56:35 did not bring it back, and the source was logged `blocked` at 06:56:48. The standard browser keeps working through its sources while the native wait runs, and a bot check is not passed on a page that is not being drawn (measured 2026-11-24, above). So `fetch_web_sources` now waits for the plain-HTTP and standard groups, then starts the native group, then the native retry; `tests/test_web_fetch_outcomes.py` holds the order. The cost is the native group's own time added to the run instead of overlapping it (about 3.5 min on 2026-11-25: 120 s of `erobay`'s wait and 81 s of `ra-sf`). A standard group that is given up on as hung may leave its window open; that case is not handled. `unverified`: that `erobay`'s check then passes without a click. The overlap explains a click that could not land, not a check that asked for one: on 2026-11-24 the check asked for a click at 18:20 and at 18:55 in runs of this source alone, with no standard browser on screen.

*After that harvest, 2026-11-26:* with the change loaded (`1fe46db`, harvest service restarted 07:40:23) `erobay` was run alone at 07:41:45. Its check was up as a checkbox within 2 s; nothing lay over the window; one click through the KVM at 07:41:53 and the page was through at 07:41:55; 269 messages, 105 events, the page kept. The events cache merges a run into what it already holds (`mcp_server`: new events plus the still-future existing ones, de-duplicated), so a one-source run after a harvest repairs that source without repeating the harvest; the site was then rebuilt from the cache (`POST /refresh?harvest=0`, 761 events). This says nothing yet about the changed order in a full harvest: the first such run is the next scheduled one.

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
| `forward_days` / `link` / `tz` | int (default 90) / str / str (default `America/Los_Angeles`) | `ics` only (`web_modes/ics.py`) | `forward_days`: only emit events from today out to this horizon (also discards a spreadsheet feed's epoch/1970 junk rows). `link`: the source's public page, used as each event's link when the `VEVENT` has no `URL:` so the event carries a real link and skips the LLM via the row-bypass. `tz`: zone to convert timed (UTC) events into before taking the date/time. |
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

`uc_browser.py` in `src/event_harvester/sources/` is **the one place event_tool obtains a browser and its pages** (since 2026-11-23; before that it was a 17-line re-export shim). Its `WebBrowser` class wraps `UCBrowser` from the installed `uc_browser` package (`ext/universal_controller/`, editable pip install), where all real browser-launch logic still lives (`uc_browser/browser.py`).

| `WebBrowser` owns | How |
|---|---|
| Starting | `with WebBrowser(...)` constructs and enters `UCBrowser(**kwargs)`; `start()` does the same for a holder that outlives one block (an explore session). Using it before that raises `RuntimeError`: it never launches implicitly. Whether a source needs a browser at all is decided by the routing in `web_fetch`, not here |
| Pages | `open`, `open_with_intercept` and `new_page` are the only ways a page comes to exist; each is remembered. `close_pages()` closes those still open; `close()` does that and then closes the browser |
| The controller's API | every other public attribute (`detect_all`, `dismiss_cookies`, `search`, `load_signatures`, `timeout_ms`, …) is forwarded unchanged |
| The boundary | names starting with `_` are not forwarded, so `uc._context` raises `AttributeError`. `new_page()` calls the controller's public `UCBrowser.new_page()` (since the pin moved to `f3eee6a` on 2026-11-24); nothing in event_tool reads the controller's private members |

Constructed in five places and nowhere else: `web_fetch._browser_work`, `web_explore.open_session`, the CLI's `_web_replay` and `_run_url_diagnostics`, and `scripts/export_grok_history.py`. `web_login()` in `web_session.py` is the one remaining direct Playwright launch (`launch_persistent_context` on `data/.chrome_profile`); it is a sign-in flow, not a harvest path, and was left alone.

**One bot-check test.** `web_utils.is_bot_check(html)` is true when the HTML contains "just a moment" or "security verification". Before 2026-11-23 the string test was written out twelve times in six files and the copies disagreed: only `_wait_cloudflare`'s entry test knew the second phrase. `_poll_bot_check` is the one polling loop, used by `_wait_cloudflare` (15 s, 3 s settle) and by the detail-page fetch (10 s, 1 s settle). *Behaviour change:* a page showing only "security verification" is now waited on, reported as blocked, and queued for the native-Chrome retry; before, it was scraped as if it were the source. `unverified` against a live page of that kind. Two classifiers remain separate on purpose: `observability/hints.py:_CHALLENGE_RE` (page diagnosis, also matches captchas) and `onboard.py`'s HTTP-header probe (names the vendor that blocked).

`web_session.py` re-exports `get_chrome_cookies`/`cookies_to_playwright` from `uc_browser.chrome_cookies` for legacy callers; the module's own remaining job is `extension_args()`, `wait_for_uc`, `apply_uc_patterns`, and the `web_login()` interactive flow.

## Per-source cooldown

`web_utils.py:_load_cooldowns`/`_save_cooldown`/`_is_on_cooldown` read/write `data/.web_cooldowns.json` — a flat `{url: iso_timestamp}` map of last-successful-fetch times. `fetch_web_sources` filters sources against this **before** dispatch (`cd_hours = src.get("cooldown_hours", -1)`; skipped only if `cd_hours >= 0` and within the window — so a source with no `cooldown_hours` key is *never* cooldown-filtered, only ones that explicitly opt in with a non-negative value). A cooldown timestamp is only saved when a source returns messages (`if msgs: _save_cooldown(src["url"])` in `_run_source`), for every mode including `chat` and `ics`; a `blocked`, `failed` or `hung` source saves none. `no_cooldown=True` (CLI/config flag) bypasses the whole filter regardless of per-source settings.

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

Two extraction paths feed `content`, tried in this order wherever a detail page is fetched (`_detail_text`, called by `_fetch_event_details_parallel` in `web_utils.py`, and inline in `_do_ics_inner`):

1. **Structured data first** — `_extract_structured_data` looks for `schema.org` `Event` JSON-LD (`<script type="application/ld+json">`, including `@graph`-wrapped arrays and list-valued `@type`), pulling `title`/`date`/`time`/`location`/`link`/`details` and formatting them as newline-joined `key: value` lines. No LLM involved.
2. **Fallback to raw text** — `_extract_text` strips `<script>`/`<style>` blocks then all remaining tags, collapses whitespace. Used whenever JSON-LD is absent, or for whole-page raw/calendar-fallback content. `_decimate_text` (used elsewhere in the pipeline, not inline in these fetchers) centers a window around the densest date/time-anchor cluster when text exceeds `max_chars`, so long pages don't drown the eventual LLM extraction step in nav/footer junk.

Dead/removed event pages are filtered via `_is_dead_page` (regex over the first 3000 chars — "deleted", "cancelled", "no longer available", "404", "event not found") before either extraction path runs; a hit returns `None` and the link is dropped rather than turned into a message.

Before detail pages are even fetched, `_filter_known_links` drops links whose fingerprint (`event_harvester.pipeline.event_match`) is already `approved` or `declined` — matched by exact URL first, then a fuzzy title+date score ≥2. A URL the listing shows on several dates is a series page and skips the URL match (added 2026-11-02): declining one Vox Body jam must not hide next month's. Fingerprints with no `status` (or `status: "auto"`) are deliberately **not** treated as "known" — `_save_link_fingerprint` is now a documented no-op (`web_utils.py:115-127`) specifically because eagerly fingerprinting every fetched page used to hide 90%+ of a calendar's events after the first run; fingerprinting now only happens in the approve/decline/ticktick-push paths elsewhere in the pipeline.

Feed and Instagram modes share a third content path with no JSON-LD/text-extraction step at all: captured response bodies are parsed as JSON and walked by `instagram.normalize_posts`, emitting one message per post with `id: "ig:<media_id>"`. `feed._extract_posts_from_api` is a thin wrapper over it since 2026-08-06 (it owned the walker before that). Three things changed in the move, all of which affect `feed` sources too:

1. **Lists are walked.** The old walker recursed through dict values only, so posts nested inside an array were invisible — silently, since the result was an empty list rather than an error.
2. **`content` leads with the post permalink** (`https://www.instagram.com/p/<code>/`), then location, then the caption. `analysis.build_prompt` truncates content to 400 chars, so a link appended after a long caption is a link the LLM never sees — and an Instagram event nobody can click is an event nobody can check.
3. **Multi-chunk bodies parse.** Deferred GraphQL arrives as newline-delimited JSON objects; `json.loads` on the whole body raised `JSONDecodeError`, which the caller caught and dropped — so the responses carrying the deferred timeline were exactly the ones discarded. `loads_multi` falls back to line-by-line.

Injected ads (`ad_id`/`is_ad`/`injected`) are dropped, and a `seen` set spans every response in a run because a feed re-serves the same media in a prefetch and again in the timeline query.

## Failure modes (by module)

| Module | Failure mode | Handling |
|---|---|---|
| `web_fetch.py` | Bot check (`is_bot_check`) still showing on a standard-group source after `_wait_cloudflare` | Page closed, source queued to `cf_retries`, retried once serially under `native_chrome=True` |
| `web_fetch.py` | A mode returns or raises with a tab still open | `uc.close_pages()` in the per-source `finally` closes it and logs the count at debug level. *Measured 2026-11-23:* a mode forced to raise after the shared page-open left one tab, and the sweep closed one |
| `web_fetch.py` | A source runs past its deadline (a browser call that never returns) | `_await_groups` records it `hung`, records the sources behind it `skipped`, keeps the group's finished messages, and stops waiting. *Before 2026-11-24:* the job waited to its 90-minute limit and discarded the whole run, twice in the week of 2026-11-17 |
| `web_fetch.py` | Bot check still showing in native Chrome | `SourceBlocked`: no message, no cooldown, state `blocked`. *Before 2026-11-24:* the challenge page was scraped and emitted as one message. *Measured 2026-11-24 against the live site:* `erobay` reported `blocked` after 17 s and emitted nothing |
| `web_utils.py` `_fetch_event_details_parallel` | A detail page still shows a bot check after its 10 s wait | `None` for that link, and one warning with the count; the challenge text is not emitted as the event's detail |
| `web_fetch.py` | Exception anywhere inside one source's processing | Caught per-source in `_run_source`; logged, OTel span records the exception, state `failed`, and the group continues to its next source — one bad source never aborts the whole run |
| `web_modes/calendar.py` | Cloudflare still blocking after `_wait_cloudflare` | Page closed, `[]` returned (no retry queued from inside a mode handler — retry only happens for the *initial* page-open Cloudflare check in `fetch_web_sources` itself) |
| `web_modes/calendar.py` | Fewer than 3 dated links and not a Calcium grid | `SourceDeviated` (or `SourceBlocked` if the page diagnoses as a bot check), no message. *Before 2026-11-24:* the page's raw text as one message |
| `web_modes/feed.py` | `page` param passed in but feed needs interception from first load | Pre-opened page is closed and a fresh one opened via `open_with_intercept` — documented in the module docstring as "interface consistency" overhead |
| `web_modes/feed.py` | Captured response body isn't valid JSON | Tried as one object, then line-by-line (`loads_multi`); a body that parses as neither yields no posts and is skipped |
| `web_modes/instagram.py` | Login wall on any account | `InstagramLoginRequired` raised by `harvest_account`, caught by the mode, **whole source stops** — remaining accounts are not visited, and the log names the fix (`web login`). If nothing was harvested, the mode then raises `SourceDeviated` of kind `login_wall` |
| `web_modes/instagram.py` | Every account walked returned nothing | `SourceDeviated`: signed out without a login redirect, or the pages changed. *Measured:* this was the state on 2026-11-20, 21 and 23, logged as sixteen per-account warnings |
| `web_modes/instagram.py` | One account fails (private, renamed, timeout) | Caught per-account, logged, the walk continues — a renamed venue must not cost the other sixteen |
| `sources/instagram.py` | Replay returns non-200, non-JSON, or no cursor | Cursor is cleared *before* the request, so the loop ends rather than re-asking; the recorded first page is still returned. `paginate` returns the count of pages that actually landed |
| `sources/instagram.py` | No GraphQL recorded at all for an account | Warns that the profile may be private, renamed, or served from cache — distinguishes "asked and got nothing" from "never asked" |
| `web_modes/search.py` | The page showed no event links at all | `SourceDeviated` with the page's diagnosis, or `[]` when the page itself reports no results |
| `web_modes/ics.py` | HTTP fetch fails (non-2xx, network error) or `Calendar.from_ical` raises | Logged, `[]` returned; **`anti-bot dependent`: N/A** — plain HTTP, no browser, so this mode is immune to Cloudflare/bot-detection failures that affect every other mode |
| `web_modes/chat.py` | Response `< 50` chars | `SourceDeviated` with the page's diagnosis; a *short but real* answer under 50 chars (e.g. "no events found") is still indistinguishable from a failed one — `fragile` |
| `web_modes/steps.py` | `mode: "steps"` with no `steps` array | Warning, `[]` returned — never falls back to another mode, since guessing at a route is worse than declining to run one |
| `web_modes/steps.py` | A step fails mid-route | Walk stops, partial links are still fetched and returned, and the failure is logged *with* a page diagnosis. `expect_min_events` (if set) warns separately when the yield drops |
| `sources/web_explore.py` | Session cap reached | `RuntimeError` naming the open sessions — raised *before* a browser is launched |
| `uc_browser` (`browser.py`) `_start_native_chrome` | `chrome.exe` not found, or CDP port never comes up after 15×1s retries | Falls back to Playwright `channel="chrome"` launch (if binary missing) or gives up and leaves `_context = None` (if CDP connect fails) — a `None` context will raise on first use downstream, `unverified` whether callers handle this gracefully |
| `web_utils.py` `_fetch_event_details_parallel` | Any exception during a detail-page fetch (timeout, navigation error, etc.) | Caught broadly, tab closed in a `finally`, `None` returned for that link — link is silently dropped, not retried. Before 2026-11-23 a tab whose `goto` raised was never closed. `_fetch_event_detail` (the single-page variant, no callers) was deleted; it alone called `_is_dead_page`, so **dead-page detection does not run on any live path** — `_is_dead_page` survives with its tests and no caller |
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

`unverified`: neither command was executed as part of writing this document (read-only research pass); behavior above is derived from static reading of `web_fetch.py`, `web_utils.py`, `web_modes/*.py`, `browser.py`, and the test suite (`tests/test_web_fetch_helpers.py`, `tests/test_web_fetch_parallel.py`, `tests/test_web_primitives.py`, `tests/test_web_ics_diag.py`), not from running the harvester. The exceptions are the passages dated 2026-11-23, which were run.

For the browser seam:

```bash
pixi run --frozen -e dev pytest -q tests/test_uc_browser.py
grep -rn -E 'UCBrowser\(|\._context\b|_stealth_plugin' --include='*.py' src scripts
```

The tests should pass (7 on 2026-11-23). The grep should print lines in `src/event_harvester/sources/uc_browser.py` only; a hit anywhere else is a caller reaching past `WebBrowser`, and it will break when Universal Controller runs as a service.
