# Session Report — the job queue, process identity, and why 30 of 31 web sources were dead

**Date**: 2026-08-07 · **Status**: ✅ LANDED, with **one defect introduced** (§1)
**Scope**: job queue · service lifecycle + identity · the Playwright/LocalSystem fix · `rss` mode · 3 new sources · 2 design docs · a blueprints folder
**Tests**: 1018 passed, 0 failed (started this stretch at 755) · **Lint**: 43, unchanged throughout
**Follows**: [2026-07-31_dispatch-gateway-as-a-service.md](2026-07-31_dispatch-gateway-as-a-service.md)

---

## 1. Read this first: a fresh clone of `main` runs nothing

**I broke HEAD and did not notice until compaction prep.**

`src/event_harvester/sources/web_modes/__init__.py` and `web_fetch.py` — both committed by me in `0cbbae8` — import `event_harvester.sources.web_modes.instagram`. That module is **not in git**:

```
MISSING from HEAD, present on disk:
  src/event_harvester/sources/web_modes/instagram.py
  src/event_harvester/sources/instagram.py
  tests/test_instagram.py
```

**It is worse than "the top commit does not import."** Measured 2026-08-07 with the new `pixi run check-head`: **31 of 70 modules** fail from a bare checkout, because `web_modes` sits under `sources/__init__.py`, which everything transitively imports. The casualties include `cli`, `cli.dispatch`, `pipeline`, `pipeline.orchestrator`, `servers.mcp_server`, `ui.app` and `watch` — **every entry point the project has**. A fresh clone cannot run the CLI, the pipeline, the MCP server or the UI. The only reason this machine works is that it runs an editable install against the working tree, where the untracked file is present.

**`origin/main` is clean** (`check-head origin/main` → 70 modules, clean), so the damage is entirely inside the 25 unpushed commits and nothing published is affected. This is recoverable at zero cost to anyone else — provided it is fixed before the push.

**Cause.** Instagram GraphQL work was in the working tree as untracked files plus edits to `__init__.py`, `web_fetch.py`, `cli/commands/web.py` and `web_modes/feed.py`. When I staged those two files for the `rss` commit I picked up its *import lines* without its *modules*. `git add <file>` takes the whole file, including whatever uncommitted edits it already carried — the trap of staging by path in a dirty tree.

**Why the suite did not catch it.** Tests run against the working tree, where `instagram.py` exists. Nothing in this repo imported from a clean checkout. Same shape as every other defect this week: green over broken.

**The instrument now exists.** `scripts/check_head_imports.py` (`pixi run check-head [ref]`) exports a ref with `git archive` — which writes only what is committed — and imports every module with the repo's own `src/` removed from `sys.path`. It asserts the package resolved to the export, because an editable install shadowing it would make the check prove nothing. Run it before any push.

**The fix is to commit the Instagram work.** It is one coherent, finished change from 2026-08-06 — 953 + 215 lines of source, 645 lines of tests, its own status report, and doc updates across five files — and HEAD already references it. Committing it is additive and revertible; stripping the imports back out would instead delete a working, tested feature to make a broken commit consistent. Verified before committing: **1018 passed, lint 43**.

---

## 2. What exists now that did not

| | |
|---|---|
| `jobs.py` + `job_runner.py` | A harvest queue. **One runner, serial**; the MCP backend owns it and everything else submits |
| `event-harvester jobs {list,show,cancel,submit,prune}` | History, cancel, re-run |
| `buildinfo.py` | Every server reports version, git SHA at boot, staleness, pid, uptime |
| `service {up,down,doctor}` | Verified transitions, not just requests |
| `web_modes/rss.py` | One message per feed item — `raw` cannot read a dense listing page |
| 3 sources | `the-bitter-end`, `mato`, `bottom-of-the-hill` → **32 web sources** |
| `docs/10 - Blueprints/` | Repeatable operator procedures. First: adding a web source |
| `01 - Design/02`, `01 - Design/03` | The control UI, and the job queue |

---

## 3. The finding that mattered most

**30 of 31 web sources had not run for weeks, and nothing reported it.**

Five consecutive nightly harvests fetched exactly one source. The cause was not selection, cooldowns, or config:

```
web source failed: BrowserType.launch_persistent_context: Executable doesn't exist at
C:\WINDOWS\system32\config\systemprofile\AppData\Local\ms-playwright\chromium-1208\...
```

Playwright's browsers live under `%LOCALAPPDATA%`, which under LocalSystem is the systemprofile directory, where nothing is installed. **The whole standard group died on the first browser launch** — one exception, logged once per run. Only `erobay` survived, because `native_chrome: true` launches the machine-wide Chrome. That one surviving source is why it looked healthy for a week.

Fixing it exposed a *second* failure only visible afterwards: conda-forge's Playwright finds its Node driver via `CONDA_PREFIX`, which exists only after env activation, and `EventHarvesterMCP` had been hand-installed launching the `.exe` directly instead of through `pixi run`.

Both are now set by `build_service_env`, and `doctor` flags a browser-running service on LocalSystem that lacks them.

```
before   1 of 31 sources          after   Web sources: 499 messages from 31 sources
```

**This is the same root cause as Signal and Discord going quiet**: LocalSystem's profile is not yours. The service docstring warned about credentials; nobody had thought a browser cache was the same class of problem.

