# Output Sinks

**Status:** CURRENT · **Date:** 2026-08-12 (month grid + shared nav added) · **Applies to:** `src/event_harvester/outputs/__init__.py`, `src/event_harvester/outputs/html_report.py`, `src/event_harvester/outputs/calendar_html.py`, `src/event_harvester/outputs/ics.py`, `src/event_harvester/outputs/nav.py`, `src/event_harvester/outputs/deeplinks.py`, `src/event_harvester/outputs/report.py`, `src/event_harvester/outputs/ticktick.py`, `src/event_harvester/outputs/obsidian.py`, `src/event_harvester/ui/display.py`

Four sinks turn harvested/validated events into something a human (or TickTick) consumes: a throwaway Markdown report, an Obsidian vault writer, a TickTick task sync, and ANSI terminal printers. The HTML pair — the list (§0) and the month grid (§0a) — is the default and is what the site serves. Only `ticktick.py` mutates external state — `report.py`, `obsidian.py`, and `display.py` are local/idempotent and safe to re-run. `src/event_harvester/outputs/__init__.py` is a one-line docstring module with no exports to import from (`"""Sinks that write extracted events outward: TickTick, Markdown reports, Obsidian vault."""`) — callers import directly from the submodules.

**Hard constraints, upfront:**
1. TickTick OAuth's authorization-code exchange is interactive (opens a browser, blocks on `input()` for the redirect URL) and has **no refresh-token path** — see below. It cannot run unattended the first time, or ever again once the token silently expires mid-run.
2. TickTick task creation and task updates are real, external, user-visible writes (`tt.task.create`, `tt.task.update`) — not easily undone. Only `create_ticktick_tasks(..., dry_run=True)` is safe to re-run freely.
3. `obsidian.py` overwrites the whole day's file on every write — it does not merge across runs within the same day (see Failure modes).

---

## 0. `outputs/html_report.py` — self-contained HTML report ⭐ **default since 2026-07-30**

**Domain:** One `.html` file with no external resources — inline CSS and JS, no CDN, no fonts, no network. That combination is the design, not a nicety:

- **Forwardable.** A single file can be emailed, AirDropped, dropped in a shared folder, or hosted anywhere. The Markdown report is forwardable too but renders as a wall of text on a phone; the Flet UI is interactive but is an *application*, not something you can send someone.
- **Mobile-first.** The phone is where "what's on this weekend" is actually read, and where the two actions that matter — add to calendar, open the listing — are one tap. 40px tap targets, sticky search, `viewport-fit=cover`, light/dark via `prefers-color-scheme`.
- **Offline.** A page that needs the network to render is a bookmark, not a document.

**Inputs:** `generate_html_report(validated_events, raw_events=None, source_counts=None, total_messages=0, output_path="events_report.html", title="Events", today=None)`. Mirrors `generate_report`'s signature so it drops into the same call sites. `links` is deliberately absent — "Top Links" is a debugging aid and this file is meant to be sent to people.

**Per event:** title, date/time, location, source, expandable details, plus three actions — *Add to calendar* (a `data:text/calendar` iCalendar URI built client-side, so it works with no server), *Open* (the source listing), and *TickTick* (the existing `ticktick://` deep link).

**Interaction:** client-side search across title/location/details/source, per-platform filter chips (see "The filter bar" below), and date bucketing — `Today`, `Tomorrow`, `This week`, `This month`, `Later`, `Undated`, `Past`. `Undated` and `Past` sort **last** so a stale event never heads the page.

### Properties worth not breaking

**Scraped text is never interpolated into markup.** Event titles and details come from arbitrary websites. The payload is embedded as JSON in a `<script type="application/json">` block and rendered through `textContent`, so an event titled `<img onerror=...>` renders as that literal string. `_safe_json` escapes `</` (an unescaped `</script` would close the block early and turn the rest of the payload into live markup) and U+2028/U+2029 (valid JSON, line terminators to a JS parser). Pinned by `tests/test_html_report.py::TestUntrustedContent`.

**Dates are bucketed against the *local* date, not UTC.** Event dates are local wall-clock dates scraped from venue pages — a 9pm SF show is `2026-07-30`, not an instant. Bucketing them against `datetime.now(timezone.utc).date()` put every event happening tonight into `Past` from 5pm PDT onward, which is exactly when someone opens the page to decide what to do that evening. `today` is injectable for deterministic tests. This was found by rendering the page in a browser, not by a test — the tests computed both sides of the comparison the same way and agreed with each other.

**And bucketed in the browser, not by the builder · changed 2026-08-19.** `_bucket` still stamps a `bucket` on every event in `events.json`, and other readers of that file use it, but **the page ignores the field** — `_SCRIPT`'s `bucketOf` recomputes it against `new Date()` at render time. The reason is the serving model, not the arithmetic: `servers/site.py` writes these files once per refresh and serves them unchanged for `interval_hours`, **24 by default and not aligned to midnight**. Observed 2026-08-19: `calendar.html`, built 16:46 the previous afternoon, still carried `data-today="2026-08-18"`, so the 19th sat under "Tomorrow" for the whole of the 19th. A page also outlives its build in the other direction — a tab left open overnight on a phone — so both pages re-check the local date on a 60s timer and on `visibilitychange`, and re-render only when it actually changed. Two copies of one rule is a drift risk, so `tests/test_day_rollover.py` lifts the threshold ladder out of the JS with a regex and runs it against `_bucket` for every day from a week back to a year out.

**When the scheduled refresh runs · changed 2026-11-26.** `site.py`'s `_loop` runs a harvest every `interval_hours` counted from the start of the process: each run is due one interval after the last one was *due*. It used to sleep one interval after the last one *finished*, so a 45-minute harvest moved the daily run 45 minutes later every day (measured on `smallblackbox`: process started 06:09:34 on 2026-11-24; harvests queued 06:09:36 on 11-25 and 06:54:46 on 11-26). A run that overruns whole intervals skips them. The hour of day is therefore whatever hour the unit was last started at; there is no setting for it (`not implemented`).

**Forwarding the file forwards the data.** It embeds every event and the source channel each came from. `_prepare` therefore whitelists display fields only; internals like `_status` never reach the file.

## 0a. `outputs/calendar_html.py` — the month grid · **added 2026-08-12**

**Domain:** the same events, arranged by *when* instead of by rank. `generate_calendar_html(validated_events, output_path, title="Calendar", today=None, nav=False)`. Same contract as §0 — one file, no network, scraped text through `textContent` only.

**Why a second page rather than a view toggle on the first.** The list answers "what is there?" and sorts by relevance; the grid answers "what is on Thursday?" and cannot sort at all, because position *is* the date. The list is happy to hide events behind a filter; the grid must render every day including the empty ones or it is not a calendar. Merging them meant a mode flag threaded through rendering, sorting, filtering and the URL. Two pages over one projection is less code and neither compromises.

