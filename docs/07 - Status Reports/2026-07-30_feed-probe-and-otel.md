# Session Report — Feed probe + package-wide OTel

**Date**: 2026-07-30 · **Status**: ✅ LANDED
**Scope**: new `sources/feed_probe.py` + `web probe` command; OTel instrumentation across the package
**Tests**: 511 passed, 0 failed (was 482 — this pass added 29) · **Follows**: [2026-07-29_fix-pass-1.md](2026-07-29_fix-pass-1.md)

## 1. The brief, and how it was split

The ask was: add bar/venue sites quickly, and give complex sites (Meetup, Eventbrite, Luma) a breadcrumbed, agent-involved way to be navigated — with erobay named as a well-crystallised source.

**The reframe that drove everything else.** erobay is crystallised not because it is simple, but because *its exploration output fit in a URL field*:

```
https://erobay.com/calendar/Calcium40.pl?Op=ShowIt&CalendarName=Community
```

Someone explored that site once, and the answer was a URL — which `web_sources.json` can hold. Meetup/Eventbrite/Luma cannot be crystallised the same way because their content sits behind stateful interaction, so the exploration output is a *route*, and the schema has no field for one. A census of all 29 sources found 10 keys, every one a scalar; **there is no `steps`**. That, not the browser and not the LLM, is the actual gap. Evidence of the squeeze: `meetup` is configured with a single hardcoded `query: "AI meetup"` — one keyhole into a huge site, because one string is all the schema can carry.

The two halves of a solution already exist and don't touch each other: `web_primitives.py` (L1 verbs, with `_arbitrate` explicitly stubbed for a VLM) and `observability/navigation_trace.py` (a JSONL breadcrumb recorder with `step`/`parent` parentage — a traversal *tree*). The trace records browser events and is a debug artifact; the primitives are called by hardcoded `_do_*` modes. Connecting them — record L1 verb calls, distil into `steps[]`, replay deterministically, re-explore on failure — is Tier 4.4–4.6.

**Two decisions taken (both confirmed):** the agent runs at **authoring and repair time only**, never per-harvest; and the **feed probe ships first**, because it needs no agent at all.

## 2. What shipped: `sources/feed_probe.py`

Four discovery lanes in decreasing order of trustworthiness — `link-rel` (the standard), `embed` (a Google Calendar iframe *derives* the ICS URL from its `?src=` id), `anchor`, `platform` fingerprint — then blind probes of four conventional paths **only if the first four found nothing**.

**Validation is the filter, and that is the design.** Nothing is trusted for matching a pattern; every candidate is fetched and must parse as iCalendar with ≥1 VEVENT. That is what makes the speculative pattern table safe — a wrong guess costs one request and is discarded, so the table can be optimistic without lying to the caller.

New CLI: `web probe [URL…] [--apply] [--timeout]`, plus probe-first ordering in `web add`/`web test` (with `--no-probe` to skip).

## 3. Two things running it taught me that reading could not

**My hit-rate prediction was wrong.** I expected the platform fingerprint to convert "a third" of the 11 bare-homepage `raw` venues, reasoning from `powerhouse` (`?ical=1`, The Events Calendar) and `dnalounge` (`.ics`). Measured result across all 27 non-ICS sources: **1 hit** — `sf-eagle`, 27 upcoming events. SF gay bars mostly run Instagram or bespoke sites, not a calendar platform. The probe is cheap and correct; the segment simply is not WordPress-shaped. Tier 4.2 records that those venues need a different answer.

**The first live run produced a false positive that would have destroyed a working source.** `stud` resolved to
`/calendar/2026/7/30/140-olivia-lauren-amp-friends?format=ical` — a Squarespace *per-event* export. Valid iCalendar, one event. `--apply` would have replaced a working calendar source with a feed pinned to one night in July. The 15 unit tests passed before and after; none modelled "the anchor points at a single event". Two rules now reject these:

1. **Dated permalinks** (`/2026/7/30/`) — dropped at discovery, so they never cost a request.
2. **Undated opaque slugs** (`/calendar/h4wr7zbsakm3fye-…`) — nothing in the URL to match on. Dropped after validation when a candidate is both nested under the probed page's path *and* holds ≤1 event. A real `/calendar/all.ics` with several events survives.

Accepted cost: a venue whose only feed legitimately has one upcoming event reads as "none". Reporting nothing beats adopting a permanently-stale feed. `--apply` additionally refuses any feed with 0 upcoming events.