---

## 4. Measured, so the next reader does not have to re-derive it

| | |
|---|---|
| Full harvest, all sources | ~285–350 s |
| Web-only, 2 days, 31 sources | 499 messages → **123 events** (23.5 min, `parallel: 1`) |
| `bottom-of-the-hill` as `raw` | 30,164-char message → **1** event (the page itself) |
| `bottom-of-the-hill` as `rss` | 964 items → 105 in window → **65** events |
| The funnel, nightly | 15097 → 4824 (classifier) → **80 (caps)** → 44 (rerank) → 9–13 events |

**The pipeline is cap-bound, not source-bound.** Caps discard ~98% of what the classifier passes, and that 80 was pinned across five runs. Fixing the web path makes this *worse*, not better: web now brings 499 messages competing with Telegram's ~15,000 for `total=150`. Nightly runs will not see 123 events until the caps move. `CAP_WEB=30` against 105 available from one venue is the tight one.

---

## 5. Decisions worth not re-litigating

- **Serial execution, one runner.** Three singletons make concurrency unsafe: native Chrome's fixed `--remote-debugging-port=9222`, the single global message cache keyed on the platform set, and the positional latest-events cache. The queue is a *requests-stop-being-dropped* feature, not a throughput one.
- **The MCP backend owns the queue; the site submits.** A queue inside the site would have left the two-processes-both-harvesting bug exactly as broken while looking solved.
- **Claiming is `os.rename`.** Atomic, fails if the destination exists, so it is a mutex — correctness no longer depends on the single-runner convention holding.
- **Coalescing must not bump `submitted_at`** (the design doc said to; it was wrong — it starves the job that keeps being asked for).
- **Categories are many-per-source tags; `/admin` is one origin, split routes.** `01 - Design/02`.
- **`raw` does not scale with events-per-page.** One message out, ~one event out, however many shows are listed.

---

## 6. Known rough edges, recorded not fixed

- **`extract_event_links.js` compares hostnames exactly**, so a site serving `example.com` while linking `www.example.com` loses every link. Measured: 0 of ~40 on Bottom of the Hill, 2 with the `www` URL.
- **The message cache keys on the platform set, not the web-source selection**, so `--web-source A` can return B's messages. The code comment says this was a symptom of a fixed path bug; the cache-key half is still live. There is no `--skip-cache` flag.
- **stdio MCP bypasses the queue** — a per-client subprocess must not own a runner. `data/.harvest.lock` is the recorded fix.
- **`EventHarvesterMCP` binds `0.0.0.0` with no auth** and is a *service*, so it survives reboots. Flagged three times; deliberately not changed, because it alters a binding that may be reached from elsewhere.
- **`data/.web_traces` is 124 MB** with no cleanup policy.

---

## 7. State at handoff

| Fact | Value |
|---|---|
| Tests | **1018 passed**, 0 failed, ~33 s |
| Lint | 43 — unchanged all week |
| Git | `main` **25 ahead** of `origin/main`. **31 of 70 modules do not import — see §1.** `origin/main` itself is clean |
| Services | `EventHarvesterSite` RUNNING @`c081004` (restarted 2026-08-07, no longer stale), `EventHarvesterMCP` RUNNING, `EventHarvesterGateway` not installed |
| Queue | empty |
| Site | `http://192.168.1.67:8800`, firewall rule scoped to LocalSubnet, **no auth** |
| Not mine | `.dvc/` + 6 pointers, and the **Instagram work** (§1) — neither committed by me |

`service doctor` reproduces most of this in one command, and did in fact discover the stale site on its own.

---

## 8. What the next session should pick up

1. **Resolve §1** by committing the Instagram work. Nothing should be pushed first, and `pixi run check-head` must pass before the push.
2. ~~Restart `EventHarvesterSite`~~ — **done 2026-08-07.** It was running `5600542` against `c081004`; now `@c0810047d364`, and `doctor`'s findings dropped 3 → 2. It has the `rss` mode.
3. **Revisit the caps** (§4). The supply problem is fixed; allocation is now the binding constraint and it is a config change, not code.
4. **`parallel: 1`** — 23.5 minutes for one serial web sweep. Raising it multiplies concurrent browsers on a 7.9 GB box, so it wants a deliberate number.
5. Still open from before: **roadmap 2.2** (`ui/app.py`, zero tests, mutates TickTick) blocks the Reflex migration and `/admin`.

## Verification

```powershell
pixi run test                                   # 1018 passed
pixi run lint                                   # 43, all pre-existing
pixi run check-head                             # 31 modules fail until §1 lands
pixi run check-head origin/main                 # clean — nothing published is broken
pixi run event-harvester service doctor         # 2 findings, both known
pixi run event-harvester jobs list              # queue state
```

**`pixi run` needs the site service stopped** if it has to re-solve the environment: `EventHarvesterSite` holds `Scripts/event-harvester.exe` open, and pixi's editable reinstall fails with `os error 32`. Fall back to `.pixi/envs/dev/Scripts/ruff.exe` / `.pixi/envs/default/python.exe` directly, or stop the service first. Found 2026-08-07, right after restarting it.

`unverified` — no push, no TickTick write, and the `rss` mode has been exercised against one feed only.