**Both pages are built from `html_report._prepare`**, in the same `_write` pass, from the same list. That is what stops them disagreeing about a harvest: a field that reaches a card is available in a cell for free, and a field the projection drops is invisible to both.

| | |
|---|---|
| Grid | Six rows always — a grid that changed height between months would move the buttons under the cursor. Sunday-first, weekends tinted, today outlined |
| A cell | Up to 3 pills (time + title), then `+N more`. Clicking opens the day panel below the grid with the full events, time-ordered |
| Undated | Its own tray button, because a grid cannot place them and dropping them would be a silent loss. **1 of 240** on 2026-08-12 |
| Export | `icsForMany` — one `.ics` of everything visible in the month. Thirty separate downloads is not an import |
| Filters | The same two axes as the list (place, source), ANDed, plus search. No sort control: the grid *is* the sort |
| Keys | `←`/`→` page the month, `T` returns to today — suppressed while the search box has focus |
| Phone | Below 700px a seven-column grid leaves ~44px per cell, so the pills are hidden and each day shows a count badge. The day panel does the reading |

**No thumbnails, and that is the point.** The list page embeds 95 inline JPEGs — 1276 KB of its 1562 KB. A grid cell is 96px tall and could not show one usefully. Measured 2026-08-12: **247 KB against 1562 KB for the same 240 events**. This is the one to open on a phone.

**Local dates, again, and here it is worse.** `new Date('2026-08-12')` parses as **UTC midnight** and renders as the 11th anywhere west of Greenwich. The grid is built with `new Date(y, m, d)` and day keys are assembled by string formatting; `toISOString()` appears nowhere. This is the same defect as the `Past`-bucket one in §0, except a calendar renders 42 cells and would have been wrong in all of them at once. `tests/test_calendar_html.py::TestDateHandling` asserts the absent constructs, since there is no DOM to inspect.

**Which day is outlined comes from the browser · changed 2026-08-19.** `TODAY` was `document.body.dataset.today` — the build date — on the reasoning that the outlined cell should agree with the "generated" line in the header. That does not survive the serving model above: the file is rebuilt every `interval_hours` and served unchanged in between, so it outlined the 18th all through the 19th. The header still carries the build time, which is where a build time belongs; the outline is a claim about *today*, and only the viewer's clock knows that. `data-today` is still written, and remains the fallback. Same 60s + `visibilitychange` rollover as the list page — a `render()` here rebuilds all 42 cells, so it fires only when the day actually turned.

### `outputs/ics.py` — the shared iCalendar builder

JavaScript source, not Python: the pages are self-contained files with no server behind them, so the calendar has to be assembled in the browser and handed over as a `data:` URI. Extracted from `html_report._SCRIPT` on 2026-08-12 when the grid needed `icsForMany` — a copy-paste would have drifted the moment either page changed its date handling, which is exactly where it is delicate. Exports `icsFor` (one event), `icsForMany` (a calendar of many), `icsName` (a safe `download` filename). An event with no date returns `null` rather than a VEVENT at epoch zero. Line folding is deliberately **not** implemented: every client tested accepts long lines, and a wrong fold corrupts the file.

### The mobile pass — measured 2026-08-12

The list page had said "mobile-first" since 2026-07-30 and had never been opened at a phone width. Rendered in headless Chromium at 320×568, 360×740 and 390×844:

| | before | after |
|---|---|---|
| `/admin` document width in a 360px viewport | **456–546px — it scrolled sideways on every phone** | 360px |
| `/` sticky control block | **303px, 53% of an iPhone SE, permanently** | 61px |
| `/` first card begins at | **y=546 — the page opened showing no events** | y=256 |
| `/calendar` grid begins at | y=611 | y=369 |
| Undersized tap targets | 6–8 per page | 0 on `/`, 2 explained below |

**Re-measured 2026-08-13**, after the desktop pass below rewrote the same stylesheet: sticky block 59–60px, first card at y=249–250, one column at 292/332/362px, no sideways scroll. Every computed font size at 320 and 375px is identical to the values in this table's "after" column, because the fluid scale's floors *are* those values.

**`/admin` was the serious one.** Its cap rows are `110px 1fr 84px 44px`, and a range input's intrinsic width meant the row could not compress — so the number field and the ∞ toggle, *the two controls that actually set a cap*, sat off the right edge with nothing indicating they existed. The page was not cramped, it was unusable.

The cause was not the table's `overflow-x`, which was already set and did nothing. **A grid item's automatic minimum size is its min-content width**, so `section` refused to shrink below the 33-row Searches table regardless of what the table itself was told; the section was never the thing scrolling. `section { min-width: 0 }` is the fix, found by hiding elements one at a time and watching `document.scrollWidth` rather than by reasoning about the cascade.

**Source order decided two silent failures**, both invisible except on a touch device:

* The `@media (pointer: coarse)` block sat mid-stylesheet, above the base `a.btn` rule it overrides. Same specificity, so the later base rule won: the block asked for 44px and measured 41.
* `#filter-toggle { display: none }` was written *after* the media query that reveals it, hiding the filter button on exactly the screens whose filters are collapsed. It shipped that way for one build.

Both are now last in their stylesheets with the reason written above them, and `tests/test_mobile.py::TestSourceOrder` asserts the ordering.

**Two undersized targets remain, deliberately.** A calendar cell is 39px wide at 320px — seven columns cannot each be 44px in a 320px viewport, and the alternative is dropping columns or scrolling the grid. It is 62px tall, which is what a thumb actually needs. And `/admin`'s checkbox inputs are 20px inside a 44px `<label>`, which is the real hit area.

### `outputs/nav.py` — the cross-page links, and why they are opt-in

`/admin` was mounted, tested and working for a full day with **nothing anywhere pointing at it** — which is functionally the same as not having shipped it. A page with no inbound link is not a feature.

`nav_html(active)` renders the bar; `NAV_STYLE` carries its CSS; `base_script(active)` pins the mount root (below). Both generated pages take `nav=False` by default and `servers/site.py` passes `nav=True`, because a forwarded copy has no origin — `calendar` and `admin` in an emailed file resolve against whatever directory the recipient opened it from. `/admin` renders both unconditionally, since that page only ever exists on the origin.

### Sub-path mounts — **corrected 2026-08-12**

The site is reached through a reverse proxy at **`central-dev.zt/harvester`**, as well as directly on `:8800`. The first version of the nav emitted root-relative links (`/calendar`, `/admin`) and `/admin` used root-relative `fetch()` for all five of its API calls. Under the proxy every one of them resolves against the *proxy's* root — `central-dev.zt/calendar` — so the nav pointed off the application and the admin page could not talk to its own backend.

**No served page emits a root-relative URL.** Links and fetch targets are relative (`calendar`, `admin`, `admin/api/state`, `events.json`, `healthz`), which resolve correctly under any mount depth and on `:8800` too. There is deliberately **no configured base URL**: the same bytes are reached as `localhost`, as a LAN address, as a ZeroTier address and through the proxy, and hard-coding one breaks the other three. `tests/test_subpath_mount.py::TestNoAbsolutePaths` is the regression — a reintroduced `/` prefix breaks *only* behind the proxy and looks fine in every local check.

