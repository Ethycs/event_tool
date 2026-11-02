# CLI Command Reference

**Status:** CURRENT · **Date:** 2026-07-29 · **Applies to:** `src/event_harvester/cli/parser.py`, `src/event_harvester/cli/dispatch.py`, `src/event_harvester/cli/commands/*.py`, `README.md`

Seven top-level subcommands (`harvest`, `watch`, `web`, `recruiters`, `classifier`, `serve`, `mcp`), all rooted at `event_harvester.cli.parser:build_parser()`. Every flag table below is transcribed directly from `parser.py`, not from `README.md` or memory.

**This document is now the only complete flag list.** As of 2026-07-29 `README.md` is a landing page that shows a handful of common invocations and links here; it no longer carries flag tables of its own, precisely because the duplicate copy is what drifted. The `undocumented` markers below are therefore historical — they record which flags `README.md` never mentioned *before* that change, which is a decent proxy for "obscure", but their absence from `README.md` is now by design, not an omission. The two `BUG: README drift` items are resolved and kept for the same reason.

## Hard constraints

1. `--only` and `--skip` are mutually exclusive on every subcommand that has them (`harvest`, `watch`, `classifier train`, `serve`).
2. `-v`/`--verbose` and `-q`/`--quiet` are mutually exclusive on every subcommand.
3. Bare `event-harvester` (no subcommand, or leading flags with no subcommand token) defaults to `harvest`.
4. `classifier eval --labels FILE` is required — there is no default.
5. `web`, `recruiters`, `classifier` each require a second-level subcommand.
6. **There is no `--no-cap` flag.** `CapConfig.no_cap` is a real field, but it is reachable only through the MCP `harvest_events` tool's `no_cap` parameter, `data/profiles.json` (`{"caps": {"no_cap": true}}`), or the Flet UI checkbox — never through the CLI. *`README.md` documented it until 2026-07-29; the docs were corrected to match the parser rather than the flag being added. If the flag is actually wanted, that is a feature request.*
7. **`--cap web=-1` exits non-zero.** `parse_cap_arg` (`parse_helpers.py:51-54`) raises `ArgumentTypeError` for any negative value, even though the `-1` "unlimited" sentinel is real and honored everywhere else (`pipeline/analysis.py`, env var `CAP_WEB=-1`). Use `CAP_WEB=-1` in the environment. *`README.md` documented the flag form until 2026-07-29 and now documents the env form; `parse_cap_arg` was left alone because accepting the sentinel is a behavior change.*

## `harvest`

Default subcommand. Entry point: `commands/harvest.py:harvest_cmd`.

