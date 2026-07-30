**Standard ID:** EH-STD-EVENT-v0 v0
**Title:** Event Record
**Status:** Normative (v0, DRAFT — derived from as-built code 2026-07-29)
**Applies to:** the event-candidate `dict` produced by `src/event_harvester/pipeline/analysis.py:_parse_event_ini` (LLM extraction output), tagged with `_status` by `pipeline/orchestrator.py:Pipeline._status_for` / `servers/mcp_server.py:_event_status`, deduplicated by `pipeline/event_match.py` / `pipeline/dedup.py`, and consumed by `outputs/ticktick.py`, `outputs/report.py`, `outputs/obsidian.py`, `servers/mcp_server.py:_public_event`
**Depends on:** `EH-STD-MSG-v0` (the `source` field is a formatted back-reference to a message's `author`/`channel`)

## 0. Scope (what IS / IS NOT covered)

IS covered: the exact field set the LLM extraction prompt asks for and `_parse_event_ini` parses (`pipeline/analysis.py`), the `_status` lifecycle tag applied after fingerprint matching, the fingerprint record shape used for cross-run dedup (`pipeline/event_match.py`), and how each sink (`outputs/ticktick.py`, `outputs/report.py`, `outputs/obsidian.py`, `servers/mcp_server.py`) reads/re-shapes the record.

IS NOT covered: the message record consumed as extraction input (see `EH-STD-MSG-v0`); the separate, differently-shaped "event" dicts produced by `pipeline/weights.py:extract_events` (regex/heuristic date-scoring, fields `content`/`dates`/`times`/`resolved_dates`/`best_date`/`recency`/`proximity`/`scheduling`/`score`) — these are a *local heuristic pre-filter* record, not the LLM event candidate, but `outputs/report.py` and `outputs/obsidian.py` accept **either** shape interchangeably via `.get()` fallbacks (see §4, Non-conformance — dual event shape).

## 1. Normative Goals (numbered MUST / MUST NOT / SHOULD clauses)

1. The LLM extraction prompt (`analysis.py:_EVENT_EXTRACTION_PROMPT`) MUST request INI-formatted output, one `[Event.N]` section per event, with exactly the keys `title`, `date`, `time`, `location`, `link`, `source`, `details` (read directly, `analysis.py:96-118`).
2. `_parse_event_ini` (`analysis.py:496`) MUST drop any section whose section name does not start with `event` (case-insensitive) and MUST drop any section with an empty/missing `title` (`analysis.py:513,517-518`).
3. `_parse_event_ini` MUST convert the literal string `"TBD"` to `None` for `date`, `time`, `location`, `link` (`analysis.py:542-545`) — `"TBD"` MUST NOT appear as a value in a parsed event record; absence is represented as `None`.
4. `_parse_event_ini` MUST drop events whose parsed `date` resolves (via `dateutil.parser.parse(..., fuzzy=True)`) to a date strictly before today, recording the drop in `_parse_event_ini._last_dropped` for the `--show-rejects` / `rejects["llm_past"]` path (`analysis.py:527-538`; consumed at `pipeline/orchestrator.py:221-223`).
5. Every parsed event record MUST carry the fixed default `"priority": 3` and `"due_in_days": None` (`analysis.py:563-564`) — the LLM is never asked for a priority; it is a static per-batch constant. `PRIORITY_LABEL` (`analysis.py:21`) maps `{0: "none", 1: "low", 3: "medium", 5: "high"}` — `3` is therefore always `"medium"` for freshly extracted events, and `0`/`1`/`5` only appear on records mutated later by `outputs/ticktick.py:update_task_details` (`priority` is upgraded, never downgraded, when merging into an existing TickTick task).
6. After LLM parsing, events MUST pass through `pipeline/event_match.py:dedup_events` (within-run structural dedup — title-word-overlap + date + location + exact-link scoring, `score >= 2` to collapse) before being returned from `extract_events_llm` (`analysis.py:428-437` for cloud batches, `:485-491` for local).
7. Every event MUST be tagged with an internal, underscore-prefixed `_status` field before being handed to a sink or cache. `_status` MUST be one of the literal strings `"new"`, `"approved"`, `"declined"` (see §3.2 vocabulary) — never `"auto"` (fingerprints may internally be `"auto"`, but `_status_for`/`_event_status` only surface `"approved"`/`"declined"`/`"new"`, per `event_match.py:_ACTED_STATUSES = {"approved", "declined"}`).
8. `_status` tagging MUST be idempotent: `Pipeline._status_for` and `mcp_server._event_status` both check `event.get("_status")` first and return it unchanged if already set, only computing via `find_acted_fingerprint` when absent (`orchestrator.py:255-258`, `mcp_server.py:76-82`) — the two implementations are independently maintained but MUST stay behaviorally identical per the `orchestrator.py` module docstring ("mirrors mcp_server._event_status").
9. `save_fingerprint` (`event_match.py:211`) MUST be called exactly at three action points — TickTick creation/update (`outputs/ticktick.py:164,207`, `status="auto"`... actually `status` defaults to `"auto"` only when unspecified; ticktick.py calls it with the default, i.e. implicitly `status="auto"`), MCP `approve_event` (`status="approved"`), and MCP `decline_event` (`status="declined"`) — see §4 for the resulting non-conformance this creates.
10. `outputs/ticktick.py:create_ticktick_tasks` MUST classify every incoming event into exactly one of `to_create` / `to_update` / `to_skip` via `dedup_events_against_ticktick` (`pipeline/dedup.py:35`), which MUST short-circuit to `to_skip` whenever `find_acted_fingerprint(event)` returns a match — i.e. events the user has already approved or declined once MUST NOT be re-offered for creation even if they reappear in a fresh harvest.
11. `servers/mcp_server.py:_public_event` MUST project the internal record down to a fixed 10-key public shape (§3.3) for any MCP client — internal-only keys (`_status`, `notes` vs `details` ambiguity, `priority` as a raw int) MUST NOT leak through except as explicitly re-mapped fields.

## 2. Core Concept

An **event record** starts life as one `[Event.N]` INI section parsed out of an LLM completion (one completion per batch of ≤10 messages — `analysis.py:_extract_events_cloud`/`_extract_events_local`). It is a flat dict with a fixed 9-key shape (`title`, `date`, `time`, `location`, `link`, `notes`, `source`, `priority`, `due_in_days`). Once extracted, it is deduplicated within the run, tagged with an internal `_status` field that reflects whether the user has already acted on a structurally-matching event in a prior run (via the on-disk fingerprint store, `data/.event_fingerprints.json`), and then handed unchanged to whichever sink the caller wants: a TickTick task, a Markdown report row, an Obsidian vault note, or an MCP tool response. Every sink independently re-derives display fields (`author`/`channel` by splitting the `source` string on `" in "`) rather than the record carrying them structurally — `source` is a free-text convenience string, not a normalized back-reference.

## 3. Specification (field tables, value vocabularies, examples)

### 3.1 Core event-candidate fields (as produced by `_parse_event_ini`)

| Field | Type | Required | Producer(s) | Consumer(s) | Meaning |
|---|---|---|---|---|---|
| `title` | str | yes (section dropped if empty) | `analysis.py:_parse_event_ini` | all sinks; `event_match._normalize_title` (fingerprint key) | Event name. |
| `date` | str \| None | no | same | `event_match._normalize_date` (fingerprint key), `outputs/ticktick.py` (`dueDate` derivation via `dateutil.parser.parse`), `outputs/report.py`/`obsidian.py` (`resolved_date`) | Free-text or ISO date string; `"TBD"` normalized to `None`. |
| `time` | str \| None | no | same | `outputs/ticktick.py:update_task_details` (merge-detection only — not parsed into `dueDate`), `outputs/report.py`/`obsidian.py` display | Free-text time string; `"TBD"` normalized to `None`. **Not itself combined with `date` for TickTick's `dueDate`** (see §4). |
| `location` | str \| None | no | same | `event_match._normalize_location` (fingerprint key), sinks | Free-text location; `"TBD"` normalized to `None`. |
| `link` | str \| None | no | same | `event_match.event_signature` (exact-match fingerprint key, weight 2), sinks | Registration/event-page URL; `"TBD"` normalized to `None`. |
| `notes` | str | always `""` at minimum | `analysis.py:547-561` (`" \| ".join([details, location, link])`, filtered to non-empty) | `outputs/ticktick.py` (`content`), `mcp_server._public_event` (`notes` fallback for `details`) | Synthesized free-text blob: LLM `details` + duplicated `location` + `link`. |
| `source` | str | yes (may be empty string) | `analysis.py:524` — the raw LLM `source` field, prompt-documented as `"@author in #channel"` | `mcp_server._events_for_report` (parsed via `.split(" in ")`), `outputs/report.py`/`obsidian.py` (`channel` display fallback) | Free-text provenance string; NOT a structured back-reference to the originating message. |
| `priority` | int | yes, always `3` at extraction | `analysis.py:563` (constant); mutated later by `outputs/ticktick.py:update_task_details:104-105` (kept-if-higher merge) | `outputs/ticktick.py` (TickTick task priority + `PRIORITY_LABEL` display), `mcp_server._public_event` | `PRIORITY_LABEL = {0: "none", 1: "low", 3: "medium", 5: "high"}`. |
| `due_in_days` | None | yes, always `None` | `analysis.py:564` (constant, never set otherwise) | none found in-repo (`inferred` unused downstream — no reader grep-matched anywhere) | Dead field — reserved but unpopulated. |
| `_status` | str | yes, added post-extraction | `pipeline/orchestrator.py:Pipeline._status_for`, `servers/mcp_server.py:_event_status` (mirror implementations) | `mcp_server._public_event` (`status`), `mcp_server.run_event_pipeline` (`include_known` filter, stats counts) | Lifecycle tag — see §3.2. |

### 3.2 `_status` value vocabulary

| Literal value | Meaning | Set when |
|---|---|---|
| `"new"` | No matching fingerprint with an acted status exists | `find_acted_fingerprint(event)` returns `None` (`orchestrator.py:259-262`, `mcp_server.py:79-82`) |
| `"approved"` | User called `approve_event` (MCP) in a prior run and the event structurally matches (title/date/location/link scoring, `event_match.events_match`, `score >= 2`) | Fingerprint store has an entry with `status: "approved"` |
| `"declined"` | User called `decline_event` (MCP) in a prior run and the event structurally matches | Fingerprint store has an entry with `status: "declined"` |

Note: the fingerprint store itself (`data/.event_fingerprints.json`, via `event_match.save_fingerprint`) also writes a third status literal, `"auto"` — used only for pre-fetch web-link dedup hints (`_filter_known_links` in `web_utils.py`) and for **every** TickTick create/update, since `outputs/ticktick.py:164,207` calls `save_fingerprint(event, ticktick_id)` without an explicit `status=` argument, defaulting to `"auto"`. `"auto"` entries are structurally excluded from `find_acted_fingerprint` (`_ACTED_STATUSES = frozenset({"approved", "declined"})`) so they never surface as an event's `_status` value — but see §4 for why this is a real behavioral gap, not just an implementation detail.

### 3.3 `mcp_server._public_event` projection (MCP client-facing shape)

| Field | Type | Source |
|---|---|---|
| `index` | int | position in the cached `events` list (`data/.mcp_events.json`) |
| `status` | str | `_event_status(event)` — see §3.2 |
| `title` | str \| None | passthrough |
| `date` | str \| None | passthrough |
| `time` | str \| None | passthrough |
| `location` | str \| None | passthrough |
| `link` | str \| None | passthrough |
| `source` | str \| None | passthrough |
| `notes` | str \| None | `event.get("notes") or event.get("details")` — `details` is a defensive fallback key that `_parse_event_ini` never actually sets (it only sets `notes`); `inferred` legacy compatibility shim |
| `priority` | int \| None | passthrough |

### 3.4 Fingerprint record (`data/.event_fingerprints.json`, `event_match.save_fingerprint`)

| Field | Type | Meaning |
|---|---|---|
| `title` | str | raw `event.get("title", "")` |
| `date` | str \| None | `_normalize_date(event.get("date"))` — normalized to ISO `YYYY-MM-DD` at write time |
| `time` | str \| None | raw passthrough, not normalized |
| `location` | str \| None | raw passthrough |
| `link` | str \| None | raw passthrough |
| `ticktick_id` | str \| None | TickTick task id, when created via that sink |
| `status` | str | `"auto"` \| `"approved"` \| `"declined"` |
| `created_at` | str (ISO date, `date.today().isoformat()`) | write timestamp, used for 30-day pruning of undated fingerprints |

Pruning (`event_match.load_fingerprints`, `event_match.py:164-208`): an entry is dropped if its `date` parses to a day strictly before today; else if `date` is unparseable and `created_at` is more than 30 days old; else kept.

### 3.5 Example (reconstructed from the extraction prompt's own example, `analysis.py:98-105`, run through `_parse_event_ini` and `_status`-tagged)

```json
{
  "title": "Hackathon Weekend",
  "date": "2026-08-02",
  "time": "09:00",
  "location": "San Francisco",
  "link": "https://lu.ma/hack",
  "notes": "$45k in prizes, Google DeepMind sponsoring | San Francisco | https://lu.ma/hack",
  "source": "@author in #channel",
  "priority": 3,
  "due_in_days": null,
  "_status": "new"
}
```

MCP public projection of the same record (`_public_event`, index 0):

```json
{
  "index": 0,
  "status": "new",
  "title": "Hackathon Weekend",
  "date": "2026-08-02",
  "time": "09:00",
  "location": "San Francisco",
  "link": "https://lu.ma/hack",
  "source": "@author in #channel",
  "notes": "$45k in prizes, Google DeepMind sponsoring | San Francisco | https://lu.ma/hack",
  "priority": 3
}
```

## 4. Non-conformance observed (as-built deviations, each with file:function)

- **Non-conformance — `notes`/`details` key ambiguity.** `pipeline/analysis.py:_parse_event_ini` (line 555) always writes the synthesized string under the key `notes`, never `details`. `servers/mcp_server.py:_public_event` (line 118) and `mcp_server._events_for_report` (line 98) both read `event.get("notes") or event.get("details")` — the `details` fallback is dead code for LLM-extracted records (no producer ever sets `details` on this shape) and only resolves for the *other* event shape (`pipeline/weights.py:extract_events`, which does set a `details`-shaped field indirectly via `content`). This confirms two different "event" dicts are treated as interchangeable by the sinks — see the next entry.

- **Non-conformance — dual event shape accepted by report/obsidian sinks.** `outputs/report.py:_append_event` and `outputs/obsidian.py:_append_obsidian_event` both do `title = ev.get("title") or ev.get("content", "Untitled")[:80]` and reference `ev.get("dates")`, `ev.get("times")`, `ev.get("best_date")`, `ev.get("scheduling")`, `ev.get("score")` — none of which `_parse_event_ini` ever sets (those are exclusively fields of `pipeline/weights.py:extract_events`'s heuristic record). This means `generate_report`/`write_events_report` are written to silently accept either the LLM event-candidate record (this standard) or the local heuristic-scored record as `raw_events`/`validated_events`, with no type tag distinguishing which shape a given list element is. Not a bug per se (both call sites pass one shape consistently in current call paths — `inferred`, not exhaustively traced across every caller) but a latent footgun: a caller that mixes the two lists would silently get `"Untitled"` / blank fields for the mismatched half.

- **Non-conformance — `time` is parsed for merge-detection but never combined into TickTick's `dueDate`.** `outputs/ticktick.py:create_ticktick_tasks` (lines 178-186) builds `due_date` from `dateutil.parser.parse(event["date"])` alone; `event["time"]` is never concatenated or passed to the parser, so a TickTick task's due timestamp is always midnight-anchored (`dt.strftime("%Y-%m-%dT%H:%M:%S+0000")` where `dt` has no time component unless `dateutil` happened to find one embedded in the `date` string itself). `time` is only ever read for the free-text merge-detection heuristic in `update_task_details` (line 77) and for display in `outputs/report.py`/`outputs/obsidian.py`.

- **Non-conformance — every TickTick sync writes an `"auto"`-status fingerprint, which is structurally invisible to the approve/decline lifecycle.** `outputs/ticktick.py:164,207` calls `save_fingerprint(event, ticktick_id)` with no `status=` argument (defaults to `"auto"`, `event_match.py:216`). Per §3.2, `"auto"` fingerprints are excluded from `find_acted_fingerprint`, so an event created via TickTick sync (CLI path, not MCP `approve_event`) does **not** cause its `_status` to read `"approved"` on a later run — it will read `"new"` again unless a *separate* MCP `approve_event` call is later made against the same structural match. This is a real cross-path inconsistency between the CLI/TickTick-direct flow and the MCP approve/decline flow: only the latter durably marks an event as user-acted-upon for the purposes of `_status` and the dedup-against-reoffering guarantee in Goal 10.

- **Non-conformance — `source` is unstructured provenance, not a validated back-reference.** The extraction prompt (`analysis.py:104,113`) instructs the LLM to write `source = @author in #channel`, and `mcp_server._events_for_report` parses it back out via a literal `.split(" in ")` (line 90-91) — if the LLM ever omits `" in "`, varies the format, or a channel name legitimately contains the substring `" in "`, both `author` and `channel` silently resolve to empty strings (`""`) rather than raising. `inferred` — not observed to fail in this pass, but the parse has no fallback/validation path.