Relative URLs have exactly one failure case, and it is the URL that was reported: **`/harvester` with no trailing slash**, where the browser's base is `/` and `calendar` resolves to `/calendar` again. `base_script` closes it — each page strips its own name off `location.pathname` and inserts a `<base>` while `<head>` is still parsing, so the element is in place before anything uses it. The static hrefs are already relative, so a page whose script never runs is still correct wherever the path has a trailing slash: degradation, not dependence.

Resolved for all three pages × three mount spellings (`/`, `/harvester/`, `/harvester`), 15 distinct same-origin URLs, 2026-08-12.

**`servers/site.py` also takes `root_path`** (`--root-path /harvester`, `EH_SITE_ROOT_PATH`) which mounts every route under the prefix *in addition to* the bare ones. That is for the other proxy shape — one that forwards `/harvester/calendar` unchanged rather than stripping it. It is not how the pages find each other and is not needed for a stripping proxy. `normalise_prefix` accepts every spelling (`harvester`, `/harvester/`, `//harvester//`) because `Mount` accepts exactly one.

## 1. `outputs/report.py` — Markdown report · **DEPRECATED 2026-07-30**

> Superseded by `html_report.py`. `generate_report` still works and emits a `DeprecationWarning`; `--report FILE.md` still selects it. **This does not deprecate `obsidian.py`** — that writes Markdown into an Obsidian vault, where Markdown is the native format rather than a rendering choice.
>
> `ticktick_deep_link` moved to `outputs/deeplinks.py` and is re-exported here for back-compat, so the replacement does not import from the module it replaces.

**Domain:** disposable, local Markdown summary of one harvest run, with clickable TickTick deep links (`ticktick://x-callback-url/...`) so a human can add tasks by hand without the OAuth path.

**Runs at:** `harvest_cmd()` (`src/event_harvester/cli/commands/harvest.py:144-153`) when `--report` is passed (`nargs="?", const="events_report.md"`, so bare `--report` defaults the *filename*, not just enabling the flag — `src/event_harvester/cli/parser.py:126-130`); also from the MCP tool `generate_latest_report` (`src/event_harvester/servers/mcp_server.py:487-502`), which defaults `output_path="events_report.md"` and reads from the MCP harvest cache rather than a live pipeline run.

**Inputs:** `generate_report(validated_events, raw_events, links, source_counts, total_messages, output_path="events_report.md")`. Renders `validated_events` if non-empty, else falls back to `raw_events` — the two have different key shapes (`title`/`date`/`details` vs. `content`/`channel`/`dates`/`times`), and `_append_event()` (`report.py:94`) branches on `.get()` with fallbacks to paper over that.

**Outputs:** a single Markdown file at `output_path` (default `events_report.md`, relative to CWD — no directory is created for you). Plain `open(..., "w")` — **not atomic**, unlike the Obsidian writer (see §3). Full overwrite each call.

**Internal structure:**
- `ticktick_deep_link(title, start_date, all_day, content)` (`report.py:7`) — builds the `x-callback-url`. Note the date-normalization branch at `report.py:22-25` is `unverified` for anything other than `YYYY-MM-DD`, bare `YYYY-MM-DDTHH:MM`, or already-fully-qualified strings; it does a `len()`/`in`-based heuristic rather than real parsing, so a date string of unexpected shape (e.g. seconds already present but no offset) could silently produce a malformed `startDate`.
- `generate_report()` (`report.py:33`) — header block, `## Events & Scheduling` section (one `### N. Title` block per event via `_append_event`), `## Top Links` (top 15 by list order, not re-sorted here), or `"No events or links found."` if both are empty.
- `_append_event()` (`report.py:94`) — truncates title to 80 chars, content snippet to 200 chars; tags `PIN`/`SCHED` badges; appends the `ticktick_deep_link` under each event.

**Hard constraints:** none beyond disk write; this sink never talks to a network.

**Failure modes:** silently overwrites any existing file at `output_path` with no backup. If `output_path`'s parent directory doesn't exist, `open()` raises `FileNotFoundError` — uncaught, propagates to the CLI/MCP caller. No encoding issues expected (`encoding="utf-8"` explicit).

---

### Serving it: `servers/site.py` — **added 2026-07-30**

The site is a **file**, and serving it is a detail. That is why this module is small, and it is the property worth protecting: the served page and a forwarded page are the same bytes, so they can never drift apart.

Built on Starlette + uvicorn, which `mcp`/`fastmcp` already pull in — **no new dependency**. `event-harvester site` builds `data/.site/index.html`, serves it at `/`, and refreshes on a schedule (default 24h).

**The route map, as of 2026-08-12:**

| Route | File | |
|---|---|---|
| `/` | `data/.site/index.html` | the list, with thumbnails |
| `/calendar` | `data/.site/calendar.html` | month **and** week views (selectable toggle; the choice is remembered per-viewer in `localStorage`), no thumbnails. Week view is a time grid — day columns, an hour axis, events as blocks positioned by start time with overlapping events lane-packed side by side, plus an all-day row for timeless events. The day panel below the grid (shared by both views) bins the selected day's events by the hour they start: same-hour events sit side by side, empty hours are omitted, and timeless events fall into one "Time TBD" bin |
| `/admin` | rendered per request | caps, keywords, profiles — `servers/admin.py` |
| `/events.json` | `data/.site/events.json` | the same projection the pages embed |
| `/healthz`, `GET`/`POST /refresh` | — | state, identity, queue; `GET /refresh` is a control page (buttons + `/healthz` poll), `POST` triggers |
| `/debug/incomplete` | — | events with an empty key field (date/time/location/link), with per-field counts and each event's `missing` list — a data-quality view; `?field=` narrows it |
| `/debug/empty` | — | configured sources that produced no events — every web source and known messaging platform with its event count (zero-count first). Each web source also carries its cooldown state (`cooling`, `last_fetch_hours`), so the response splits the zeros into `skipped_sources` (empty because a run passed them over while cooling) vs `empty_after_fetch` (fetched and genuinely produced nothing — the ones worth looking at). Also `empty_web_sources`, `empty_platforms`, and `unattributed_web_events`. The "which source went dark, and why?" view |

`/` and `/calendar` are written together in one `_write` pass, so they cannot show different harvests. A calendar built lazily on first request would have been one more thing that can be stale. Both are served `no-store`: the point of the URL is that it is current.

**Every route above is also reachable under a prefix** when `--root-path` is set, and the pages link relatively regardless — see "Sub-path mounts" above. The site is proxied at `central-dev.zt/harvester`.