| Flag | Value | Default | Effect |
|---|---|---|---|
| `--days` | int | `None` (falls back to `DAYS_BACK` env, or `7`) | Sets `cfg.days_back` for this run. |
| `--only` | CSV platforms (`discord,telegram,gmail,signal,web`) | `None` | Harvest only these platforms; mutex with `--skip`. Also disables per-source caps for the run (`unlimited_caps=True`). |
| `--skip` | CSV platforms | `None` | Harvest all platforms except these; mutex with `--only`. |
| `--cap` | `KEY=VAL[,KEY=VAL]`, repeatable | `[]` | Merges into `cfg.caps`; later `--cap` wins on key collision. Valid keys: `discord`, `telegram`, `gmail`, `signal`, `web`, `total`. Rejects negative values (see constraint 7). |
| `--group-by-source` | flag | `False` | `dead code?` — sets `cfg.caps.group_by_source = True`, which nothing in the codebase reads (grep confirms only its definition and this one assignment site). The flag's own help text calls it "(deprecated) ... kept for compatibility." |
| `--no-analyze` | flag | `False` | Sets `cfg.skip_analyze = True`; harvest/filter/link-extraction still run, but no LLM event extraction and no TickTick sync. |
| `--no-sync` | flag | `False` | Skip TickTick task creation; events are still extracted and printed. |
| `--dry-run` | flag | `False` | Passed to `create_ticktick_tasks(dry_run=True)` — prints what would be created without creating it. `known rough edge`: does **not** set `cfg.dry_run` (a separate `AppConfig` field the Flet UI reads); the CLI flag and that field can disagree. |
| `--show-rejects` | flag | `False` | `undocumented`. Prints per-stage reject counts and writes `data/rejects.ini` with full reject details. |
| `--save` | path | `None` | `undocumented` label, but the flag itself is documented. Saves raw harvested messages to the given JSON file. |
| `--load` | path | `None` | Loads messages from a JSON file instead of harvesting live. |
| `--web-source` | name[,name…] | `None` | Restrict web harvesting to these named sources. Comma-separate and/or repeat the flag (e.g. `--web-source erobay,luma --web-source ra-sf`). Unknown names are skipped with a warning, not rejected. Any `--web-source` (like `--only`) scopes the run and bypasses per-source caps. |
| `--no-cooldown` | flag | `False` | `undocumented`. Ignore per-source web cooldowns and force re-fetch. |
| `--profile` | name | `None` | Apply a named profile from `data/profiles.json` before `--cap`/`--web-source` overrides are applied. |
| `--report` | path, optional value (`nargs="?"`) | `None`; `"events_report.html"` if the bare flag is given with no value | Generate a report. **Format follows the extension** — anything but `.md`/`.markdown` produces the self-contained mobile HTML view (inline CSS/JS, no network, safe to forward). A `.md` path selects the **deprecated** Markdown report and logs a warning. Default changed from `.md` to `.html` on 2026-07-30. |
| `--obsidian` | flag | `False` | `undocumented`. Write an Obsidian-compatible report; requires `OBSIDIAN_EVENTS_DIR` (events) and/or `OBSIDIAN_RECRUITERS_DIR` (recruiters, only with `--grade-recruiters`) to be set — the flag alone does nothing. |
| `--grade-recruiters` | flag | `False` | `undocumented`. Also runs Gmail recruiter grading as part of this harvest (chains into `commands/recruiters.py:_run_recruiter_grading`). |
| `--auto-trash` | flag | `False` | `undocumented`. With `--grade-recruiters`, auto-trashes recruiter emails scoring below 20. |
| `--debug-web-trace` | flag | `False` | Sets `cfg.web.debug_trace = True`; records navigation events to `data/.web_traces/<source>/<run-id>.jsonl`. |
| `--debug-web-screenshots` | flag | `False` | Sets `cfg.web.debug_screenshots = True` **and** `debug_trace = True` (screenshots imply trace). |
| `--debug-otel` | flag | `False` | Enables the OTel console span exporter for this run. |
| `-v`, `--verbose` | flag | `False` | `undocumented`. Sets log level to `DEBUG`; mutex with `-q`. |
| `-q`, `--quiet` | flag | `False` | `undocumented`. Sets log level to `WARNING`; mutex with `-v`. |

```powershell
pixi run event-harvester --days 14 --only discord,telegram
pixi run event-harvester --dry-run --no-sync --show-rejects
pixi run event-harvester harvest --profile tech-meetups --cap total=100 --report weekly.html
pixi run event-harvester harvest --report                # events_report.html, forwardable
pixi run event-harvester harvest --report legacy.md      # deprecated Markdown path
```

## `watch`

Entry point: `commands/watch.py:watch_cmd` → `watch.py:watch_mode`. Polls **only** Discord and Telegram — see the architecture doc for the loop mechanics.

| Flag | Value | Default | Effect |
|---|---|---|---|
| `--interval` | int (seconds) | `30` | `undocumented`. Poll interval; no min/max validation. |
| `--only` | CSV platforms | `None` | Only `discord`/`telegram` are meaningful — passing a gmail/signal/web name here logs a warning (`"Watch mode only supports Discord and Telegram"`) and that platform is still never polled. |
| `--skip` | CSV platforms | `None` | `known rough edge`: skipping gmail/signal/web (or leaving them un-skipped) produces **no** warning either way — the "unsupported platform" warning only fires when `--only` is used. |
| `--profile` | name | `None` | Applied to `cfg` before the loop starts; only affects fields watch mode never reads (web sub-source selection, caps). |
| `--debug-web-trace` | flag | `False` | `known rough edge`: accepted and sets `cfg.web.debug_trace`, but watch mode never touches the web source, so this has no observable effect. |
| `--debug-web-screenshots` | flag | `False` | Same caveat as above. |
| `--debug-otel` | flag | `False` | Same caveat as above (tracing is initialized in `dispatch.py` regardless of subcommand, but watch mode emits no web-fetch spans to trace). |
| `-v`, `--verbose` / `-q`, `--quiet` | flag | `False` | `undocumented`. |

```powershell
pixi run event-harvester watch
pixi run event-harvester watch --interval 60
pixi run event-harvester watch --skip gmail,signal,web
```

## `web {list, add, test, probe, replay, login}`

Entry point: `commands/web.py:web_cmd`. Manages `data/web_sources.json`.

