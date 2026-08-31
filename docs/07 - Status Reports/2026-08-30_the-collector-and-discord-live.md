# Session Report — the collector is born, pulls four sources, becomes a service

**Date**: 2026-08-30 · **Status**: 🟢 LANDED (collector, all pushed) · ⚠️ **event_tool has a large uncommitted pile the user lands; the collector service is built but not installed (needs an elevated shell + your password)**
**Scope**: a new standalone repo **`Ethycs/collector`** (submodule `ext/collector`) · event_harvester's Gmail digest-split · the collector consumer seam · the Discord BetterDiscord plugins (fixed + enabled + live)
**Follows**: [2026-08-24_ocr-flyers-discord-mods-and-the-rolling-window.md](2026-08-24_ocr-flyers-discord-mods-and-the-rolling-window.md)

---

## 1. Git state (read first)

| Repo | HEAD | Pushed? |
|---|---|---|
| **`ext/collector`** (`Ethycs/collector`, **private**) | `17d1ee2` | ✅ in sync with origin/main |
| **event_tool** | `1417ca2` (collector gitlink bumps) | 3 commits unpushed (the user pushes) |

The event_tool commits are **only** submodule gitlink bumps (`411c2d0`, `fddc4f9`, `8e68926`, `910d52e`, `b9132ee`, `1417ca2`). **All the actual event_harvester code/doc changes are UNCOMMITTED** — see §5. Consistent with this project's rule: I land collector commits (its own repo) and the gitlink; the user lands event_tool feature commits.

---

## 2. The collector — what it is, and that it works on real data

A standalone service that scrapes the local desktop-client sources **once, on a schedule**, normalizes to a validated record, stores append-only, and serves two consumers over REST (+ an MCP facade, unbuilt). Design and full gap report live in the submodule: `ext/collector/docs/01 - Design/00`, `06 - Roadmaps/01 - Gap Analysis.md`.

**All four sources pulled REAL data into the store this session** (under event_harvester's env, which has the deps):

| Source | Stored | How |
|---|---|---|
| Signal | **7,527** | local SQLCipher DB, no auth |
| Telegram | **7,086** | reused `harvest_session` — no re-auth |
| Gmail | **166** | reused `token.json` — no re-consent (`OAUTHLIB_RELAX_TOKEN_SCOPE=1`) |
| Discord | **68** | scheduled-events plugin export + cache (cache is **not** dead on this box) |

The store is at `ext/collector/data/{collector.sqlite,librarian.sqlite}` — **gitignored, real private data, this box only, unencrypted at rest**.

**The loop closed end to end**: event_harvester pulled 77 Discord messages *through the seam* and extracted **34 real events** (Noisebridge, furry meetups, book clubs — real dates/venues). Those were unreachable before today.

### The collector build arc (all pushed)
`29169fb` scaffold+seam → `8d5c791` port scrapers (signal/telegram/discord + poller) → `5f8f6b0` Gmail raw-fetch → `17e04f2` gap analysis → `98507cf` Discord capture ingestion (gap 4.2) → `17d1ee2` NSSM service. 24 tests green.

---

## 3. The Discord plugins — fixed, enabled, live

Both BetterDiscord plugins threw `ENOENT … \os` on enable — this BD build routes a plugin's bare `require("os")` through its addon loader as a *file path*. **Fixed** in both (`process.env.USERPROFILE` for the homedir fallback), copied into `%APPDATA%\BetterDiscord\plugins\`, and **the user enabled both — working**. The scheduled-events plugin wrote ~50 KB (67 events); the message-capture plugin writes JSONL as you browse. The collector now ingests both (scheduled events always; captured messages via the new `read_capture_messages`, which also fills the v1 superset fields).

---

## 4. The service (event_tool playbook) — built, NOT installed

`collector service install/start/stop/status/uninstall` (NSSM wrapper, mirrors `event-harvester service`). One process: `serve --poll-interval-min N` runs the scrape loop in a background thread **and** the fastapi API. Validated: env installs, real `serve` returned `/status` (all four) + `/messages` from the live store.

> **Not installed** — needs an **elevated shell** and your **Windows password** for the run-as-you step (or every source goes silently quiet under LocalSystem). Commands: `ext/collector/docs/02 - Implementation/03 - Deploy as a Service.md`.

---

## 5. Uncommitted in event_tool (the user's to land)

| File(s) | What |
|---|---|
| `config.py`, `pipeline/harvest.py`, `sources/collector_client.py`, `tests/test_collector_client.py` | **the collector consumer seam** (flag-off): `CollectorConfig` + routing + client, 12 tests |
| `sources/gmail.py`, `tests/test_gmail_bodies.py` | **Gmail digest-split** (list emails → one message per event), 12 tests |
| `.env.example` | new `COLLECTOR_*`, `GMAIL_DIGEST_*` vars |
| `integrations/discord-betterdiscord-plugin/*` | the `require("os")` fix (both plugins) + README |
| `docs/01 - Design/05`, `docs/03 - Architecture/02`, `docs/06 - Roadmaps/00` | design doc + reconciliations |
| `.mcp.json` | the user's gateway entry (theirs) |
| `web_modes/calendar.py`, `test_web_fetch_helpers.py`, `docs/03 - .. Web` | pre-existing, not from this session |

Full test suite was **1759 passing, lint 43 (baseline)** after the seam landed. The pre-existing `test_discord_events` time-bomb (a pinned timestamp aged past its 227h freshness window) still fails on a clean tree — unrelated, one-line fix available.

---

## 6. Open, in priority order

1. **Install the collector service** (§4) — elevated + `nssm set collector ObjectName ".\<you>" "<pw>"`. Bind `127.0.0.1` first (no auth; the store holds private data — gap 6.2).
2. **Collector Tier 1.1 — populate the superset.** Every stored record is still the v0 projection except captured Discord; `author_id`/`reply_to`/`reactions`/`edited_at` unpopulated for the others. This is the value gap: until it's done the collector stores what event_harvester already had.
3. **Stage 4 proof gate** — flip event_harvester to read from the running collector and drop the native scraper deps from its `pyproject.toml`; suite green with them **absent** = the split is real.
4. **Collector Tier 3** — `/channels`/`/authors`/`/media`, cursor paging, deletion-marking; **Tier 2.3** MCP facade.
5. **Land the event_tool pile** (§5) and push.

---

## Verification

```bash
# collector
cd ext/collector && ls .pixi/envs/dev >/dev/null && echo "env installed"
PYTHONPATH=src pixi run -e dev python -m pytest tests/ -q     # 24
git -C ext/collector log --oneline -1                        # 17d1ee2, pushed
# event_tool
git status --short                                           # the uncommitted pile (§5)
```

**First action next session**: decide install-the-service (§6.1, needs you elevated) vs close-the-superset (§6.2, I can do). The collector pulls real data today; the superset is what makes it worth more than a copy.