| Property | Why |
|---|---|
| Builds from **cache first**, then harvests in the background | A cold start that waited for a full harvest would serve `503` for minutes |
| A failed harvest keeps the **last good page** | The site must not go down because one source broke; the error surfaces on `/healthz` |
| Refreshes serialised by a lock | A harvest opens browsers and calls an LLM. Two at once doubles the cost and races on the output file |
| `/refresh`: GET is a page, only POST triggers | A link preview or crawler (both GET) must not start a harvest — so the trigger is POST, while GET serves a safe control page with buttons that POST |
| `POST /refresh` is fire-and-forget | A full harvest takes minutes; a blocking request dies at the proxy's read timeout (~60s). The POST returns `{"started": true}` at once and the page polls `/healthz`. Measured 2026-09-02: a blocking POST behind `central-dev.zt/harvester` hung the client until it was made non-blocking |
| Loopback by default | **No authentication.** These are personal events; `--host 0.0.0.0` needs a proxy with auth in front |
| Sleep floored at 60s | `--interval-hours 0.0001` would otherwise harvest in a hot loop |
| The page **re-renders**, it does not reload | A reload discards the search term, active filter and scroll position — the state a browsing session has built up |
| Live polling gated on `location.protocol` | The same file opened from disk has no server; polling from `file://` fails CORS and would log an error a minute, forever |
| `--reload` does **not** harvest per restart | A file save every few seconds would otherwise open browsers and spend tokens every few seconds. It shells out to `python -m uvicorn --factory --reload`, because uvicorn reloads by supervising child processes and cannot be driven from inside the CLI's loop |

### Rendering 240 cards without freezing — 2026-08-12

The page was built for 33 events. At **240**, with ~95 inline JPEGs, it locked the tab until everything was ready rather than filling progressively. Three compounding causes, and the images were the *least* of them:

| Cause | Fix |
|---|---|
| **240 forced reflows in one loop.** `syncDetailToggles` read `scrollHeight`/`clientHeight` for every `.details` in the document to decide whether the "More" button was needed. Each read forces a synchronous layout — the classic thrash, and the dominant cost | An `IntersectionObserver` measures each card once, when it comes within 400px of the viewport, then unobserves it |
| **Every card built in one synchronous pass** | The work list is flattened to individual cards and appended across animation frames on an 8ms budget. A `renderToken` cancels an in-flight build, because typing in the search box starts a new render almost per keystroke |
| **Offscreen cards still laid out, painted and decoded** | `content-visibility: auto` with `contain-intrinsic-size: auto 260px`. One CSS line, the single largest win, and the intrinsic size stops the scrollbar lurching |

The observer and `content-visibility` reinforce each other: under `content-visibility` an offscreen card has **no layout to measure**, so the old eager loop would have produced wrong answers as well as slow ones.

Both degrade cleanly. A browser without `IntersectionObserver` measures immediately, exactly as before; one without `content-visibility` renders everything, as before.

`known rough edge`: the file is still 1.5 MB and every byte must arrive before the first card can be built, because the events are embedded in the document. Chunking fixes the *freeze*, not the *download*. If that becomes the complaint, the fix is a smaller thumbnail budget or `SITE_THUMBNAILS=0` — not a second network request, which would cost the self-contained property.

### Thumbnails — added 2026-08-12

Cards carry a preview image when the event's `link` publishes one, read the way a chat app reads a link: `og:image`, then `twitter:image`, then schema.org `Event.image`. Generic on purpose — Luma, Eventbrite, Partiful and a venue's own site all publish one, and a per-platform path would need a new branch per source forever.

**Images are downscaled and inlined as `data:` URIs, never linked.** That is forced by what this file *is*. `test_no_external_resources` fails the build on any external `src`, and the footer promises "forwarding it forwards them" — a remote `<img>` would call home to `images.lumacdn.com` every time anyone opened a forwarded copy, from wherever they opened it. Inlining keeps both properties, and the page still works on a plane.

**Off by default** (`thumbnails=False`). This is the only part of report generation that touches the network, and a caller rebuilding from cached events should not silently acquire a dependency on the internet. The site turns it on (`SITE_THUMBNAILS=0` to stop it); `--build-only` and the tests do not.

Bounded three ways: a disk cache at `data/.thumbnails/` keyed by URL (a rebuild costs nothing), `max_links`, and a total byte budget so the page cannot quietly become a 40 MB attachment. Every failure is soft — a missing image is a missing thumbnail, never a failed report.

**Measured 2026-08-12** on 33 live events (22 with a link): **20 illustrated**, page 49 KB → 388 KB, 1.7 s cold and ~1 s warm.

| Source | Result |
|---|---|
| Eventbrite | **12 of 12** — after unwrapping its `og:image`, which points at a `/_next/image?url=…` proxy that answers 400 to anyone but its own front end. The real CDN URL is in the `url` parameter |
| Instagram | **8 of 8** — see below |
| Luma, Partiful | work directly |
| `forms.gle`, `lonelyhackers.club` | no `og:image` — correctly skipped |

#### The escaping bug that looked like an access restriction

Worth recording in full, because it was misdiagnosed **twice** as a property of the remote host.

Instagram's images returned 403. The first theory was a missing `Referer` — hotlink protection being the usual cause. Adding one changed nothing. The second theory, written into the code and the docs as fact, was that *"those URLs are session-signed and not reachable from here."*

Both were wrong, and the evidence was one `print` away the whole time. Dumping the parsed query parameters showed:

```
{'stp': 'c400…', 'amp;_nc_cat': '100', 'amp;oh': '00_AQFG…', 'amp;oe': '6A81F015'}
```

**Every parameter after the first was named `amp;…`.** A `content="…"` attribute is HTML, so a multi-parameter URL is written with `&amp;` between parameters, and reading it literally renames them all. The two that matter — `oh` (signature) and `oe` (expiry) — arrived under names the CDN does not read, so the request was genuinely unsigned and the 403 was correct. `html.unescape` on the extracted value fixed all 8, and a plain bot User-Agent then gets a 200 with no `Referer` at all.

The lesson is the one this codebase keeps relearning: **an error from a remote system describes our request at least as often as it describes their policy.** Two plausible stories about Instagram's access control were built before anyone looked at what was actually being sent. `_unescape` carries this history in its docstring; the `Referer` header is kept as a courtesy but is explicitly documented as **not** load-bearing, so its presence is never read as evidence that some host demanded it.

`known rough edge`: the Eventbrite unwrap must decode **exactly once**. The value looks doubly-encoded (`https%253A%252F%252F`) and unquoting until stable is the obvious thing to write — it produces a 400, because the inner host expects the CDN URL as an *encoded* path segment. `parse_qs` already performs the outer decode. Same family of bug as the above: encoding layers, counted wrong.

### The place filter — added 2026-08-12

A second chip row: **San Francisco · Oakland · Broader · Unplaced**, ANDed with the source row rather than replacing it, so "SF" + "web" means SF events from web sources. Both rows share one click handler that reads which axis a chip belongs to from its `data-` attribute, so a third axis costs a row of markup and a table entry, not a parallel copy of the logic.

**Classification is three signals, not a substring match**, because the same city arrives four ways — measured over 27 located events:

```
'San Francisco, CA'                          a city name
'484 5th Street, San Francisco, CA 94107'    a postal address
'Mother Bar, SF'                             an abbreviation
'El Rio'                                     a bare venue, no city at all
```

ZIP first (unambiguous), then city name, then a venue table. **That last case is why the venue table exists and is not optional**: the `raw`-mode sources are SF bars scraping their own homepages, and a bar does not write its own city on its own site. A city-name matcher files all of them under "unplaced" — which is exactly where the local queer nightlife would land, the events most likely to be wanted and least likely to be labelled.

Two ordering rules that matter, both learned from the data:

- **Named elsewhere-places are checked before the venue table.** `"DoubleTree San Jose"` would otherwise hit an SF venue substring and be filed as local.
- **`unknown` is its own bucket, never folded into `broader`.** "We could not tell" and "definitely elsewhere" are different answers, and only the first is a reason to extend this module. It is labelled *Unplaced* rather than *Other* for the same reason.

`oakland` covers the near East Bay — Berkeley, Emeryville, Alameda — rather than the city limits. A fourth chip for three events would be worse than a slightly wide one; the chip's `title` says so on hover rather than leaving it to be discovered.

**Measured 2026-08-12** on 33 live events: SF 23, Broader 4, Unplaced 6. The unplaced are genuinely unplaceable — five carry no location string at all and one is `"DEF CON, TBD"`.

`known rough edge`: the venue table is hand-maintained and only holds venues observed without a city. A new SF bar is `unplaced` until someone adds it. The durable fix is a `city` field on the web-source config, which does not exist yet.

### The filter bar — rebuilt 2026-08-11

Reported by a screenshot, not a test: **22 chips for 33 events, three of them completely blank**, and the same sender listed twice. All three symptoms had one cause — the chip label was `source.split(" in ")[0]`, i.e. the LLM's free-text *author*, because `EH-STD-EVENT-v0` defines no `platform` field on an event.

| Symptom | Cause | Fix |
|---|---|---|
| 3 blank chips | `<llamalounge@calendar.luma-mail.com>` interpolated **unescaped** into the markup, so the browser parsed it as an unknown element | `html.escape` on the label and the attribute. Model-derived text reaches the document as data, never as markup |
| 22 chips | one per sender — a filter with one option per row is not a filter | label by platform: `web 20 · gmail 10 · telegram 3` |
| `www.eventbrite.com` **and** `@www.eventbrite.com` | two spellings of one sender | same — both are `web` now |

**Where the platform came from is the part worth remembering.** It was never missing: `build_prompt` groups messages under `f"{platform.capitalize()} / {channel}"`, and the model echoes that group key back inside `source` — `"@someone in Gmail / INBOX"`. The old code split on `" in "` and kept the **wrong half**. So the fix needed no re-harvest and healed the already-cached page immediately, which a `_platforms` field alone would not have done (it is recorded too, by `analysis.attach_source_ids`, as the exact answer for events extracted from here on).

**A `platform` outside the known vocabulary is ignored rather than trusted.** `data/.site/events.json` stores *already-prepared* output, so rebuilding feeds `_prepare` the `platform` an older build wrote — the author's email. Without the vocabulary check the bad chips survived every rebuild and would have healed only after a fresh harvest. Measured: 22 chips → **4**, on the same cache file.

Also in this pass: search, sort and the layout toggle moved onto one row (they were stacked across three); chips wrap instead of scrolling horizontally, since horizontal scroll hid every filter past the viewport edge with no affordance saying so; each chip carries its own count, so a filter says how much it will show before it is clicked; and an explicit **All** chip replaced "re-click the active chip to clear", which was discoverable only by accident.

`n × m` layout: the grid is `repeat(auto-fill, minmax(var(--card-min), 1fr))`, so *n* comes from the viewport and *m* from how many events are in that date bucket — one column on a phone, nine on an ultrawide, from the same file with no media-query ladder. The Grid/Carousel toggle switches a single body class, so the cards are identical in both modes and switching costs a repaint rather than a re-render.

### The desktop half of that claim was false until 2026-08-13

The layout above was measured on a phone and at 1440px, and it was true at both. It was not true past 1400px, because **`main { max-width: 1400px }` capped the thing the grid was measured against**. One declaration produced two defects:

| viewport | columns | `main` | empty gutter | h1 at x | first card at x |
|---|---|---|---|---|---|
| 1280 | 4 | 1280 | 0 | 16 | 16 |
| 1920 | 4 | 1400 | 520 | 16 | 276 |
| 2560 | 4 | 1400 | 1160 | 16 | 596 |
| 3440 | **4** | 1400 | **2040** | 16 | **1036** |

The grid stopped resizing — four columns however wide the window got, which is what "not responsive" looks like from the chair. And because only `main` was centred while `header` and `.controls` stayed full-bleed, the **search field ran to x=3270 while the cards it filters began at x=1036**: a 1020px skew between the furniture and its content, on the same page, at the same moment.

The cap is gone; every band now shares the same 16px inset, so they align by construction rather than by a second number that has to be kept equal to the first. And the track itself is fluid — `--card-min: clamp(260px, 20vw, 340px)` — so a wide screen gains columns *and* keeps each card legible, instead of tiling ever more 270px slivers.

Measured after: **3 / 4 / 5 / 6 / 7 / 9 columns** at 1024 / 1280 / 1920 / 2240 / 2560 / 3440, tracks between 305 and 385px throughout, gutter 0 and skew 0 at every width, no horizontal overflow anywhere. The phone is untouched: 320/360/390 still show one column at 292/332/362px with the sticky block at 59–60px.

### The type scale is fluid, and it stops · **2026-08-13**

Five steps, each a `clamp()` interpolating between its phone value at 375px and its desktop value at 1600px:

| token | 320–375px | 1600px+ | used by |
|---|---|---|---|
| `--fs-xs` | 11 | 13 | chip tallies, footer |
| `--fs-sm` | 13 | 15 | meta, chips, location, source, bucket headings, controls |
| `--fs-md` | 14 | 16 | detail text, the date line, buttons, nav tabs |
| `--fs-base` | 16 | 18 | body, card titles, the search field |
| `--fs-lg` | 18 | 26 | `h1` |

**Every clamp's upper bound is a fixed px, never a vw.** A 3440px monitor is further from your eyes than a phone, not larger in your field of view, so text that keeps growing with the viewport is the failure mode — the scale saturates at 1600px and holds.

Three things this had to get right:

- **The floors are the sizes the 2026-08-12 phone pass measured.** At 320 and 375px every computed size is identical to what it was before this change, so a desktop improvement cannot cost the phone anything.
- **`--fs-base` floors at 16px deliberately.** Safari zooms the page when a focused input computes below 16px, and the search field is on that token.
- **Each size is now declared once.** `h1 { font-size: 18px }` in the phone media query was restating `--fs-lg`'s floor in a second place that could drift from it; it is gone, and `_STYLE` contains no numeric `font-size` at all. This is the same lesson as the source-order bugs in §0b from the other end — a value that appears twice can disagree with itself, and a value that appears once cannot.