| Subcommand | Purpose | Positional / flags |
|---|---|---|
| `web list` | List configured web sources | none |
| `web add URL` | Auto-diagnose `URL` (edge-wall probe → feed probe → browser), **log the attempt** to `data/.source_attempts.json`, then append to `data/web_sources.json` — **unless** diagnosis found a terminal wall (DataDome/403) or nothing to extract, which it logs and refuses without `--force`. A Cloudflare site is saved with `native_chrome`. | `url` (required); `--mode`, `--no-probe`, `--force` |
| `web test URL` | Same diagnosis, logged the same way, without saving | `url` (required); `--no-probe` |
| `web probe [URL…]` | **Added 2026-07-30.** Find iCalendar feeds without a browser. With no arguments, probes every configured source not already in `ics` mode | `urls` (optional, repeatable); `--apply`, `--timeout` |
| `web replay NAME` | **Added 2026-07-30.** Replay a configured source's recorded route, printing every step with its result and timing. Exits `1` if the route fails. The debugging surface for `mode: steps` — a harvest says "0 messages", this says which step stopped producing | `name` (required); `--diagnose` |
| `web login` | Open a browser to log into web sites and save session state | none |

| Flag | Value | Default | Effect |
|---|---|---|---|
| `--mode` (`add`) | `calendar`, `feed`, `search`, `raw`, `ics` | `None` (auto-detected) | Overrides the detected fetch mode. Passing it also **skips the feed probe** — an explicit mode is taken as the answer. |
| `--no-probe` (`add`, `test`) | flag | `False` | Skip the browser-free feed probe and go straight to Playwright. |
| `--force` (`add`) | flag | `False` | Add the source even when diagnosis found a terminal wall (e.g. DataDome) or nothing to extract. The attempt is logged either way. |
| `--apply` (`probe`) | flag | `False` | Rewrite configured sources that have a feed to `ics` mode. Only valid with no URL arguments — there is nothing to rewrite for an unconfigured URL. |
| `--timeout` (`probe`) | int (seconds) | `10` | Per-request timeout. |
| `-v`, `--verbose` / `-q`, `--quiet` | flag | `False` | |

**Probe-first ordering.** `web add`/`web test` now run `feed_probe.probe_feeds` before launching a browser. If the site publishes iCalendar, that is the answer, and starting Playwright to reach a worse recommendation is wasted time. On a hit, `web add` stores the **feed** URL rather than the page it was found on, plus a `source_page` field recording where it came from — otherwise a venue whose feed lives on a CDN or on `tockify.com` would be named after the platform.

