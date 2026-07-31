# Output Sinks

**Status:** CURRENT · **Date:** 2026-07-30 (HTML report added, Markdown deprecated) · **Applies to:** `src/event_harvester/outputs/__init__.py`, `src/event_harvester/outputs/html_report.py`, `src/event_harvester/outputs/deeplinks.py`, `src/event_harvester/outputs/report.py`, `src/event_harvester/outputs/ticktick.py`, `src/event_harvester/outputs/obsidian.py`, `src/event_harvester/ui/display.py`

Four sinks turn harvested/validated events into something a human (or TickTick) consumes: a throwaway Markdown report, an Obsidian vault writer, a TickTick task sync, and ANSI terminal printers. Only `ticktick.py` mutates external state — `report.py`, `obsidian.py`, and `display.py` are local/idempotent and safe to re-run. `src/event_harvester/outputs/__init__.py` is a one-line docstring module with no exports to import from (`"""Sinks that write extracted events outward: TickTick, Markdown reports, Obsidian vault."""`) — callers import directly from the submodules.

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

**Interaction:** client-side search across title/location/details/source, per-platform filter chips, and date bucketing — `Today`, `Tomorrow`, `This week`, `This month`, `Later`, `Undated`, `Past`. `Undated` and `Past` sort **last** so a stale event never heads the page.

### Two properties worth not breaking

**Scraped text is never interpolated into markup.** Event titles and details come from arbitrary websites. The payload is embedded as JSON in a `<script type="application/json">` block and rendered through `textContent`, so an event titled `<img onerror=...>` renders as that literal string. `_safe_json` escapes `</` (an unescaped `</script` would close the block early and turn the rest of the payload into live markup) and U+2028/U+2029 (valid JSON, line terminators to a JS parser). Pinned by `tests/test_html_report.py::TestUntrustedContent`.

**Dates are bucketed against the *local* date, not UTC.** Event dates are local wall-clock dates scraped from venue pages — a 9pm SF show is `2026-07-30`, not an instant. Bucketing them against `datetime.now(timezone.utc).date()` put every event happening tonight into `Past` from 5pm PDT onward, which is exactly when someone opens the page to decide what to do that evening. `today` is injectable for deterministic tests. This was found by rendering the page in a browser, not by a test — the tests computed both sides of the comparison the same way and agreed with each other.

**Forwarding the file forwards the data.** It embeds every event and the source channel each came from. `_prepare` therefore whitelists display fields only; internals like `_status` never reach the file.

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

| Property | Why |
|---|---|
| Builds from **cache first**, then harvests in the background | A cold start that waited for a full harvest would serve `503` for minutes |
| A failed harvest keeps the **last good page** | The site must not go down because one source broke; the error surfaces on `/healthz` |
| Refreshes serialised by a lock | A harvest opens browsers and calls an LLM. Two at once doubles the cost and races on the output file |
| `POST`-only `/refresh` | A link preview or crawler must not be able to trigger a harvest |
| Loopback by default | **No authentication.** These are personal events; `--host 0.0.0.0` needs a proxy with auth in front |
| Sleep floored at 60s | `--interval-hours 0.0001` would otherwise harvest in a hot loop |
| The page **re-renders**, it does not reload | A reload discards the search term, active filter and scroll position — the state a browsing session has built up |
| Live polling gated on `location.protocol` | The same file opened from disk has no server; polling from `file://` fails CORS and would log an error a minute, forever |
| `--reload` does **not** harvest per restart | A file save every few seconds would otherwise open browsers and spend tokens every few seconds. It shells out to `python -m uvicorn --factory --reload`, because uvicorn reloads by supervising child processes and cannot be driven from inside the CLI's loop |

`n × m` layout: the grid is `repeat(auto-fill, minmax(270px, 1fr))`, so *n* comes from the viewport and *m* from how many events are in that date bucket — one column on a phone, four on a desktop, from the same file with no media-query ladder. The Grid/Carousel toggle switches a single body class, so the cards are identical in both modes and switching costs a repaint rather than a re-render. Measured: 1 column at 390px, 4 at 1440px, zero horizontal overflow at both.

Search is AND across terms — "queer friday" narrows rather than matching either word — over title, location, details, source and date. Sort by date, priority or title.

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