`NAV_STYLE` uses `var(--fs-md, 14px)`: only the event list carries the scale so far, and without the fallback the declaration would be invalid on `/calendar` and `/admin` and the tabs would silently inherit the body size. **Those two pages still use fixed type** — bringing them across is a separate measured pass, since the calendar's seven-column month grid is the constraint that decides how far its text can grow.

Pinned by `tests/test_desktop_responsive.py` (27).

Search is AND across terms — "queer friday" narrows rather than matching either word — over title, location, details, source and date. Sort by date, priority or title.

### Detail on the card · **changed 2026-07-31**

Detail used to sit behind a `Details` button, hidden by default. Measured against live data it runs **50–146 characters**, so the button was concealing roughly one line — and part of that line was already on the card twice over, because the extractor emits `details` as `prose | location | url` while the card renders its own location row and its own Open button.

Now the prose is rendered inline, with the duplicated segments stripped for display only (`detailText`); search still runs against the raw `ev.details`, so a URL stays findable. The text is clamped to **three lines** so one long detail cannot stretch its card past its neighbours in the grid, and a `More`/`Less` toggle appears **only when the clamp actually bit**.

Two things that decide whether this works:

- **Clipping can only be known after layout**, and it changes with card width. `syncDetailToggles` compares `scrollHeight` against `clientHeight` in a `requestAnimationFrame` after render, and re-runs on the Grid/Carousel switch and on a debounced resize. The `+1` absorbs sub-pixel rounding — without it you get a `More` button that expands to reveal nothing.
- **`.btn` sets `display: inline-flex`, which beats the user-agent `[hidden]` rule**, so a hidden toggle would have rendered anyway. `a.btn[hidden], button.btn[hidden] { display: none }` puts it back. This is pinned by a test, because the failure is invisible in the common case where the button was going to show regardless.

## 1a. `outputs/ticktick_task.py` — the API object · **added 2026-07-30**

A typed, validated TickTick **Open API v1** task. Until now the payload was assembled inline in `create_ticktick_tasks` and handed to `ticktick-py`'s `task.builder(**kwargs)`, so what reached the API was whatever the pipeline happened to hold. Two things were wrong against the published schema, and **both were silent**:

| Defect | Effect |
|---|---|
| `priority` forwarded verbatim | The API accepts `0 \| 1 \| 3 \| 5`. The LLM emits 0–5, so **2 and 4 were sent as-is**. `coerce_priority` now rounds *up* — under-prioritising a real event costs more than the reverse |
| `dt.strftime("...%S+0000")` on a **naive** datetime | Every due date claimed UTC. A 9pm San Francisco show became `21:00Z` — **2pm local, seven hours early, on every sync**. `format_api_datetime` attaches the real local offset instead |

Also newly correct: `isAllDay` is inferred (a dated event with no time was previously a *timed* task at midnight), `startDate` is set alongside `dueDate`, `timeZone` is populated from the system zone when it can be determined, and `date` + `time` are parsed **together** so `"2026-07-30"` + `"9pm"` becomes 21:00 rather than midnight.

| Piece | Purpose |
|---|---|
| `Priority` / `TaskStatus` / `ItemStatus` | `IntEnum`s. `TaskStatus.COMPLETED == 2` but `ItemStatus.COMPLETE == 1` — the two vocabularies differ and are easy to conflate |
| `TickTickTask` | Field names are the **API's**, in the API's casing. This object is the wire contract; snake_case would put a translation layer between the schema and the thing that must match it |
| `.validate()` | Returns problems as strings. `create_ticktick_tasks` refuses a non-compliant task *before* the API call, so the failure names the field rather than arriving as a 400 |
| `.to_api()` / `.to_update_api()` | `POST /open/v1/task` and `POST /open/v1/task/{taskId}`. Unset fields are **omitted, not nulled** — an explicit null means "clear this" on update. The update body always carries `id` and `projectId`, without which it silently targets nothing |
| `.from_event()` | The pipeline-event → task path |

`unverified` — no request has been made against the live API with this object; there are no TickTick credentials in this checkout. The schema is taken from the published Open API v1 reference, and the sync path is covered by fakes in `tests/test_ticktick_task.py::TestIntegrationWithTheSyncPath`.

## 2. `outputs/ticktick.py` — TickTick OAuth + task sync

**Domain:** the only sink that mutates external state. Authenticates against TickTick's (unofficial, reverse-engineered) API via the third-party `ticktick-py` package, deduplicates events against existing tasks, and creates/updates/skips TickTick tasks.

**Runs at:** `harvest_cmd()` (`harvest.py:169-193`, skipped by `--no-sync`, non-mutating when `--dry-run`) and the MCP tool `approve_event` (`mcp_server.py:428-473`, one event at a time, per user approval).

### 2a. Auth flow

| Stage | Mechanism | Env vars | Cache | On expiry |
|---|---|---|---|---|
| OAuth2 (app-level) | `ticktick.oauth2.OAuth2` — authorization-code grant | `TICKTICK_CLIENT_ID`, `TICKTICK_CLIENT_SECRET`, `TICKTICK_REDIRECT_URI` (default `http://127.0.0.1:8080`) | `data/.token-oauth`, anchored to the repo root. The `ticktick-py` default is `cache_path='.token-oauth'` **relative to CWD** (`oauth2.py:54`), so the cache used to land wherever the process was launched from; `get_ticktick_client()` now passes an explicit `cache_path` and calls `_migrate_legacy_token_cache()` to move a pre-fix root-level token once (fixed 2026-07-29, `unverified` end-to-end) | `is_token_expired()` checks `expire_time - now < 60`; if expired, `validate_token()` calls `_request_access_token()` again, which **opens a browser and blocks on a console `input()` prompt** ("Enter the URL you were redirected to: ") — `oauth2.py:167-212`. There is no refresh-token grant in this library at all. `known rough edge`: this makes any unattended run (systemd service, MCP server with no TTY) hang or crash the moment the ~15-day-ish token window lapses. |
| Session login (per-client) | `TickTickClient(username, password, auth)` → `_prepare_session()` → `_login()` + `_settings()` + `sync()` | `TICKTICK_USERNAME`, `TICKTICK_PASSWORD` | none — re-authenticates with plaintext username/password **every time** `get_ticktick_client()` is called (every CLI run, every `approve_event` call) | failure raises inside `TickTickClient.__init__`, caught by `get_ticktick_client()`'s broad `except Exception` and logged, returning `None` |

`get_ticktick_client(cfg: TickTickConfig)` (`ticktick.py:17`) short-circuits to `None` with a `logger.warning` if `cfg.is_configured` is false (`TickTickConfig.is_configured` requires all four of client_id/secret/username/password — `src/event_harvester/config.py:76-86`; redirect_uri and project are not required). Callers (`harvest.py:177-179`, `mcp_server.py:458-464`) both treat a `None` client as "abort this sink," not a hard crash.

