# The control UI — browse, sources, categories, run

**Status:** DRAFT — **§0a is built, the rest is not** · **Date:** 2026-08-12 · **Applies to:** `src/event_harvester/servers/site.py`, `src/event_harvester/servers/admin.py`, `src/event_harvester/outputs/html_report.py`, `src/event_harvester/outputs/calendar_html.py`, `src/event_harvester/outputs/nav.py`, `data/web_sources.json`, `data/profiles.json`, `data/keywords.json`, `src/event_harvester/ui/app.py`, `src/event_harvester/cli/commands/web.py` · **Related:** roadmap 2.2, 2.15 · `03 - Architecture/05 - Outputs.md` §0a, §1, `03 - Architecture/07 - Flet UI.md`

A design doc, not a description. Filed as `DRAFT` so nobody mistakes it for as-built — the folder's freeze policy is "stable, corrections only". **§0a is the exception: it records what has actually shipped**, and is the section to trust. Everything from §1 onward is still design.

## 0. The honest baseline

Four capabilities exist, in four different places, with no single surface over them.

| Capability | Where it lives today | State |
|---|---|---|
| **Browse events** | `outputs/html_report.py` → `servers/site.py` at `:8800` | ✅ Works. Static, self-contained, no auth, forwardable |
| **Add / view sources** | `event-harvester web {add,list,test,probe,replay}` + 6 MCP tools | ✅ Works, CLI-only. No visual surface |
| **Categories** | `category` field in `data/web_sources.json` | ❌ **Dead metadata.** Set on 24 sources, all `"venues"`, **read by nothing** — the only code that touches it is `mcp_server.py:659`, which writes it |
| **Run a harvest** | CLI, Flet UI, MCP `harvest_events`, the site's 24 h timer, `POST /refresh` | ✅ Works ×5. None of them let you pick *which* sources from a browser |

And **profiles** already do part of the job categories were presumably meant for: `data/profiles.json` holds three, each a `name` + `web_sources[]` + `caps{}`, applied with `--profile`. So the system currently has one working subset mechanism (profiles, explicit membership) and one inert one (category, a tag).

That overlap is the first thing this design has to resolve, because "a page for running all or some categories" is ambiguous until it is.

## 0a. What is BUILT, as of 2026-08-12

This document is a design; most of it is still unbuilt. Three pages now exist on
the site origin:

| | | |
|---|---|---|
| **`/`** | the event list | `outputs/html_report.py` |
| **`/calendar`** | the month grid — added 2026-08-12 | `outputs/calendar_html.py`, see `03 - Architecture/05 - Outputs.md` §0a |
| **`/admin`** | the panels below | `servers/admin.py`, mounted by `servers/site.py` |

**They are linked to each other, which they were not.** `/admin` shipped
2026-08-10 mounted, tested and working, with nothing anywhere pointing at it —
not from `/`, not from the CLI banner, not from this table. It was asked for
again two days later by the person who had asked for it built, which is the
clearest possible signal: **a page with no inbound link has not shipped.**
`outputs/nav.py` is the fix, and `event-harvester site` now prints all three
URLs on startup.

`/admin` implements four panels:

| Panel | Backing store | Notes |
|---|---|---|
| **Caps equalizer** — slider + number + an **∞** toggle per source, plus `total` | `data/settings.json` (`config.load_caps_overlay`) | Overlay only. Delete the file and `.env`/defaults apply again, unchanged. `-1` = unlimited (**the default**), `0` = drop that source, otherwise clamped to `5000`; unknown keys dropped. Any negative normalises to `-1` rather than `0`, so a typo cannot silently mean "drop everything from this source" |
| **Keyword lists** | `data/keywords.json` (`keywords.py`) | Name + description + terms. Names are validated lowercase-kebab |
| **Profiles** — editor added 2026-08-12 | `data/profiles.json` | Name, description, and **checkbox** pickers for web sources and keyword lists. Per-profile cap overrides are displayed but edited in the file — the equalizer above is the place for caps |
| **Searches** — what each source will do with its keywords | derived, `keywords.plan_for_source` | Shows the **strength** of each: `native` / `category` / `local`. Live on 2026-08-12: 1 native (meetup), 1 category (luma), **31 local** |

The Searches panel is the one worth explaining. It exists because "search my
keywords on Luma, Meetup and Instagram" **cannot be delivered uniformly** — see
§2.5 — and a UI that listed sources without saying which of them can actually
be queried would be lying by omission. The table names the mechanism per
source, so the degradation is visible *before* a run rather than inferred from
disappointing output.

