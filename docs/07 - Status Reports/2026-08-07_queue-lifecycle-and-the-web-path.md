# Session Report — the job queue, process identity, and why 30 of 31 web sources were dead

**Date**: 2026-08-07 · **Status**: ✅ LANDED, with **one defect introduced** (§1)
**Scope**: job queue · service lifecycle + identity · the Playwright/LocalSystem fix · `rss` mode · 3 new sources · 2 design docs · a blueprints folder
**Tests**: 1018 passed, 0 failed (started this stretch at 755) · **Lint**: 43, unchanged throughout
**Follows**: [2026-07-31_dispatch-gateway-as-a-service.md](2026-07-31_dispatch-gateway-as-a-service.md)

---

## 1. Read this first: `main` does not import

**I broke HEAD and did not notice until compaction prep.**

`src/event_harvester/sources/web_modes/__init__.py` and `web_fetch.py` — both committed by me in `0cbbae8` — import `event_harvester.sources.web_modes.instagram`. That module is **not in git**:

```
MISSING from HEAD, present on disk:
  src/event_harvester/sources/web_modes/instagram.py
  src/event_harvester/sources/instagram.py
  tests/test_instagram.py
```

**Cause.** Instagram GraphQL work was in the working tree as untracked files plus edits to `__init__.py`, `web_fetch.py`, `cli/commands/web.py` and `web_modes/feed.py`. When I staged those two files for the `rss` commit I picked up its *import lines* without its *modules*. `git add <file>` takes the whole file, including other people's uncommitted edits to it — which is exactly the trap of staging by path in a dirty tree.

**Why the suite did not catch it.** Tests run against the working tree, where `instagram.py` exists. Nothing in this repo imports from a clean checkout of HEAD. Same shape as every other bug this week: green over broken.

**Two ways out, and it is not my call which:**

1. **Commit the Instagram work** — it is referenced by HEAD already, so this is the smallest step to a working tree. But it is not mine to commit, and it is mid-flight.
2. **Strip the instagram imports back out of `__init__.py` and `web_fetch.py`** and re-commit — keeps history clean, but breaks the Instagram feature in the working tree until its owner commits.

**Do not push `main` until this is resolved.** It is 24 commits ahead of `origin/main` and the top commit does not import.

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
| Git | `main` **24 ahead** of `origin/main`. **Top commit does not import — see §1** |
| Services | `EventHarvesterSite` RUNNING (**STALE**, restart it), `EventHarvesterMCP` RUNNING, `EventHarvesterGateway` not installed |
| Queue | empty |
| Site | `http://192.168.1.67:8800`, firewall rule scoped to LocalSubnet, **no auth** |
| Not mine | `.dvc/` + 6 pointers, and the **Instagram work** (§1) — neither committed by me |

`service doctor` reproduces most of this in one command, and did in fact discover the stale site on its own.

---

## 8. What the next session should pick up

1. **Resolve §1.** Nothing else should be pushed first.
2. **Restart `EventHarvesterSite`** — `doctor` says it is running `5600542` while `0cbbae8` is checked out, so it does not have the `rss` mode.
3. **Revisit the caps** (§4). The supply problem is fixed; allocation is now the binding constraint and it is a config change, not code.
4. **`parallel: 1`** — 23.5 minutes for one serial web sweep. Raising it multiplies concurrent browsers on a 7.9 GB box, so it wants a deliberate number.
5. Still open from before: **roadmap 2.2** (`ui/app.py`, zero tests, mutates TickTick) blocks the Reflex migration and `/admin`.

## Verification

```powershell
pixi run test                                   # 1018 passed
pixi run lint                                   # 43, all pre-existing
pixi run event-harvester service doctor         # 3 findings, all known
pixi run event-harvester jobs list              # queue state
git ls-tree HEAD src/event_harvester/sources/web_modes/   # instagram.py absent — §1
```

`unverified` — no push, no TickTick write, and the `rss` mode has been exercised against one feed only.