`side-effecting`: constructing a `TickTickClient` always calls `.sync()` once during `_prepare_session()`, and `sync_tasks()` (`ticktick.py:51`) calls `tt.sync()` again — this is a read against TickTick's servers on every invocation, not just when creating tasks.

### 2b. Task payload & idempotency

`create_ticktick_tasks(tt, events, project_name="", dry_run=False)` (`ticktick.py:115`):

1. `sync_tasks(tt, project_name)` — resyncs, then filters `tt.state["tasks"]` down to the resolved project (`_find_project_id`, case-insensitive name match; falls back to inbox if project not found, with a warning).
2. If there are existing tasks, delegates classification to `dedup_events_against_ticktick()` (`src/event_harvester/pipeline/dedup.py:35`), which:
   - Skips (no API call at all) any event with an **acted** fingerprint (`status in {"approved", "declined"}` via `find_acted_fingerprint()`, `src/event_harvester/pipeline/event_match.py:267`) — auto-saved fingerprints (e.g. from web link dedup) do **not** suppress here, only explicit user approve/decline.
   - Otherwise structurally matches the event against each existing task (title/date/location/link scoring via `events_match()`, threshold `score >= 2`) and classifies as create / update / skip.
3. **Creates** (`ticktick.py:171-212`): builds `tt.task.builder(title, projectId, content=notes, priority, dueDate)` and calls `tt.task.create(task_obj)`. Title truncated to 80 chars. `dueDate` parsed from `event["date"]` via `dateutil.parser.parse`, formatted `%Y-%m-%dT%H:%M:%S+0000` — `unverified` whether TickTick's API actually respects a bare `+0000` suffix vs. requiring milliseconds like the deep-link format in `report.py`; the two date-formatting code paths in this codebase (`ticktick.py` vs. `report.py:ticktick_deep_link`) are not shared and could drift.
4. **Updates** (`ticktick.py:71-112`, `update_task_details()`): merges `notes`/`link`/`location`/`time` into `task["content"]` only if not already substring-present (case-insensitive), fills `dueDate` only if the existing task has none, raises priority only if higher. Calls `tt.task.update(task)`.
5. Every create/update calls `save_fingerprint(event, ticktick_id, status="approved")`. *Changed 2026-07-30: this used to pass the default `status="auto"`, which `find_acted_fingerprint` deliberately ignores — so a CLI-synced event was re-offered as `new` in the next MCP or Flet run even though its TickTick task already existed. Creating a task **is** the approval, so it now records one.*

6. The returned dict carries a **`failed`** bucket alongside `created`/`updated`/`skipped`. Per-task exceptions are caught here so one bad event cannot abort the batch — which previously left callers with no way to distinguish a successful sync from a failed one. The Flet UI depended on exactly that distinction; see the Flet UI doc.

**Is creation idempotent on re-approval?** `unverified`/nuanced: not automatically. Re-running `create_ticktick_tasks` for the *same* event dict a second time depends entirely on `sync_tasks()` returning the just-created task from TickTick's server (i.e., sync latency/consistency) and on `events_match()` scoring it as a match. If TickTick's project has no existing tasks yet (`existing_tasks` empty at `ticktick.py:132`), dedup is skipped entirely and every event goes straight to `to_create` — so back-to-back calls in an empty/never-synced project **will** create duplicates. The MCP `approve_event` path additionally records a `status="approved"` fingerprint *after* task creation (`mcp_server.py:472`), so a second `approve_event(index)` call for the same cached index is blocked by `dedup_events_against_ticktick`'s fingerprint check — but only because the caller re-derives the same event from the same cached index; there's no TickTick-side dedup key (no external ID stored back onto the event, only `ticktick_id` into the *local* fingerprint store).

**Failure modes:**
- `sync_tasks()` swallows sync failures (`except Exception: logger.warning(...); return []`) — an empty result here is indistinguishable from "no tasks yet" and **skips dedup**, silently risking duplicate creation (see above). `BUG`-adjacent: this failure mode is not surfaced to the caller/CLI beyond a log line.
- Individual task creation failures are caught per-event (`ticktick.py:210-211`) and printed in red, but the event is otherwise dropped — not retried, not added to any result list, and no fingerprint is saved for it, so it will be retried as a fresh "create" on the next run.
- `update_task_details` failures return `None`, logged as an error; the caller demotes it to `"skipped"` rather than failing the whole batch.
- `dry_run=True` fully short-circuits before any `tt.task.*` call — confirmed by `tests/test_ticktick.py::test_dry_run_does_not_call_api` (asserts `tt.task.builder`/`tt.task.create` never called). This is the only fully safe-to-repeat mode.

---

## 3. `outputs/obsidian.py` — Obsidian vault writer

**Domain:** writes two kinds of Obsidian-flavored Markdown notes (callouts, checkboxes, tags, wikilinks) into a configured vault folder: a dated events digest and a dated recruiter-email digest. Also contains an unrelated interactive CLI helper (`reparse_recruiter_report`) that re-reads a written report and lets a human reply/trash recruiter emails from the terminal.

**Runs at:**
- `write_events_report()` — `harvest_cmd()` (`harvest.py:155-166`) when `--obsidian` is passed **and** `cfg.obsidian_events_dir` is set (env `OBSIDIAN_EVENTS_DIR`, `src/event_harvester/config.py:217`, `:329`; empty string by default, i.e. opt-in, no default vault path).
- `write_recruiter_report()` — `harvest_cmd()` (`harvest.py:118-122`, only when `--grade-recruiters` also produced grades) and `src/event_harvester/cli/commands/recruiters.py:56`, gated on `--obsidian` + `cfg.obsidian_recruiters_dir` (env `OBSIDIAN_RECRUITERS_DIR`).
- `reparse_recruiter_report()` — invoked separately (not part of the harvest pipeline itself; a human-in-the-loop follow-up step over an already-written recruiter report file). `side-effecting`: this function sends real Gmail replies and trashes real messages via `event_harvester.sources.gmail_reply`/`gmail_trash`.

**Inputs:** `write_events_report(validated_events, raw_events, links, source_counts, total_messages, output_dir, run_date=None)`; `write_recruiter_report(grades: list[RecruiterGrade], output_dir, run_date=None)`. `run_date` defaults to `date.today()` (`_run_date()`, `obsidian.py:21`).

**Outputs / target path & filename scheme:**

| Report | Path | Filename |
|---|---|---|
| Events | `{OBSIDIAN_EVENTS_DIR}/` | `Events {YYYY-MM-DD}.md` (run date) |
| Recruiters | `{OBSIDIAN_RECRUITERS_DIR}/` | `Recruiters {YYYY-MM-DD}.md` (run date) |

Neither writer creates `output_dir` if missing — `Path(output_dir) / "..."` then straight to `_atomic_write()`, which does `tempfile.mkstemp(dir=str(path.parent), ...)`; if the directory doesn't exist this raises `FileNotFoundError`. `known rough edge`: the vault directory must already exist; there's no `mkdir(parents=True)` anywhere in this module.