**Sources and keyword lists are checkboxes, not text fields.** Both vocabularies
are closed and already known to the page, and a typed source name that matches
nothing fails *silently at harvest time*, hours later — the classic shape of a
config bug that costs an evening. `__none__` gets its own checkbox ("skip web
entirely") and is mutually exclusive with picking sources, because "none of
them" and "these three" cannot both be the answer.

Still unbuilt from this design: `/admin/sources`, the tag vocabulary, the
`enabled` flag, and the run page.

**`/admin` had no tests until 2026-08-12**, which is how a real defect survived
in it: `api_profiles` clamped caps with `max(0, ...)`, so a profile carrying
`-1` was rewritten to `0` on every save — turning "unlimited" into "drop this
source entirely", the exact misreading `config._clamp_cap` exists to prevent.
Unreachable while profiles were hand-edited; reachable the moment the editor
above could round-trip one. `tests/test_admin.py` (25) is the regression.

**On access control — answered 2026-08-10.** These routes write config and the
origin carries no application-level auth, which earlier drafts of this document
and several status reports flagged as an open exposure. **It is not: the
services are served inside a ZeroTier mesh whose peers are all authenticated**,
so reaching the port requires being an authorized member. "Add auth to /admin"
is settled, not outstanding, and should not keep resurfacing as a blocker on UI
work.

The handlers still validate against closed vocabularies and write atomically
(temp + `os.replace`). That guards against *mistakes* rather than intruders —
a mistyped cap costs money at the LLM stage, and a half-written `keywords.json`
breaks the next harvest hours later, far from its cause. Worth keeping on those
grounds alone. A genuine escalation would be binding beyond the mesh.

## 1. Decisions taken

### D1 — Categories are tags, many per source

A source carries a **set** of tags. `venues`, `queer`, `tech`, `aggregator`, `weekly` are not mutually exclusive and the current one-category-per-source shape forces a false choice: The Stud is a venue *and* queer *and* weekly.

Profiles are **kept**, not demoted. They gain the ability to select by tag query in addition to naming sources explicitly.

*Cost, accepted:* "which category owns this source" stops having an answer, and tag vocabulary drifts without discipline — §2.3 addresses that. *Rejected alternative:* one category per source, profiles demoted to saved category-sets. Simpler to manage, but it would have required re-tagging on every overlap and profiles would have lost the ability to name a single odd source.

### D2 — One origin, split routes

`/` stays the static grid, unauthenticated. `/admin/*` is the control UI, authenticated, served by the same process on the same port.

The grid's defining property is that **the served page and a forwarded page are the same bytes** (`03 - Architecture/05 - Outputs.md`). That survives here: `/` is still a file. Splitting into two services would also have preserved it, at the cost of a second Windows service and a second thing to expose; folding everything behind auth would have destroyed it.

*Consequence to take seriously:* the site currently binds `0.0.0.0` with **no authentication at all** (changed 2026-07-31, firewall-scoped to `LocalSubnet`). Adding `/admin` to that origin means auth stops being optional — see §6.

## 2. Data model

### 2.1 `data/web_sources.json` — tags

```jsonc
{
  "url": "https://studsf.com/calendar",
  "name": "stud",
  "mode": "calendar",
  "tags": ["venues", "queer", "weekly"],   // new
  "category": "venues",                    // legacy, read-only fallback
  "cooldown_hours": 6,
  "enabled": true                          // new — see 2.2
}
```

**Migration is one pass and reversible**: where `tags` is absent and `category` is present, `tags = [category]`. Keep reading `category` as a fallback for one release, then drop it. All 24 affected sources carry the same value, so this is mechanical.

`EH-STD-WEBSRC-v0` is normative for this file and must be amended in the same change, not after — the standard already lists its non-conformances and this must not become another.

### 2.2 An `enabled` flag, because deleting is the wrong verb

Today the only way to stop harvesting a source is to remove its object. That loses the mode, the cooldown, the tags and the discovered URL — everything `web test` cost a browser run to establish. A source that Cloudflare started blocking should be *switched off*, not forgotten. `enabled: false` defaults to absent-means-true.

### 2.3 Tag vocabulary — derived, not declared

No `data/tags.json`. The tag list is the distinct union of tags across sources, so it cannot drift out of sync with reality. The admin UI offers existing tags first and free-text second.

The cost is typo-tags (`venue` vs `venues`), and the mitigation is the **merge** operation in §4.3 rather than an up-front controlled vocabulary — which would need maintaining and would block adding a source at 1am because the tag did not exist yet.

`known rough edge`: no tag means a source is invisible to every tag query. **This is not hypothetical — 6 of the 30 configured sources carry no category today**, and they are the highest-yield ones: `luma`, `sfchamber`, `erobay`, `instagram`, `eventbrite`, `meetup`. A run-by-tag that quietly dropped all six would look like a working harvest that had lost most of its output.

So the run page must always offer an explicit **"untagged"** pseudo-tag, and migration should tag those six rather than leave them to it.

### 2.4 `data/profiles.json` — tag queries

```jsonc
{
  "name": "queer-venues",
  "description": "Every queer venue, relaxed cap",
  "tags": { "all": ["venues", "queer"] },     // new: any / all / none
  "web_sources": ["qcal-sf"],                 // still explicit, unioned in
  "caps": { "web": 60, "total": 120 }
}
```

Resolution order, and it must be stated because it is where surprises live: **tag query → union with explicit `web_sources` → subtract `none` → drop `enabled: false`**. A disabled source is never harvested, however it was selected. `unverified` — no code implements this yet; it is a specification.

### 2.5 `data/keywords.json` — the operator's own words

**Built 2026-08-10.** Named lists, referenced by a source's `keywords` field
(`EH-STD-WEBSRC-v0`) so one list can feed several sources instead of each
inlining its own strings — which is how `meetup` came to be the only source
with any queries at all.

```jsonc
{"lists": [
  {"name": "ai", "description": "…", "terms": ["AI", "machine learning", "LLM"]}
]}
```

**A keyword cannot mean the same thing on every platform, and this is the
finding the whole feature rests on.** Measured 2026-08-10:

| Platform | Free-text search? | A keyword becomes |
|---|---|---|
| Meetup | yes | a query typed into the site — `native` |
| Luma | **no**, `api.lu.ma/search/get-results` → *"You are not signed in"* | one of 5 public categories if it matches, else `local` |
| Partiful | **no**, 5 trending per city is the whole public surface | `local` |
| Instagram | hashtag search needs a login | `local`, over the configured accounts |

So `keywords.plan_for_source` returns a **strength** — `native`, `category` or
`local` — and `/admin` renders it. The alternative, listing every source as
"searched", would have been a lie the user only discovered from thin results.

**Local matching tags rather than filters**, and that was a decision, not an
oversight. Everything fetched still reaches the pipeline; a match sets
`_keywords` and earns +4 in `analysis.score_message`, so it wins a cap slot
instead of the non-matches being deleted. Filtering was rejected because the
caps already discard ~98% of what is fetched (`07 - Status Reports/2026-08-07`
§4) — a keyword filter on top would narrow the funnel twice, and Luma is
currently the largest single source of real events.

Matching is word-boundary and case-insensitive. That is not a detail: the most
valuable short keywords — `AI`, `VC`, `ML` — are exactly the ones a substring
test ruins, firing on "blockchain" and "said".

## 3. Route map

| Route | Auth | What it is |
|---|---|---|
| `/` | — | The static grid. Unchanged |
| `/calendar` | — | The month grid. **Built 2026-08-12** — a second static file, same projection |
| `/events.json` | — | Unchanged |
| `/healthz` | — | Unchanged |
| `/admin` | ✅ | Dashboard: last run, per-source counts, what is stale |
| `/admin/sources` | ✅ | List, filter by tag, enable/disable, test, replay |
| `/admin/sources/new` | ✅ | The add-a-site flow (§5) |
| `/admin/sources/<name>` | ✅ | One source: config, last result, route steps, traces |
| `/admin/categories` | ✅ | Tag management: rename, merge, delete, bulk assign |
| `/admin/run` | ✅ | Pick tags/sources/profile + caps, dry-run, go |
| `/admin/jobs` | ✅ | History |
| `/admin/jobs/<id>` | ✅ | Live progress and log for one run |

Everything under `/admin` mutates or reveals configuration. Nothing under it may be reachable unauthenticated, including on a `GET`.

## 4. The screens

### 4.1 `/admin/sources` — view what is added

The list `web list` prints, made operable. Per row: name, mode, tags, enabled, last-harvest message count, last error, cooldown remaining.

The column that earns its place is **"last yielded"** — the count from the most recent harvest. A source that has returned 0 for three runs is either rotted or mis-configured, and today nothing surfaces that; you find out by reading a log. This is the same signal `web.route.yield` already meters (`03 - Architecture/04 - Observability.md`), so the data exists.

Row actions: **Test** (re-run diagnostics, no save), **Replay** (route sources only), **Enable/Disable**, **Edit tags**.

### 4.2 `/admin/sources/new` — add a site

Detailed in §5; it is the screen with the most design content.

### 4.3 `/admin/categories` — manage tags

Tags are derived, so this page manages them *through* the sources that carry them. Four operations, and each is a bulk edit of `web_sources.json`:

| Operation | Effect | Why it is needed |
|---|---|---|
| **Rename** | `venue` → `venues` everywhere | Typos are the predictable cost of free-text tags |
| **Merge** | `bars` + `venues` → `venues` | The same, after it has spread |
| **Delete** | Drop a tag from every source | Retiring a grouping |
| **Bulk assign** | Add/remove a tag across a selection | Tagging 24 venues one at a time is why nobody tags |

Each shows an **affected-source count before applying** and writes once. A rename that silently touched 24 sources would be indistinguishable from a bug.

### 4.4 `/admin/run` — run all or some

The page the request named. Three selection modes, and they compose into one resolved set shown **before** the run starts:

```
Run          ( ) Everything
             (•) By tag      [x] venues  [x] queer  [ ] tech  [ ] untagged
             ( ) Profile     [queer-venues v]
             ( ) Pick sources

Also         [x] Discord  [x] Telegram  [x] Gmail  [ ] Signal      ← non-web platforms
Caps         web 60   total 120                                     ← from profile, editable
Options      [ ] Dry run (no TickTick writes)   [ ] Ignore cooldowns

→ Resolves to 14 sources: stud, beaux, jolenes, oasis, … (2 disabled, excluded)
                                                              [ Run ]
```

**The resolved list is not decoration.** A tag query that silently matches nothing, or matches 40 sources when you meant 4, is the difference between a 30-second run and a 20-minute one that spends real tokens. Show the count and the names before committing.

Non-web platforms sit on the same page because a harvest is one pipeline run across all sources, not a web-only operation — the `--only`/`--skip` platform filter already exists and this is its UI.

### 4.5 `/admin/jobs/<id>` — watch it run

A harvest is minutes long (measured: **320 s** for the full source set on 2026-07-31), so this is a job, not a request. That is the single most consequential structural point in this document and §6.2 covers it.

Reuse the stage vocabulary that already exists in `ui/app.py:32` rather than inventing a second one:

```
harvest → filter → extract → match → quality_check → done
```

Per stage: state (pending/active/done/error), elapsed, and the per-source counts as they land. Plus a log tail, and **Stop**, which must be cooperative — `Pipeline` already awaits `self._checkpoint()` between stages (`orchestrator.py:150,190,219,239`), so stop takes effect at a stage boundary and is honest about that rather than pretending to be instant.

## 5. Adding a site — the flow worth designing carefully

`web add` already does the hard part: probe for a feed without a browser, then run Playwright, detect the mode, count event links, and suggest a config. The UI's job is to make that legible and correctable, not to reimplement it.

```
1  Paste URL           → feed probe (fast, no browser)
2  Browser test        → mode recommendation, link counts, sample, screenshot
3  Review & override   → mode, name, tags, cooldown, enabled
4  Save
```

Three things this screen should do that the CLI cannot:

**Show the screenshot and the hints.** `observability/hints.py` already diagnoses a live page — blocked, empty, needs-pagination, needs-scroll — and escalates to a vision model when unsure. On the CLI that output is prose. On a screen it belongs next to the actual screenshot, which is the difference between "0 links found" and "0 links found *because Cloudflare is showing a challenge*".

**Offer the better URL.** This is grounded in a real case from 2026-07-31: `thebitterendsf.com` returns 4619 characters of nav and gallery with **0 event links**, while `thebitterendsf.com/calendar` — a section of the same one-page site — returns 821 characters of pure schedule. A human found that by reading the HTML. The step-2 result should surface candidate subpaths (nav links containing `calendar`/`events`/`schedule`, sitemap entries) and their link counts, so the choice is visible rather than discovered.

**Make the mode override explain itself.** `the-cafe` is configured `raw` because the detector suggested `search`, which wants a query and is meant for result pages, not a venue homepage. That reasoning currently lives in a git commit message. The override field should take a one-line note and store it.

## 6. What this forces

### 6.1 Authentication is no longer optional

`/admin` mutates configuration and spends money. The site has **no auth today** and, since 2026-07-31, binds `0.0.0.0`. Three consequences:

1. `/admin` cannot ship before auth does.
2. The firewall rule scoped to `LocalSubnet` is not authentication — it is a smaller blast radius.
3. The gateway (`servers/gateway.py`) already solved the analogous problem and **refuses to bind non-loopback without auth**. The site should adopt the same posture for `/admin`: serve `/` regardless, refuse to mount `/admin` on a non-loopback bind unless auth is configured. Same rule, same reasoning, one implementation to copy.

### 6.2 A harvest must become a job

Every current trigger is fire-and-forget: the timer, `POST /refresh`, `harvest_events`. `SiteState` holds `refreshing: bool` and an `asyncio.Lock` — single-flight, which is right, but it means a second request is *dropped*, not queued, and there is no id to watch.

The run page needs: submit → id, poll or stream progress, see history, cancel. That is the job-dispatch shape, and it is worth noting the site and the MCP `harvest_events` tool should share it rather than growing two.

`known rough edge`: the existing lock is per-process. The MCP backend (`:8765`) and the site (`:8800`) are **separate processes**, so both can harvest simultaneously today — two browser fleets, two LLM bills, and a race on `data/.message_cache.json`. **This is a pre-existing bug that the run page will make easy to trigger**, not one it introduces.

Designed in full in **[`03 - The Job Queue.md`](03%20-%20The%20Job%20Queue.md)**: serial execution, FIFO with priority, the MCP backend as sole runner. Decided there, not here.

> **Correction, 2026-07-31.** An earlier draft of this section implied `interlock.py` already serialized browser work cross-process, on the strength of `00 - System Overview.md`'s concurrency table. It does not — `interlock.py` and `_interlock_worker.py` are the RyzenAI **NPU local-inference** path, and `llm.py:35` is their only consumer. There is no cross-process browser serialization at all. The overview has been corrected.

### 6.3 Framework

`/admin` is forms, a table, and a live job view. Reflex (roadmap **2.15**) fits, and this is the surface it was requested for — the *events grid* never needed it, being read-mostly over a fixed dataset.

The sequencing constraint is real though: **roadmap 2.2** — `ui/app.py` is 1456 lines with zero tests and mutates TickTick. `/admin` overlaps it heavily (profiles, caps, progress, stages, approve/decline). Building `/admin` while the Flet UI is untested means either duplicating untested behaviour or migrating on top of nothing. 2.2 first.

## 7. Explicitly out of scope

- **No second pipeline.** Axiom 1 says harvest→filter→extract→match lives only in `Pipeline`; `/admin` becomes a fourth caller, not a fourth implementation.
- **The grid stays static bytes.** No server-rendered event list at `/`. If it needs server state to render, it is no longer forwardable.
- **No approve/decline on `/admin` yet.** That is the Flet UI's job and moving it is 2.15, after 2.2.

## 8. Sequencing

| # | Step | Unblocks | Notes |
|---|---|---|---|
| 1 | Tags in `web_sources.json` + migration + `EH-STD-WEBSRC-v0` amendment | everything | Data-only, no UI. Testable today |
| 2 | Tag queries in profiles; `enabled` honoured in resolution | run page | Still CLI-visible: `--tag venues` |
| 3 | Auth on the site | `/admin` at all | Copy the gateway's posture |
| 4 | Job model shared by site + MCP | run page, jobs page | Includes the cross-process lock (§6.2) |
| 5 | Roadmap 2.2 — test `ui/app.py` | 2.15 | Pre-existing blocker |
| 6 | `/admin/sources` + `/admin/categories` | — | Read-then-write; lowest risk |
| 7 | `/admin/run` + `/admin/jobs` | — | Needs 2 and 4 |
| 8 | `/admin/sources/new` with screenshots and hints | — | Highest value, most design |

Steps 1 and 2 are worth doing regardless of whether the UI ever gets built: they make `category` mean something and give the CLI `--tag`, which is most of the practical benefit at a fraction of the cost.

## Open questions

1. **Auth mechanism for the site.** The gateway supports OAuth-proxy providers, static token and JWT. `/admin` is a browser surface, so a session cookie over one of those, or basic auth over the LAN? Not decided.
2. **Does `/admin` eventually absorb the Flet UI**, or do they coexist permanently? 2.15 implies absorption; nothing has decided it.
3. **Job persistence.** Survive a service restart, or in-memory and lost? In-memory is much simpler and a restart mid-harvest is already destructive.
4. **Tag limit per source.** None proposed. Unbounded tags make the run page's checkbox list unusable past ~20 distinct tags.

## Verification

Nothing to verify — no code. The claims about *current* behaviour above were read from the source and, where numbered, measured on 2026-07-31:

```powershell
pixi run event-harvester web list        # 30 sources: 24 category=venues, 6 with none
pixi run event-harvester --help          # no --tag flag exists yet
curl.exe http://127.0.0.1:8800/healthz   # refreshing/lock state, single-flight
```

Measured 2026-07-31: **30** sources, **24** carrying `category: "venues"`, **6** carrying none; **0** carrying `tags` or `enabled`. Full harvest wall-clock **320 s**. Stage list read from `ui/app.py:32`; cooperative stop points from `orchestrator.py:150,190,219,239`.
