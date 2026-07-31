# Session Report — The events site: served, scheduled, hot-reloading; plus a compliant TickTick object

**Date**: 2026-07-30 · **Status**: ✅ LANDED
**Scope**: HTML report → served site → Windows service → hot reload · TickTick Open API v1 object · link-extraction provenance · OTel metrics · the navigation theory doc
**Tests**: 755 passed, 0 failed, 0 warnings (started the day at 479) · **Lint**: 43, unchanged all day
**Follows**: [2026-07-30_otel-native.md](2026-07-30_otel-native.md)

## 1. Where to start reading

This is the **fourth** report dated 2026-07-30. In order: [tier1-fix-pass](2026-07-30_tier1-fix-pass.md) → [api-spike](2026-07-30_api-spike-tier-4-7.md) → [commits-spike-and-gmail](2026-07-30_commits-spike-and-gmail.md) → [routes-and-hints](2026-07-30_routes-and-hints.md) → [otel-native](2026-07-30_otel-native.md) → this one. If you only read one, read this one and follow the links backwards as needed.

## 2. What exists now that did not this morning

| | |
|---|---|
| `event-harvester site` | Serves the events site on `127.0.0.1:8800`, re-harvests every 24h, `n × m` responsive grid + carousel toggle, AND-term search, sort, `/events.json`, `/healthz` |
| `event-harvester service install` | Runs the above as a Windows service under NSSM. **Installed on this machine, currently stopped** |
| `--reload` | Dev mode. Restarts on source change, deliberately does *not* harvest per restart |
| The page updates itself | Polls `/healthz`, re-renders in place — preserving search, filter and scroll |
| `outputs/ticktick_task.py` | A real Open API v1 task object |
| `outputs/html_report.py` | Self-contained mobile HTML; **Markdown deprecated** |
| Link provenance | Every extracted link carries `strategy` + `region` |
| `observability/metrics.py` | OTel metrics, and span status that actually marks failures |
| `00 - Theory/01` | Navigation as a planning problem (GOAP/POMDP) — argument only, nothing built |

## 3. Five bugs, none found by the test suite

This is the pattern of the day and it is worth stating plainly, because it says what to distrust.

1. **`event-harvester site` had never once worked.** `run_site` called `uvicorn.run()`, which calls `asyncio.run()` internally, inside the CLI's already-running loop. Instant death. Found by installing the NSSM service and seeing `SERVICE_PAUSED`. The tests drove `build_app` through `TestClient` and never the entry point.
2. **Every TickTick due date claimed UTC.** A naive datetime from a venue page got `+0000` stapled on, so a 9pm SF show was sent as `21:00Z` — **2pm local, seven hours early, on every sync**, silently.
3. **The HTML report bucketed local dates against the UTC date**, so from 5pm PDT everything happening *tonight* sorted into "Past" — exactly when you would open it to decide what to do that evening.
4. **Region keys gave one region per link.** `regionOf` queried the card for a heading, and an event card's title *is* an `<h3>`. Measured on Eventbrite: 27 links, 27 regions. Useless, and it looked like it worked.
5. **The hint rules called a healthy page an error at 0.85 confidence** — a bare `\b404\b` matching "1,404 going". The confidence was the real damage: 0.85 is above the escalation threshold, so `auto` would never have taken the screenshot that disproves it.

Two recurring shapes: **a test that constructs both sides of a comparison the same way** (2, 3), and **a test that exercises the object instead of the entry point** (1). Both produce green suites over broken software.

## 4. Decisions worth not re-litigating

