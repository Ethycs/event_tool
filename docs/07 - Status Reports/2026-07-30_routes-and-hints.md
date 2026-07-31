# Session Report — Routes, an authoring agent surface, and live page diagnosis

**Date**: 2026-07-30 · **Status**: ✅ LANDED
**Scope**: Tier 4.4 + 4.5 (both closed) · 4.6 partial · new 4.10 · Gmail path docs
**Tests**: 620 passed, 0 failed (was 549 — this pass added 71) · **Lint**: 43, unchanged
**Follows**: [2026-07-30_commits-spike-and-gmail.md](2026-07-30_commits-spike-and-gmail.md)

## 1. The gap this closes

`EH-STD-WEBSRC-v0` had ten keys and every one was a scalar. It could hold a **destination**; it could not hold a **route**. That is the entire reason erobay looked "crystallised" while Luma, Meetup and Eventbrite did not — erobay's exploration output happened to fit in a URL field. The 4.7 spike then confirmed the awkward half: Eventbrite and Luma have **no API discovery path**, so navigating them is not a preference, it is the only option.

So: a schema that can carry a route, an engine that replays one, and a way to author one.

## 2. Opposite economics, opposite mechanisms

| | Authoring (`sources/web_explore.py`) | Replay (`sources/web_steps.py`) |
|---|---|---|
| Runs | once per site | every harvest, every source |
| Driver | an agent, over 9 MCP tools | `web_modes/steps.py` |
| LLM | yes — that is the point | **never**, unless a step fails |
| Output | a `steps` array | messages |

This split is the design. Paying for inference per source per run is how a scraper becomes too expensive to run daily; paying for it once per site is how a site gets added at all.

**Steps address roles, not selectors.** `{"verb": "fill", "target": "search"}` resolves through `web_primitives.find_search_box` *at replay time*, so a site that moves its own search box keeps working. `selector:<css>` exists as an escape hatch and is the brittle option on purpose — routes using it are the ones that rot first.

**Only successful steps are recorded.** Replay cannot distinguish an attempt from an event, so a route made of attempts would be a liability. The opening `goto` is recorded as step 0, which means a walk with no further actions crystallises into exactly the old schema shape — the erobay case falls out for free rather than being special-cased.

## 3. `web_primitives.py` is no longer dead code

Roadmap 2.4 listed it as one of four modules "a reader must understand to conclude they don't matter" — its only importer was its own test file. `web_steps.resolve_target` now dispatches five `find_*` verbs by role, and `_run_one` calls six action/extract verbs. The route engine is the production caller its docstring anticipated in the first place.

## 4. The hint system, and why the rules come first

`observability/hints.py` answers "why did this page not give us what we asked for" against an **open** page. (`nav_quality.py` answers it after the fact, from a trace on disk.)

One `page.evaluate` collects text length, link/input/dialog counts, a password-field flag, next-controls and scroll geometry. Pure rules rank that into `Hint`s, each carrying a `suggested_step` in the route vocabulary where one exists — a diagnosis you cannot act on is just a log line. A screenshot and one vision call happen **only** when the top rule sits at or below `VLM_ESCALATION_THRESHOLD = 0.6`, or when the caller asks outright.

When the picture and the DOM disagree, the picture wins — that asymmetry is the reason for taking it — but the rules' suggested step is kept underneath, because that is the actionable half.

## 5. Two bugs that only a live run could find

Unit tests were green. Both of these came from pointing it at `lu.ma/discover` — a real page, 51 event links, perfectly healthy.

**The rules called it `error` at 0.85 confidence.** `_ERROR_RE` contained a bare `\b404\b` and `\b500\b`, and the page says things like "500 attendees" and "1,404 going". The wrong answer was not the damage — **the confidence was**. 0.85 sits *above* the escalation threshold, so `auto` mode would never have taken the screenshot that disproves it. A confidently wrong rule is worse than an unsure one, because unsure rules self-correct by escalating. Status codes now require error wording attached.

**Then the fix exposed a cost bug.** With `error` gone the top hint became `needs_pagination` at 0.50 — *below* the threshold. Luma has a "load more" control, and so does most of the web, so every healthy paginated page would have bought a vision call. The fix was structural: structural *suggestions* (scroll, search, paginate) must not suppress a `content` verdict on a page that is plainly showing content. `content` now ranks first at 0.75 when the page has real text and ≥10 links, with suggestions kept beneath it.

Both are pinned by regression tests carrying the measured numbers. Re-run against the live page afterwards: rules say `content` at 75% with no vision call, and when forced, the VLM independently agrees — `content`.

**This is the third time in three sessions that a live run caught something the suite could not** (`stud`'s per-event feed, the MCP `NameError`, now these two). The pattern is consistent enough to be worth stating plainly: unit tests here pin the shapes I already thought of.

## 6. What shipped

| Area | Detail |
|---|---|
| `sources/web_steps.py` | 7 verbs, role-addressed targets, `validate_steps` (browser-free, rejects unexpected keys so a typo fails loudly), `replay()` with partial-result retention |
| `sources/web_explore.py` | Session registry, `MAX_SESSIONS=3`, 15-min idle reaping, `crystallize`/`save_source` (refuses silent overwrite) |
| `sources/web_modes/steps.py` | The 7th mode. Never auto-detected — a route only runs when someone configured one |
| `observability/hints.py` | Rules + gated VLM escalation, `Diagnosis.summary()` for one-line reads |
| MCP | 20 → **29 tools**. Parameter schemas verified intact through `traced` |
| CLI | `web replay NAME [--diagnose]` — the debugging surface for `mode: steps` |
| Schema | `steps`, `expect_min_events`, `mode: "steps"` — spec in `EH-STD-WEBSRC-v0 §3.2a` |
| Gmail docs | The connector comparison and the credential-precedence fix written into `03 - Architecture/02`; new roadmap 2.12 (dead `delete`/`mark_*`) and 2.13 (`_extract_body` loses HTML and attachments) |

## 7. State at handoff

| Fact | Value |
|---|---|
| Tests | 620 passed, 0 failed, ~12s |
| Lint | 43 — unchanged. 19 new errors were introduced and all 19 fixed |
| Roadmap | Tier 4.4 ✅ · 4.5 ✅ · 4.6 🟡 · 4.7 ✅ · 4.10 ✅ · 2.4 partly closed |
| Git | Branch `fix/tier0-tier1-and-feed-probe`, 4 commits ahead of `main` at `ed3a4ee`; this work is a 5th |
| Not mine | `.dvc/` and six `*.dvc` pointers still untracked. `.env` not read or edited |

**First action next session**: `pixi run test` (expect 620). Then the honest next step is **using** this — walk Meetup or Eventbrite with `web_explore_*` and save a route. Everything here is proven on one page; no route has yet been authored end to end and replayed from config.

## Verification

```powershell
pixi run test                                    # 620 passed
pixi run event-harvester web replay --help
pixi run python scripts/mcp_probe.py             # 29 tools, 2 resources
```

`unverified` — **no route has been authored through the MCP tools and replayed from `web_sources.json` end to end.** The engine was exercised against a live page with a hand-written route (dismiss → scroll → extract on `lu.ma/discover`: 51 links, all steps green), and every unit is tested, but `web_explore_open` itself is covered by nothing automated because covering it means launching Chromium. The seam between "agent saves a route" and "harvest replays it" is the part still taken on trust.

`unverified` — `expect_min_events` warns from a hand-set threshold; nothing records per-source history, so the replay-rot tripwire (4.6) is half-built.