**Frontmatter fields:**
- Events: `date`, `type: events`, `sources: [..]`, `event_count`, `link_count`, `message_count`, `tags: [events-pool, harvester]`.
- Recruiters: `date`, `type: recruiter-grades`, `total`, then one line per action tier (`respond`, `review`, `ignore`, `trash`) with its count, `tags: [recruiters, harvester]`.

**Overwrite behavior:** `_atomic_write()` (`obsidian.py:25`) writes to a `.obsidian_*.tmp` sibling file then `Path(tmp_path).replace(path)` — atomic on the same filesystem, and the comment explicitly calls out this is to dodge OneDrive sync conflicts on partial writes. But because the filename is date-only (no run timestamp/sequence), **a second run on the same calendar day fully replaces the first's content** rather than merging or appending — confirmed by `tests/test_obsidian.py::test_overwrites_same_day`, which asserts the second call's `event_count` wins. `known rough edge`: any events surfaced only in an earlier same-day run and not carried forward by the caller are silently lost from the vault note (the pipeline doesn't accumulate across runs — each `harvest_cmd()` invocation passes only its own run's `validated_events`).

**Internal structure:**
- `_append_obsidian_event()` (`obsidian.py:122`) — near-identical to `report.py:_append_event` but with Obsidian syntax: `#event`/`#scheduled`/`#pinned` tags, `- [ ]` checkboxes for "Add to TickTick" (reuses `report.ticktick_deep_link`, imported at `obsidian.py:11`) and "Decide: attend or skip".
- `_append_recruiter_item()` (`obsidian.py:266`) — branches by `action`: `trash` → pre-checked `- [x]` with strikethrough Gmail link; `ignore` → plain bullet with live Gmail/Trash links; `respond`/`review` → unchecked `- [ ]` with live action links. Trash tier's Gmail link uses `~~...~~` even though the underlying email presumably still exists unless something else actually trashed it — `unverified` whether trash grading (`RecruiterGrade.action == "trash"`) implies the email was actually deleted from Gmail anywhere in this codebase, or whether the strikethrough is purely cosmetic/aspirational at report-write time.
- `reparse_recruiter_report()` (`obsidian.py:313`) — regexes actionable `respond`/`review` lines (`_ITEM_RE`) and their nearby Gmail message ID (`_GMAIL_ID_RE`, looks ahead up to 5 lines), then loops an `input()`-driven `o/r/t/s/q` menu per item. On reply/trash success it flips `- [ ]` → `- [x]` in-memory and does one final `_atomic_write()` back to the same file on quit or loop exhaustion. `side-effecting`, `unverified` for correctness beyond code reading (not exercised by `tests/test_obsidian.py`, which only covers the two `write_*` functions).

**Hard constraints:** vault directory must exist ahead of time; same-day reruns are destructive to previously-written same-day content; `reparse_recruiter_report` sends real emails and deletes real messages when the user picks `r`/`t`.

**Failure modes:** `FileNotFoundError` on missing `output_dir` (uncaught); `_atomic_write` cleans up its temp file (`Path(tmp_path).unlink(missing_ok=True)`) and re-raises on any write exception, so a failed write shouldn't leave a `.tmp` litter file behind or corrupt the previous version (the `replace()` swap only happens after a full successful write).

---

## 4. `ui/display.py` — terminal display

**Domain:** pure stdout formatting with ANSI color codes; no files, no network, no state. Entirely safe to re-run / call redundantly.

**Runs at:** throughout `harvest_cmd()` (`harvest.py`) as messages/links/events are produced; `print_recruiter_grades` is used by the recruiter-grading CLI path.

**Inputs / functions:**

| Function | Input | Notes |
|---|---|---|
| `print_message(msg)` | one raw message dict (`platform`, `timestamp`, `channel`, `author`, `content`, optional `pinned`) | color keyed by `PLATFORM_COLOUR = {"discord": BLUE, "telegram": CYAN}` — any other platform (gmail, signal, web) gets **no color prefix**, `unverified` whether that's intentional or a gap left when only two platforms had colors assigned |
| `print_links(links, max_links=15)` | scored link dicts | score-tiered color (`>=8` green, `>=5` yellow, else dim); truncates URL to 90 chars, context snippet to 80 |
| `print_validated_events(events, max_events=20)` | LLM-validated event dicts | score-tiered color at `>=10`/`>=6` thresholds (different cutoffs than `print_links`) |
| `print_raw_events(events, max_events=15)` | raw weighted events (fallback path when LLM unavailable) | score-tiered at `>=10`/`>=6`; prints a 120-char slice and appends `"..."` whenever that differs from the full string. *Fixed 2026-07-29: the check used to compare against `evt["content"][:200]`, so content between 120 and 200 chars was truncated with no ellipsis.* |
| `print_recruiter_grades(grades, max_items=30)` | `RecruiterGrade` objects | tier bands mirror the ones in `obsidian.py`'s recruiter report (`>=66` respond/green, `>=46` review/yellow, `>=21` ignore/dim, else trash/red) — kept in sync by convention only, not a shared constant |

**Outputs:** stdout only, via `print()`. No return values consumed anywhere; these are fire-and-forget display calls.

**Hard constraints:** none — assumes a terminal that understands raw ANSI escape codes (no Windows-console detection/`colorama` fallback visible in this file; `unverified` whether something upstream (e.g. `colorama.init()`) is called elsewhere in the app entrypoint to make this work on plain `cmd.exe`).

**Failure modes:** none observed — all functions guard empty-list inputs early and use `.get()`/dict indexing consistent with the shapes the pipeline actually produces; `print_raw_events` does use direct `evt["scheduling"]`/`evt["score"]` indexing (not `.get()`) unlike its `print_validated_events` sibling, so it would `KeyError` on a raw-event dict missing those keys — `unverified` whether the raw-event producer upstream guarantees them.

---

## Verification

No `--dry-run` variant touches all four sinks at once, but each can be checked in isolation without mutating TickTick or the Obsidian vault:

```bash
# Markdown report only (no TickTick, no Obsidian) — safe, writes one local file
pixi run event-harvester harvest --no-sync --report scratch_report.md
```

This exercises `outputs/report.py::generate_report` end-to-end and proves the pipeline → report path works, without invoking `ticktick.py` (`--no-sync` skips it entirely) or `obsidian.py` (omitting `--obsidian` skips it). Inspect `scratch_report.md` for `### N.` event blocks and `ticktick://x-callback-url` deep links.

To verify the TickTick path without creating real tasks:

```bash
pixi run event-harvester harvest --dry-run
```

`--dry-run` still requires a working OAuth handshake (it authenticates, then short-circuits before any `tt.task.*` call per `ticktick.py:157-160` / `:190-196`) — confirm no `+`/`~` task lines appear as real creates by checking the printed `[dry-run]` prefixes, and cross-check nothing new appears in the TickTick app itself.