- **Markdown reports deprecated, not deleted.** `--report FILE.md` still works and warns. **Obsidian is untouched** — Markdown is native there, not a rendering choice.
- **The site is a file, and serving it is a detail.** The served page and a forwarded page are the same bytes, so they cannot drift. This is why `servers/site.py` is small and why live-polling is gated on `location.protocol`.
- **Reflex was requested and not used.** The events site is read-mostly — search and filter over a fixed dataset — which static HTML already does, while Reflex would add a server process, websocket state sync and a Node/Bun toolchain for a page needing none of them. Reflex's real case is the **review UI** (`ui/app.py`, 1456 lines, mutating, writes TickTick), and that wants roadmap 2.2 first. Recorded as **2.15**.
- **JSPyBridge considered and declined** for DOM testing: Playwright was already installed and a real Chromium beats a JS DOM shim, which returns zeros from `getBoundingClientRect` — and this codebase's visibility checks filter on exactly that.
- **`install` does not start the service, and does not set the account.** The first start harvests everything and spends tokens; the account command takes a password.

## 5. State at handoff

| Fact | Value |
|---|---|
| Tests | **755 passed**, 0 failed, 0 warnings, ~17s |
| Lint | 43 — unchanged since this morning; ~25 introduced and all fixed |
| Git | Branch `fix/tier0-tier1-and-feed-probe`, **13 commits** ahead of `main` at `ed3a4ee`. **Working tree clean** apart from the 8 untracked `.dvc` paths |
| Roadmap | Tier 0 closed except 0.9 · Tier 1 closed · **Tier 1b** (Gmail) 3/4 · **Tier 1c** (TickTick) 3/4 · 2.1 closed · 4.1/4.3/4.4/4.5/4.7/4.10/4.11 closed · 4.6 partial · 2.14 closed |
| Machine state | **`EventHarvesterSite` service installed and STOPPED.** Runs as LocalSystem until an account is set |
| Not mine | `.dvc/`, `.dvcignore`, six `*.dvc` pointers — untracked, never committed. `.env` never read or edited |

## 6. What the next session should pick up

**Two things need *you*, not me:**

1. **Set the service account, then start it.** Under LocalSystem the Discord cache, Signal DB, Gmail token and browser profile are all invisible, so those sources go *quiet* rather than erroring — a harvest that looks fine with four fewer sources.
   ```powershell
   nssm set EventHarvesterSite ObjectName "WORKGROUP\jobhelp" "<password>"
   pixi run event-harvester service start
   ```
   Then check `service logs` and the per-source counts. **Session 0 has no desktop**, so the native-Chrome Cloudflare path (erobay) is unproven under the service — that is the thing to look at first.
2. **`GMAIL_TOKEN_JSON` in `.env` is still dead** (expired 2026-03-25). Survivable, but every Gmail call pays a failed round-trip first. Roadmap 1b.4.

**Then, in order of value:**

- **Roadmap 2.2 — `ui/app.py` has zero tests** and mutates TickTick. It has been the biggest hole all day, it blocks the Reflex migration, and today's approve-on-failure fix is still read-verified only.
- **Roadmap 4.13(a) — the cross-page chrome test.** A region present on both a listing page and a detail page is navigation, not results. Two page loads, no history, and it is the actual fix for the carousel problem; provenance only made it possible.
- **Roadmap 1c.4 / 4.9** — both blocked on credentials: verify a TickTick write against the live API, and settle whether Meetup's `eventSearch` is entitled on a free account.
- **Nobody has authored a route end to end.** `web_explore_*` → save → `web replay`. The engine works on a hand-written route against a live page; the seam between "agent saves it" and "harvest replays it from config" is still on trust.

## Verification

```powershell
pixi run test                                    # 755 passed, no warnings
pixi run lint                                    # 43, all pre-existing style
pixi run event-harvester site --build-only       # writes data/.site/index.html
pixi run event-harvester service status          # SERVICE_STOPPED, LocalSystem
pixi run python scripts/mcp_probe.py             # 29 tools, 2 resources
```

`unverified` — no TickTick write, no Meetup API call, no full harvest under the service. All three need credentials or a decision that is not mine.

`unverified` — the site has been exercised with synthetic events and with the 18 events in the live MCP cache, but never with a harvest it ran itself end to end.