## 4. OTel: every boundary, not every call

Added `traced` (decorator) and `set_span_attrs` to `observability/tracing.py`, then applied them across the package. `traced` handles sync, async, and generator functions; records exceptions and **re-raises** them; and preserves signature metadata.

That last property is load-bearing, not cosmetic: FastMCP builds each MCP tool's JSON schema by introspecting the decorated function. A decorator that collapsed signatures to `(*args, **kwargs)` would silently strip every tool's parameters. Verified live — `scripts/mcp_probe.py` reports 20 tools, 2 resources, full parameter lists on all six harvest/review tools.

Newly covered: a `cli.{command}` **trace root** (before this, every span was its own orphan trace, so "what did this run do" was unanswerable); all four `pipeline.*` stages with per-stage counts and reject-bucket sizes; the four source readers; classifier and reranker (including the cross-encoder inference split out from the filtering around it); fingerprint load/save/dedup; all 16 L1 web verbs; all 8 MCP tools and resources; TickTick, reports, profiles, watch, training.

**Two things went from unobservable to observable:**

- **LLM token counts.** litellm logs nothing by default and the pipeline discards the response object, so per-run API cost was not recoverable from anything the app produced. Now on `llm.litellm.completion`.
- **TickTick creation failures.** `create_ticktick_tasks` caught a per-task exception, printed it, and moved on — the failure never reached `result`, so no caller could count it. Now `ticktick.failed`, plus a WARNING log line.

Deliberately **not** instrumented: per-element helpers inside tight loops (`score_message`, `_pattern_to_element`). A span per message would cost more than it explains and bury the spans that matter.

`web.verb.*` doubles as the substrate for Tier 4.5: every `find_*` funnels through `_arbitrate`, which now records candidate count, chosen selector, confidence, and whether confidence fell below `CONFIDENCE_THRESHOLD` — i.e. which step will break first when the page changes.

## 5. Mistakes made in this session

- **I broke MCP tool registration and caught it by probing, not by testing.** Two instrumentation scripts guarded import insertion with `if "_TRACER" not in txt` — but the decorators they had just inserted *reference* `_TRACER`, so the guard was already false and the import was never added. `mcp_server.py` then raised `NameError` at import. The unit tests passed anyway (they don't import that module), and `scripts/mcp_probe.py` was what actually failed. Fixed, and `tests/test_tracing.py::TestTracedDecorator` plus a full 69-module import check now exist to catch this class of error.
- **I started a real harvest by accident.** A script written to import every module for that check imported `__main__.py`, which *runs the CLI on import*. It connected to Telegram and scraped Instagram/Eventbrite/erobay/sfchamber for ~5 minutes before its timeout killed it. Nothing tracked was written — only gitignored runtime state (`data/.web_cooldowns.json`, the browser profile), and no TickTick writes (the run died before the sync stage). Those sources are now on cooldown until it lapses; `--no-cooldown` overrides. The script now skips `__main__` with a comment saying why.

## 6. State at handoff

| Fact | Value |
|---|---|
| Tests | 511 passed, 0 failed, ~11s |
| Modules | 69/69 import cleanly |
| Lint | 46 ruff errors, all pre-existing (down from 102 — `ruff --fix` cleared old import-ordering issues) |
| Git | **Uncommitted.** Source, tests, and docs all modified |
| Not mine | `.dvc/`, `.dvcignore`, six `*.dvc` files still **untracked** — do not commit without asking. Three long-running `event-harvester` python processes (started 7/28 and 7/29) are the user's, and were left alone |

**First action next session**: ~~`pixi run test` (expect 511)~~ — superseded by [2026-07-30_tier1-fix-pass.md](2026-07-30_tier1-fix-pass.md) later the same day (expect **542**). The recommendation still stands: **Tier 4.7** — spike the Eventbrite/Meetup APIs *before* building `steps[]`, since an API path could delete the hardest third of the navigation work. Numbers in this report are a snapshot; they are intentionally left as they were.

## Verification

```powershell
pixi run test                            # 511 passed
pixi run event-harvester web probe       # 1/27 (sf-eagle, 27 upcoming)
pixi run python scripts/mcp_probe.py     # 20 tools, 2 resources, full params on the first 6
pixi run event-harvester web probe --help
```

If `mcp_probe.py` reports `params=['args','kwargs']` for `approve_event`, `traced` has lost its signature-preserving behaviour and every MCP client has silently lost tool parameters — that is the failure this session actually hit, so it is the one worth re-checking.