**Edge-wall probe + attempt log (2026-11-01).** Before any of that, both commands run `onboard.probe_block` — a browser-free GET that names a bot wall from the response alone: `datadome` (terminal; even `native_chrome` cannot clear it, so the source is refused and the site's API is the only route), `cloudflare-challenge` (clearable — the source is saved with `native_chrome`, the erobay path), or `forbidden-403`/`rate-limited-429`/`server-error-5xx`. Every run then records one line to `data/.source_attempts.json` (`onboard.record_attempt`, newest-first, capped at 200) — URL, mode, link/feed counts, block reason, saved-or-not — so a dead source is explained once rather than re-diagnosed by hand. The `/sources` page reads the same log.

`.ics`/`webcal://` URLs skip discovery entirely and are validated directly.

**`--apply` safety rail:** a discovered feed with **0 upcoming events** is skipped rather than applied, and says so. Zero upcoming is equally consistent with "stale export nobody updates" as with "quiet month", and switching a working browser source to it would silently zero that venue.

```powershell
pixi run event-harvester web list
pixi run event-harvester web probe                      # audit every configured source
pixi run event-harvester web probe --apply              # ...and convert the hits to ics
pixi run event-harvester web probe https://somebar.com  # one-off check
pixi run event-harvester web replay meetup              # walk a saved route, step by step
pixi run event-harvester web replay meetup --diagnose   # ...and screenshot it if it breaks
pixi run event-harvester web test https://lu.ma/discover
pixi run event-harvester web add https://cdn.dnalounge.com/events.ics
```

## `site` — **added 2026-07-30**

Entry point: `commands/site.py:site_cmd` → `servers/site.py`. Serves the events site and keeps it fresh.

**Zero new dependencies** — `mcp`/`fastmcp` already bring Starlette and uvicorn, so the server is built on what was installed rather than on a framework added for the purpose.

| Flag | Default | Effect |
|---|---|---|
| `--host` | `127.0.0.1` | Bind address. **Loopback by default because this server has no authentication** and the events are personal |
| `--port` | `8800` | |
| `--interval-hours` | `24` | Hours between scheduled harvests. Floored at 60s so a typo cannot produce a hot loop |
| `--no-schedule` | off | Serve without the background refresh loop |
| `--no-harvest-on-start` | off | Serve the cached events; do not harvest at startup |
| `--build-only` | off | Build `data/.site/index.html` and exit without serving |
| `--reload` | off | **Dev.** Restart on source changes. Serves cached events and **does not harvest per restart** — a save every few seconds would otherwise open browsers and spend tokens every few seconds |
| `--harvest-on-reload` | off | With `--reload`, allow harvesting/scheduling anyway |

| Route | Purpose |
|---|---|
| `GET /` | The page. `Cache-Control: no-store` — a cached copy of yesterday's events is worse than a slow load. `503` with an explanation if it has not been built |
| `GET /events.json` | The same projection the page embeds, so a client cannot see a different set of events than a browser does |
| `GET /healthz` | Build age, event count, last error, whether a refresh is in flight. `503` when the last build failed |
| `GET /refresh` | A control page: two buttons that POST, and a live status readout that polls `/healthz`. A GET has **no side effect** (a link preview or crawler still cannot start a harvest), so navigating here in a browser gives buttons, not a `405` |
| `POST /refresh` | Manual rebuild — **fire-and-forget**: kicks the harvest onto a background task and returns immediately (`{"started": true}`), because a full harvest takes minutes and a blocking request times out behind a proxy. Watch progress on `/healthz`. `?harvest=0` rebuilds from cache. A second POST while one is in flight returns `{"already_refreshing": true}` (single-flight lock) |
| `GET /debug/incomplete` | Data-quality view: events with an empty key field (`date`/`time`/`location`/`link`; blank = missing/`TBD`/`None`). Returns `{ok, total_events, incomplete, missing_counts, events:[{index,title,source,…,missing:[fields]}]}`. `?field=link` narrows to one missing field. Reads the same cache the page serves |
| `GET /debug/empty` | Source-health view: which configured sources produced no events, and why. Returns `{ok, total_events, empty_web_sources:[names], skipped_sources:[names], empty_after_fetch:[names], empty_platforms:[names], web_sources:[{name,mode,events,cooling,last_fetch_hours,cooldown_hours}], platform_counts:{platform:n}, unattributed_web_events:n}`. Each empty web source is classified as `skipped` (cooling — a run passed it over) or `empty_after_fetch` (fetched, produced nothing). Web events are attributed by `ics:`/`web:` source-id prefix, then link host, then source-name match; `unattributed_web_events` counts those none claimed. Reads the same cache the page serves |

**Startup order matters**: it builds from the cached events *first* so the URL answers immediately, then harvests in the background. A cold start that waited for a full harvest would serve `503` for minutes.

**The page updates itself.** When served over http(s) it polls `/healthz` every 60s (and on `visibilitychange`, so a phone catches up on wake). If the build stamp moved it fetches `/events.json` and **re-renders in place** rather than reloading — a reload would throw away the search term, the active filter and the scroll position, which is exactly the state someone browsing has built up. Verified: with `night` typed in the search box, a rebuild took the counter from `1 of 2` to `1 of 3` without the box or the filtered view changing. Polling is gated on `location.protocol` being http(s), so the identical file opened from disk stays inert instead of failing CORS once a minute.

A failed harvest never takes the site down — the last good page keeps being served and the error surfaces on `/healthz`. Refreshes are serialised by a lock, because a harvest opens browsers and calls an LLM and two at once would double the cost.

```powershell
pixi run event-harvester site                       # http://127.0.0.1:8800, daily refresh
pixi run event-harvester site --build-only          # just write data/.site/index.html
pixi run event-harvester site --interval-hours 6
pixi run event-harvester site --no-harvest-on-start # serve the last harvest immediately
```

## `service {install, uninstall, start, stop, restart, status, logs}` — **added 2026-07-30**, extended 2026-07-31

Entry point: `commands/service.py` → `servers/service.py`. Runs a long-lived process as a Windows service via **NSSM**, which supervises it as a plain console process — auto-start, restart-on-crash, log rotation — without the app needing to know it is a service.

### Three services

`--service` selects which (default `site`, so the pre-2026-07-31 invocations are unchanged):

| `--service` | Windows name | Port | Process | Exposed? |
|---|---|---|---|---|
| `site` | `EventHarvesterSite` | 8800 | `event-harvester site` | loopback |
| `mcp` | `EventHarvesterMCP` | 8765 | `event-harvester mcp --transport streamable-http` | loopback — reached *through* the gateway |
| `gateway` | `EventHarvesterGateway` | 9000 | `pixi run gateway` | **this is the one that faces outward** |

`gateway` declares `DependOnService EventHarvesterMCP`, so the service manager starts them in order and a reboot does not race.

`--all` acts on every service in dependency order — reversed for `stop` and `uninstall`, so a gateway is never left proxying a backend that is going away underneath it. `--all` with `--name` is refused: one name cannot rename three services.

All subcommands take `--service` and `--name` (default: per service). `install` also takes `--host`, `--port` (defaults per service), `--interval-hours` (site), `--path` and `--allow-any-host` (mcp), `--backend-url` and `--allow-insecure` (gateway). `--allow-insecure` wires `EH_GATEWAY_ALLOW_INSECURE=1` into the gateway service so it will bind a non-loopback address with `auth=none` — for a network that authenticates on its own (a ZeroTier mesh), see Deploy Playbook §5b. `logs` takes `--lines` and has no `--all`.

Install/uninstall/start/stop **require an elevated shell**; `status` and `logs` do not.

### `up` / `down` / `doctor` — verified transitions · **added 2026-08-06**

`start` and `stop` ask the service manager to act. **`up` and `down` check that it happened**, and that distinction is the whole point — every lifecycle failure this project has hit was something that *looked* done.

| Verb | Does |
|---|---|
| `service down` | Stops every service in reverse order, then reports harvester **servers that survived** — the ones no service owns and `stop --all` silently leaves running |
| `service up` | Starts every installed service in order, health-checks each, and reports **queue depth before anything drains it** |
| `service doctor` | What is running, at what version, what is unmanaged, whether the queue is armed |

**Why `down` reports survivors.** A backend started by hand on 2026-07-28 was still serving on `0.0.0.0:8765` with no authentication eight days later. `stop --all` would never have touched it. Teardown that only stops what it installed is not teardown.

**Why `up` reports the queue first.** A job can sit queued for days and then run a full unattended harvest the moment a runner appears — that should be something you are told, not something you infer from the token bill.

**Process classification is ancestry-based, not PID equality.** NSSM's service PID is `nssm.exe`; the worker is a grandchild (`nssm` → `pixi` → `python`). Comparing PIDs directly reported every managed worker as unmanaged — twelve findings for three real processes on the first run. Chains are collapsed to one entry, and client-spawned **stdio** MCP servers are counted separately rather than flagged, because they are expected (they do, however, bypass the job queue).

### Process identity

Every long-lived surface now reports what code it is running — `version`, git SHA at boot, `stale`, `pid`, `started_at` — on `/healthz` (site, gateway) and in `config_summary` (MCP). See `src/event_harvester/buildinfo.py`.

The SHA is read out of `.git/` directly rather than by shelling out: a health check should not spawn a subprocess, and under LocalSystem `git` may not be on PATH. The **booted** SHA is cached deliberately, so it cannot follow the working tree — noticing that the checkout moved and the process did not *is* the feature.

`known rough edge`: a process that predates this reports no identity at all. `doctor` treats that absence as the staleness signal it is.

### The gateway is the dispatch endpoint

A Claude custom connector takes exactly **one URL**, so whatever is at that URL is the entire surface Claude gets. `install --service gateway` prints the exposure notice; the rules live in `servers/gateway.py` and the operator procedure in `docs/02 - Implementation/02 - Deploy Playbook.md` §5c.

The one hard guard: **`run_gateway` refuses to bind a non-loopback address while `EH_GATEWAY_AUTH=none`**, exiting 2 rather than warning. Behind it sit `harvest_events`, `approve_event`, `chat` and the `grok_*` family — tools that spend LLM tokens, write to TickTick, and drive a logged-in browser session.

### The account is the thing that will bite you

NSSM defaults a new service to **LocalSystem**, and almost every credential here is scoped to *your* user: Discord's cache under `%APPDATA%\discord`, Signal's DB under `%APPDATA%\Signal`, `token.json` and `harvest_session.session` at the repo root, and the browser profile the web sources log into. Under LocalSystem `%APPDATA%` resolves elsewhere, so **those sources go quiet rather than failing loudly** — a harvest that "succeeded" with four fewer sources.

`install` therefore does not pretend it handled this. It prints the command, which takes a password and so is yours to run:

```powershell
nssm set EventHarvesterSite ObjectName "DOMAIN\you" "<your-password>"
```

`service status` flags it in red until you do.

**Session 0**: a service cannot show a window. Headless Playwright is fine; the native-Chrome Cloudflare path is not. `/healthz` and per-source counts are how you tell which sources survived.

### Configuration written by `install`

| Setting | Value | Why |
|---|---|---|
| `Application` | the `pixi` executable | Everything goes through `pixi run`, services included |
| `AppDirectory` | repo root | `.env`, `data/`, `token.json` and the source list all resolve from it |
| `Start` | `SERVICE_AUTO_START` | |
| `AppStopMethodConsole` | 15 s | Ctrl-C first, so uvicorn's lifespan runs and the refresh tasks are cancelled |
| `AppStdout`/`AppStderr` | `data/.service/<service>.*.log` | One pair per service, rotated online at 10 MB |
| `AppExit Default` | `Restart`, 30 s delay, 10 s throttle | A service crashing on a bad credential must not spin at full speed |
| `AppEnvironmentExtra` | gateway only: `GATEWAY_HOST/PORT`, `EH_GATEWAY_PATH`, `EH_MCP_URL` | Non-secret wiring only — this lands in the registry in plaintext, so tokens and OAuth secrets go in `.env` |
| `DependOnService` | gateway → `EventHarvesterMCP` | Start ordering across a reboot |

**`install` does not start the service.** The first start of `site` or `mcp` runs a harvest — browsers, every configured source, LLM tokens — and that is a standing commitment that should begin when you say so.

The account warning is skipped for `gateway`: it is a pure proxy and reads no user-scoped credential of its own. `site` and `mcp` both need the account set.

```powershell
pixi run event-harvester service install                      # elevated; site
nssm set EventHarvesterSite ObjectName "$env:USERDOMAIN\$env:USERNAME" "<password>"
pixi run event-harvester service start
pixi run event-harvester service status
pixi run event-harvester service logs --lines 60 --service mcp
pixi run event-harvester service uninstall

# the dispatch pair, in dependency order
pixi run event-harvester service install --service mcp
pixi run event-harvester service install --service gateway
pixi run event-harvester service start --all
```

## `recruiters {grade, reparse}`

Entry point: `commands/recruiters.py:recruiters_cmd`.

| Subcommand | Purpose | Positional / flags |
|---|---|---|
| `recruiters grade` | Fetch Gmail-only, grade recruiter emails, optionally trash/report | see flag table |
| `recruiters reparse FILE` | Interactively act (open/reply/trash) on a saved recruiter report | `file` (required, path to the report Markdown) |

| Flag (`grade` only) | Value | Default | Effect |
|---|---|---|---|
| `--days` | int | `None` (falls back to `DAYS_BACK` or `7`) | `undocumented`. Sets `cfg.days_back`. |
| `--auto-trash` | flag | `False` | `undocumented`. Auto-trashes emails scoring below 20. |
| `--obsidian` | flag | `False` | `undocumented`. Writes a recruiter report to `OBSIDIAN_RECRUITERS_DIR` if set. |
| `--no-analyze` | flag | `False` | `undocumented`. Skips LLM-assisted refinement of borderline grades. |
| `-v`, `--verbose` / `-q`, `--quiet` | flag | `False` | `undocumented`. |

```powershell
pixi run event-harvester recruiters grade
pixi run event-harvester recruiters grade --days 3 --auto-trash
pixi run event-harvester recruiters reparse recruiter_report.md
```

## `classifier {train, eval}`

Entry point: `commands/classifier.py:classifier_cmd`.

| Flag (`train`) | Value | Default | Effect |
|---|---|---|---|
| `--in-labels` | path | `None` | `undocumented`. Load previously saved labels instead of harvesting + LLM-labeling now. |
| `--out-labels` | path | `None` | Save the (harvested-and-labeled, or loaded) label set to this file before training. |
| `--days` | int | `None` | `undocumented` for `classifier train` specifically. |
| `--only` / `--skip` | CSV platforms | `None` | `undocumented` for `classifier train` specifically; mutex with each other. |
| `-v`, `--verbose` / `-q`, `--quiet` | flag | `False` | `undocumented`. |

| Flag (`eval`) | Value | Default | Effect |
|---|---|---|---|
| `--labels` | path | **required** | Path to a labeled JSON file (created by `classifier train --out-labels`). |
| `--out-samples` | dir | `None` | Save up to 500 eval samples per stage to this directory for manual review. |
| `-v`, `--verbose` / `-q`, `--quiet` | flag | `False` | `undocumented`. |

```powershell
pixi run event-harvester classifier train --out-labels labels.json
pixi run event-harvester classifier train --in-labels labels.json
pixi run event-harvester classifier eval --labels labels.json --out-samples samples/
```

## `serve`

Entry point: `commands/serve.py:serve_cmd`. Launches the Flet review UI at `http://127.0.0.1:8550`; every flag below only seeds the **initial** `cfg` — subsequent runs are driven by the UI.

| Flag | Value | Default | Effect |
|---|---|---|---|
| `--days` | int | `None` | `undocumented`. |
| `--only` / `--skip` | CSV platforms | `None` | `undocumented`. Translated into `cfg.sources.*` booleans, mutex with each other. |
| `--profile` | name | `None` | `undocumented` for `serve` specifically (README's Profiles section says it applies, but no `serve` example is shown). |
| `--debug-web-trace` / `--debug-web-screenshots` | flag | `False` | Documented generically in the "Web debugging features" table; `--debug-web-screenshots` also enables the post-pipeline `quality_check` stage when an LLM/vision model is configured. |
| `--debug-otel` | flag | `False` | Documented generically. |
| `-v`, `--verbose` / `-q`, `--quiet` | flag | `False` | `undocumented`. |

```powershell
pixi run event-harvester serve
pixi run event-harvester serve --profile tech-meetups --debug-web-screenshots
pixi run event-harvester serve --skip web --days 3
```

## `mcp`

Entry point: `commands/mcp.py:mcp_cmd` → `servers/mcp_server.py:run_server_async`.

| Flag | Value | Default | Effect |
|---|---|---|---|
| `--transport` | `stdio`, `streamable-http` | `stdio` | Selects the MCP transport. |
| `--host` | string | `127.0.0.1` | `undocumented`. Bind host for `streamable-http`. |
| `--port` | int | `8765` | Bind port for `streamable-http`. |
| `--path` | string | `/mcp` | `undocumented`. HTTP path for `streamable-http`. |
| `--allow-any-host` | flag | `False` | `undocumented`. Disables FastMCP's DNS-rebinding Host-header check — needed behind a reverse proxy/tunnel, otherwise non-loopback `Host` headers get a 421. |
| `-v`, `--verbose` / `-q`, `--quiet` | flag | `False` | `undocumented`. Sets MCP server log level to `DEBUG`/`WARNING` (default `INFO`). |

```powershell
pixi run event-harvester mcp
pixi run event-harvester mcp --transport streamable-http --port 8765
pixi run event-harvester mcp --transport streamable-http --host 0.0.0.0 --port 9000 --allow-any-host
```

`.mcp.json` (project-local registration, README-documented) runs `pixi run event-harvester mcp` with the `stdio` default — none of the HTTP-only flags apply there.

## Environment variables

Every variable is read once, in `config.py:load_config()`, at process start — setting one mid-run (or after `cfg` is built) has no effect. "Mirrors" means a CLI flag can override the same `AppConfig` field for a single run; "none" means the field has no CLI equivalent at all.

| Env var | Mirrors flag | Notes |
|---|---|---|
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_PHONE`, `TELEGRAM_SESSION` | none | Credentials only. |
| `DAYS_BACK` | `--days` | Sets the `AppConfig` default; `--days` overrides per-run. |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` | none | `OPENAI_API_KEY` preferred; `ANTHROPIC_API_KEY` is the fallback. |
| `LLM_MODEL`, `LLM_BACKEND`, `LLM_MODEL_PATH`, `LLM_TOKENIZER_PATH`, `LLM_DEVICE` | none | |
| `VLM_MODEL` | none | README-documented tunable; no CLI flag exists. |
| `VLM_MAX_IMAGES_PER_RUN` | none | README-documented tunable; no CLI flag exists. |
| `DISCORD_CACHE_PATH` | none | |
| `GMAIL_CREDENTIALS_FILE`, `GMAIL_TOKEN_FILE`, `GMAIL_CREDENTIALS_JSON`, `GMAIL_TOKEN_JSON` | none | |
| `SIGNAL_DB_PATH` | none | |
| `TICKTICK_CLIENT_ID`, `TICKTICK_CLIENT_SECRET`, `TICKTICK_REDIRECT_URI`, `TICKTICK_USERNAME`, `TICKTICK_PASSWORD`, `TICKTICK_PROJECT` | none | |
| `WEB_SOURCES_FILE`, `WEB_MAX_EVENTS`, `WEB_TIMEOUT_MS` | none | |
| `WEB_NO_COOLDOWN` | `--no-cooldown` (`harvest` only) | |
| `WEB_SOURCE_NAMES` | `--web-source` (`harvest` only) | Both accept a CSV **list** of names now (the flag also repeats); aligned since 2026-09-03. |
| `WEB_PARALLEL` | none | README-documented env-only tunable. |
| `WEB_DEBUG_TRACE` | `--debug-web-trace` | |
| `WEB_DEBUG_SCREENSHOTS` | `--debug-web-screenshots` | |
| `CAP_DISCORD`, `CAP_TELEGRAM`, `CAP_GMAIL`, `CAP_SIGNAL`, `CAP_WEB`, `CAP_TOTAL` | `--cap KEY=VAL` | Env sets defaults; `--cap` merges on top per-run. Unlike `--cap`, these accept negative values (the "unlimited" sentinel) without error — see constraint 7. |
| `CAP_GROUP_BY_SOURCE` | `--group-by-source` | `dead code?` on both sides — see the `harvest` flag table. |
| `SOURCE_DISCORD`, `SOURCE_TELEGRAM`, `SOURCE_GMAIL`, `SOURCE_SIGNAL`, `SOURCE_WEB` | `--only`/`--skip` (`serve` only, partial) | Sets `cfg.sources.*` defaults, read mainly by `serve`/the Flet UI. `harvest`/`watch`/`classifier train` bypass `cfg.sources` entirely via `resolve_platforms()`. |
| `SKIP_ANALYZE` | `--no-analyze` | |
| `DRY_RUN` | `--dry-run` (`harvest`) — **not actually synchronized** | `known rough edge`: `harvest --dry-run` never writes back to `cfg.dry_run`; only the `DRY_RUN` env var and the Flet UI checkbox change that field. |
| `OBSIDIAN_EVENTS_DIR`, `OBSIDIAN_RECRUITERS_DIR` | `--obsidian` (`harvest`, `recruiters grade`) | The flag is a bool switch with no effect unless the corresponding dir env var is set. |
| `TELEGRAM_CHANNELS`, `TELEGRAM_EXCLUDE` | none | |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `--debug-otel` (partial) | Read directly in `dispatch.py`, not `config.py`. OTLP export is attached whenever this env var is set, **regardless** of `--debug-otel`; `--debug-otel` additionally attaches a console exporter. |

## README drift summary — **resolved 2026-07-29**

`README.md` was rewritten as a landing page in fix pass 1. Both drift bugs are gone, and the `undocumented` rows below no longer describe a defect: `README.md` deliberately does not enumerate flags any more, so "the parser has it and README doesn't" is now true of most of them by design. The table is kept as a record of which flags were never surfaced to a casual reader before that change.

| Direction | Item | Detail |
|---|---|---|
| ✅ was `BUG: README drift` | `--no-cap` | Documented in `README.md`; does not exist in `parser.py`. `CapConfig.no_cap` exists but is CLI-unreachable. **README corrected; flag not added.** |
| ✅ was `BUG: README drift` | `--cap web=-1` | Documented in `README.md` as the way to get an unlimited cap; `parse_cap_arg` rejects negative values, so this exact command is `SystemExit`. **README now documents `CAP_WEB=-1`; parser unchanged.** |
| historical `undocumented` (parser has it, README didn't) | `harvest`: `--show-rejects`, `--web-source`, `--no-cooldown`, `--report`, `--obsidian`, `--grade-recruiters`, `--auto-trash`, `-v`/`-q` | Listed in the `harvest` table above. |
| `undocumented` | `watch`: `--interval` | README shows bare `watch` only. |
| `undocumented` | `web add`: `--mode` | |
| `undocumented` | `recruiters grade`: `--days`, `--auto-trash`, `--obsidian`, `--no-analyze` | README shows bare `recruiters grade` only. |
| `undocumented` | `classifier train`: `--in-labels`, `--days`, `--only`, `--skip` | |
| `undocumented` | `serve`: `--days`, `--only`, `--skip`, `--profile` | README shows bare `serve` only. |
| `undocumented` | `mcp`: `--host`, `--path`, `--allow-any-host` | |
| `undocumented` | `-v`/`--verbose`, `-q`/`--quiet`, `--version` | Not mentioned anywhere in README, on any subcommand. |

## Verification

```powershell
pixi run event-harvester --help
pixi run event-harvester harvest --help
pixi run event-harvester mcp --help
```

Each `--help` output is generated straight from `parser.py`'s `add_argument(..., help=...)` strings — running all three and diffing the flag names against this document's tables is the fastest way to catch any drift introduced after this doc's date. `pixi run pytest tests/test_cli_parser.py -v` additionally proves the mutual-exclusion, required-subcommand, and `type=` validation constraints listed above actually hold.
