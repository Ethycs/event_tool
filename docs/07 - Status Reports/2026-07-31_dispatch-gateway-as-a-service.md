# Session Report — The MCP endpoint as a service, and the exposure guard in front of it

**Date**: 2026-07-31 · **Status**: ✅ LANDED
**Scope**: `servers/gateway.py` (new) · `servers/service.py` generalised to three services · CLI `--service`/`--all` · one new web source
**Tests**: 818 passed, 0 failed, 0 warnings (was 755) · **Lint**: 43, unchanged
**Follows**: [2026-07-30_site-service-and-ticktick.md](2026-07-30_site-service-and-ticktick.md)

## 1. What changed

Yesterday one thing could run as a Windows service: the events site. Today three can, and the reason is that a **Claude custom connector takes exactly one URL** — so if the MCP surface is going to be reachable remotely, the thing serving that URL has to be a supervised, auto-starting process rather than a terminal someone left open.

| `--service` | Windows name | Port | Faces |
|---|---|---|---|
| `site` | `EventHarvesterSite` | 8800 | loopback |
| `mcp` | `EventHarvesterMCP` | 8765 | loopback — reached *through* the gateway |
| `gateway` | `EventHarvesterGateway` | 9000 | **outward** |

`gateway` declares `DependOnService EventHarvesterMCP`. `--all` installs and starts in dependency order and stops and uninstalls in reverse, so a gateway is never left proxying a backend that is going away underneath it.

`gateway.py` moved from a loose script at the repo root into `servers/gateway.py`. The root file is now a launcher, so `pixi run gateway` is unchanged.

## 2. The part that matters: the endpoint is not read-only

Behind that one URL sit `harvest_events`, `approve_event`, `chat` and the `grok_*` family — tools that open browsers, spend LLM tokens, write to TickTick, and drive a **logged-in** browser session. On loopback that is fine; the only caller is you. On a public hostname, an unauthenticated endpoint hands all of it to whoever finds the name.

So `run_gateway` **refuses to bind a non-loopback address while `EH_GATEWAY_AUTH=none`**, and exits 2. It is a refusal rather than a warning on purpose: a warning in a service log is not a control.

Auth modes, and the distinction that actually decides the setup:

| Mode | For | Usable as a Claude connector? |
|---|---|---|
| `none` | loopback only | — |
| `token` | your own clients over a tunnel | **No** — the connector flow is OAuth |
| `jwt` | tokens minted elsewhere | **No** — same reason |
| `github` / `google` / `auth0` / `workos` / … | FastMCP proxies OAuth to the provider | **Yes** |

Secrets stay in `.env`, never in the service config: `AppEnvironmentExtra` lands in the registry in plaintext, so `build_service_env` carries only non-secret wiring and a test asserts no key ends in `TOKEN`/`SECRET`/`PASSWORD`/`KEY`.

## 2b. The site's scheduled harvest had never run

`servers/site.py:_collect` called `Pipeline(cfg).run()`. `Pipeline.run` takes a **required keyword-only** `no_kwargs` — the mapping that tells it which sources are switched off. So every scheduled harvest raised `TypeError` immediately, while the page kept serving the 18 cached events from the MCP file and looked entirely healthy.

`/healthz` was reporting it correctly the whole time (`ok:false`, with the `TypeError` in `error`). Nobody had looked, because the page rendered.

Fixed to `run(no_kwargs=cfg.sources.to_no_kwargs())` — the same call `cli/commands/serve.py` makes, so the site harvests exactly the source set the config enables rather than a hardcoded one.

**This is the third instance of one test shape.** `tests/test_site_server.py` patches `_collect` in every test — sensibly, since a real harvest opens browsers and calls an LLM. But that made `_collect` itself the one seam nothing checked. The fix is `TestCollectMatchesTheRealPipeline`, which autospecs `Pipeline` so the call site is validated against the genuine signature, plus a bind of the captured kwargs through `inspect.signature`. A stub accepts anything; an autospec does not.

## 2c. Hot reload on the gateway

`pixi run gateway --reload`, mirroring the site's: shell out to `python -m uvicorn ... --factory --reload`, because uvicorn implements reload by supervising child processes and that cannot be driven from inside an existing loop.

The exposure guard runs in **`make_app`**, not only in `run_gateway` — under `--reload` the worker is what binds and `run_gateway` is never called in the child. It also runs in the parent before spawning, so a bad config is one clear message instead of a reload loop of child tracebacks. Both are tested.

## 3. Found by running it, not by reading it

**The MCP backend has been listening on `0.0.0.0:8765` since 2026-07-28**, launched with `--allow-any-host`, with no authentication of any kind. It surfaced only because port 8765 refused to bind for a smoke test and the owning PID turned out to be that process.

This is worth stating plainly because it is the exact exposure the new guard exists to prevent — and the guard does not prevent it, because **the guard is on the gateway and this is the backend**. `--allow-any-host` disables a DNS-rebinding *check*; pairing it with `--host 0.0.0.0` is what opens the socket. Nothing in the code stops that. Recorded as a `known rough edge` in `03 - Architecture/08 - MCP Server.md`; whether it needs the same refusal is a decision, not a bug fix.

Also verified rather than assumed: with a single backend, FastMCP 3.4.5 proxies tool names through **unprefixed**. The old gateway docstring claimed `event-harvester_harvest_events`; it does not do that, and the docstring said so for as long as nobody looked.

## 4. Verified end to end

Not through `TestClient` alone — yesterday's lesson was that a test which exercises the object instead of the entry point produces a green suite over broken software.

```
pixi run gateway  (GATEWAY_HOST=0.0.0.0, auth=none)   → refuses, exit 2
pixi run gateway  (GATEWAY_HOST=0.0.0.0, auth=token)  → binds
GET  /healthz                                          → 200
POST /mcp  no token                                    → 401
POST /mcp  wrong token                                 → 401
POST /mcp  right token → initialize → tools/list       → 20 tools proxied
```

The suite also covers the 401/200 split over HTTP, because `create_proxy` takes `**settings` — an `auth=` FastMCP did not recognise would be swallowed silently and every construction-level assertion would still pass.

## 5. Also today

`the-bitter-end` added to `data/web_sources.json` — `raw`, `venues`, 6h cooldown. The URL given was the homepage; the configured URL is `/calendar`, because the homepage is a Squarespace one-pager returning 4619 chars of nav and gallery with 0 event links, while the calendar section's own URL returns 821 chars of nothing but the schedule. No iCalendar feed, no events collection in the sitemap. A live `--dry-run` harvest extracted `General Trivia, 2026-08-04 20:00`.

`known rough edge` — the page advertises four recurring nights (Mon pool, Tue trivia, Wed entertainment trivia, Thu party night) and the extractor returned one. That is the general raw-venue weakness across all 12 such bars, not something this source introduced: a weekly schedule is *n* recurring events and the LLM tends to mint the next instance of one.

## 6. State at handoff

| Fact | Value |
|---|---|
| Tests | **826 passed**, 0 failed, 0 warnings, ~25s |
| Lint | 43 — unchanged |
| Git | Branch `fix/tier0-tier1-and-feed-probe`; `main` at `ed3a4ee` |
| Machine | `EventHarvesterSite` **RUNNING**, bound `0.0.0.0:8800`, `/healthz` → `ok:true`. `EventHarvesterMCP` / `EventHarvesterGateway` **not yet installed** |
| Firewall | New inbound rule *Event Harvester site (TCP 8800)*, profile Any, scoped `RemoteAddress LocalSubnet`. The Wi-Fi profile is **Public**, which is why it is scoped |
| Running | An MCP backend on `0.0.0.0:8765` since 2026-07-28, PID-owned by a manual launch — see §3 |
| Not mine | `.dvc/`, `.dvcignore`, six `*.dvc` pointers — untracked. `.env` never read or edited |

### The LocalSystem warning was broader than the truth

`EventHarvesterSite` is running as **LocalSystem** and Gmail, Telegram and the native-Chrome path all worked: *"Gmail: refreshed credentials from token.json"*, *"Telegram: logged in as @semtioc, scanning 147 dialog(s)"*, *"Launching native Chrome ... Connected via CDP (port 9222)"*.

The distinction the earlier warning missed: **`token.json` and `harvest_session.session` live at the repo root**, which LocalSystem can read, while Discord's cache and Signal's DB live under `%APPDATA%`, which resolves elsewhere. So only those two go quiet, not four. Session 0 also did not stop native Chrome over CDP — the thing yesterday's report flagged as unproven.

`unverified` — no per-source count has been compared against a user-account run, so "only Discord and Signal are affected" is inference from the log, not a measurement.

### ZeroTier

`central-dev.zt` is **not reachable from this box**: no ZeroTier service, no `zerotier-cli`, no ZT interface. The only non-loopback address is `192.168.1.67` (Wi-Fi). Nothing here forwards to it; the Deploy Playbook's `infra@CENTRAL-DEV` predates this session and its transport is not up.

## 7. What needs you

1. **Decide about the backend on `0.0.0.0`** (§3). Either bind it to loopback and let the gateway be the only door, or accept it deliberately.
2. **Pick an OAuth provider** if you want the Claude connector. It needs an app registered with GitHub/Google/etc. and a callback pointing at your tunnel hostname — both require accounts I do not have.
3. **Pick a tunnel.** None of `cloudflared`, `ngrok` or `tailscale` is installed on this box.
4. Still open from yesterday: the service account for `EventHarvesterSite`, and `GMAIL_TOKEN_JSON` in `.env` is still dead (expired 2026-03-25).

## Verification

```powershell
pixi run test                                          # 818 passed, no warnings
pixi run lint                                          # 43, all pre-existing
pixi run event-harvester service status --service gateway
$env:GATEWAY_HOST='0.0.0.0'; pixi run gateway          # must exit 2
```

`unverified` — no OAuth provider mode has been run against a real provider; `github`/`google` are construction-tested only. No tunnel has been stood up, and nothing has been registered as a connector in Claude. All three need accounts or a decision that is not mine.
