# Session Report — email extraction, two discovery modes, and the `%` that ate every batch

**Date**: 2026-08-12 · **Status**: ✅ LANDED, **nothing committed** (§8)
**Scope**: Gmail bodies + link expansion + labelling · `luma`/`partiful` modes · keyword lists + `/admin` · caps → unlimited · report nav, thumbnails, place filter, render strategy · **the INI parser bug**
**Tests**: 1306 passed, 0 failed (started at 1018) · **Lint**: 43, unchanged
**Follows**: [2026-08-07_queue-lifecycle-and-the-web-path.md](2026-08-07_queue-lifecycle-and-the-web-path.md)

---

## 1. The finding that mattered most

**`parse_llm_ini` was discarding every LLM reply containing a `%`.**

`configparser.ConfigParser()` treats `%` as substitution syntax. erobay's Calcium detail URLs are full of them, so every batch raised `InterpolationSyntaxError` and was thrown away — **121 messages in, zero events out, every run for weeks**. With `interpolation=None`: **erobay 0 → 54 events.**

It hid unusually well, for three separate reasons:

* `build_prompt` already escapes `%` on the way **in**, so `%` looked like a solved problem. Nothing escaped the model's reply on the way **out**.
* Interpolation fires on **value access**, not on parse. `read_string` inside the `try` succeeded; the exception came from the `dict(...)` comprehension one line *below* it, sailing past the `except configparser.Error` written to catch exactly this.
* The function had **no tests at all**. It has 23 now.

**This was never erobay-specific.** Any event with `%20` in a URL or "50% off" in a title took nine innocent events down with it, on every source, silently.

---

## 2. Chasing it also exposed

| | |
|---|---|
| **The `default` pixi env had drifted** — missing `ccl_chromium_reader`. A deadlock: the running service held `event-harvester.exe`, so `pixi install` could never sync, so the MCP backend ran stale code indefinitely. Stop → sync → start |
| **Caps were starving whole sources.** Every web sub-source shares one `web` bucket, so luma's 60 messages consumed all 30 slots and erobay never reached the LLM even before the parser bug. Reproduced exactly: `web=30` → `{luma: 30}`, unlimited → `{luma: 60, erobay: 121}` |
| **`--cap web=-1` was rejected** by the CLI validator, contradicting the new default |
| **`_events_for_report` stripped `_platforms`**, and the `source` parser required `" in "` which the model often omits — **148 of 240 events had no source chip** |

---

## 3. Caps now default to unlimited

**Decided by the operator 2026-08-11: discard at the view, not in the pipeline.** Recorded as axiom 8 in the System Overview. Nothing downstream can show an event dropped before extraction; the page can filter, search and sort whatever it is given.

* `CapConfig` defaults to `-1` everywhere. `0` still means "drop this source" — distinct and useful.
* **Any negative normalises to `-1`, never `0`.** `prioritize_with_caps` always tested `cap < 0`, so `-5` already meant unlimited; clamping a typo to "discard everything from this source" is the worst available reading.
* **The CLI keeps the old ceilings** (`apply_cli_defaults`), and only where nothing else set them. A scheduled harvest feeding the site should collect everything; a person typing a command wants a bounded bill.
* The cost moved to the LLM stage, so `prioritize_with_caps` now logs the count and warns above 1000.

**Result: 240 events, up from 33.**

---

## 4. What exists now that did not

| | |
|---|---|
| Gmail bodies | `hydrate_bodies` — the pipeline was reading **~8% of every email** (200-char snippet cap vs 2505-char median body) |
| Gmail link expansion | `expand_event_links` — schema.org from the linked page. **12 of 14 answered**; every event gained a date *and* time |
| Gmail labelling | `label_event_messages` + `_source_ids` (a new `EH-STD-EVENT-v0 §3.2a` back-reference). Live: 12 messages tagged `events` |
| `web_modes/luma.py` | Keyless discovery API + cursor pagination. **1 event → 13** |
| `web_modes/partiful.py` | Server-rendered Next.js props, city-bucketed |
| `keywords.py` + `data/keywords.json` | Operator-owned lists, per-platform strength (`native`/`category`/`local`) |
| `servers/admin.py` | `/admin`: caps equalizer with ∞ toggles, keyword editor, search-plan table |
| `outputs/thumbnails.py` | Inline `data:` URI previews. **20 of 22 linked events** |
| `outputs/regions.py` | SF / Oakland / Broader / Unplaced filter |
| `sources/venue_location.py` | **Written, NOT wired** — see §7 |

---

## 5. Measured, so the next reader does not re-derive it

| | |
|---|---|
| Gmail, same 63-message window | snippets **8 events** → bodies **11** → bodies + read mail kept **21** |
| Gmail reranker "no date/link" drops | 61% → 26% once bodies arrived |
| Read mail | was **42% of the mailbox**, discarded. It was the *larger* of the two Gmail effects |
| Luma | 1 message → 1 event, now 60 → 13 |
| erobay | 121 messages → **0 events**, now → **54** |
| Full harvest | **240 events** (33 the day before) |
| Report page | 240 events, 95 thumbnails, 1.5 MB |

---

## 6. Decisions worth not re-litigating

* **Content order in a Gmail message is normative.** `build_prompt` truncates to 400 chars *and* derives its date hints from that same slice, so `content` is assembled facts-first: subject → schema.org `EVENT:` line → links → date-decimated body. A caller that reorders these silently degrades extraction without failing.
* **Local keyword matching tags, never filters.** Caps already discarded ~98%; filtering on top would narrow the funnel twice.
* **Thumbnails are embedded, not linked.** A remote `<img>` would fail `test_no_external_resources` *and* phone home from every forwarded copy.
* **`Unplaced` is its own region**, never folded into `Broader`. "Could not tell" and "definitely elsewhere" are different answers.
* **Chips are platforms, not senders.** 22 chips for 33 events is not a filter.
* **The origin needs no auth** — the services run inside an authenticated ZeroTier mesh. Settled; stop re-raising it. Escalate only if a bind leaves the mesh.

---

## 7. Known rough edges, recorded not fixed

* **`sources/venue_location.py` is written but unwired.** Detects a venue's city at add-time (schema.org address → og tags → ZIP → phone area code; 415/628 → SF, 510 → East Bay) so events inherit it. Needs: a `city`/`region` field in `EH-STD-WEBSRC-v0`, a hook in `_web_add`, a backfill for the 33 existing sources, and `regions.classify` to accept it above the venue table. **This is the intended fix for the hand-maintained venue list.**
* **`Unplaced` is 66 of 240.** Partly because erobay puts `[SF]`/`[Oakland]` in the *title* while `location` is empty — `classify` only reads `location`.
* **`--web-source` is single-valued**; passing it twice silently keeps the last. `WebConfig.selected_names` is already a list, but widening the flag ripples through the job queue's request shape and coalescing.
* **The report is 1.5 MB and fully embedded.** Chunked rendering fixed the *freeze*, not the *download*.
* **Luma `/sf` is Bay Area-wide**, not SF — Fremont, Stanford, Santa Clara all appear.
* **60 Luma messages → 13 events** is a worse conversion than `rss` achieves (105 → 65). Undiagnosed.
* **`GMAIL_TOKEN_JSON` is still dead** — fails a refresh on every Gmail call before falling through to the working `token.json`.

---

## 8. State at handoff

| Fact | Value |
|---|---|
| Tests | **1306 passed**, 0 failed (1018 at session start) |
| Lint | 43 — unchanged all session |
| Git | `main` == `origin/main` at `ddf481a`. **The previous handoff's §1 is resolved** — `instagram.py` is in HEAD and everything was pushed |
| **Uncommitted** | **35 modified + 14 new files — all of this session's work.** `git add` was blocked by the permission classifier every time it was attempted |
| Services | `EventHarvesterMCP` + `EventHarvesterSite` RUNNING on current code, `default` env repaired |
| Cache | `data/.mcp_events.json` — 240 events incl. 54 erobay |
| Not mine | the 8 `.dvc` paths, still untracked and uncommitted |

**Nothing from 2026-08-12 is committed.** That is the first thing the next session should fix, and it needs a `git add` permission rule or the operator running it by hand.

---

## 9. Method note

Three times this session a confident conclusion about a *remote* system was wrong, and a two-minute local test settled it:

1. "Instagram's CDN needs a `Referer`" — it did not.
2. "Those URLs are session-signed and unreachable" — written into code and docs as fact. Also wrong: **our own `og:image` was never HTML-unescaped**, so `&amp;` renamed the signature parameters and the request really was unsigned. `html.unescape` fixed all 8.
3. "The job queue drops `no_cooldown`" — it does not; two log lines from different jobs had been conflated.

The pattern: theorising about a distant layer instead of instrumenting the seam in front of you. Printing the parsed query parameters, and separately asserting what reached `fetch_web_sources`, each took one command and ended the argument outright. **An error from a remote system describes your request at least as often as their policy.**

## Verification

```powershell
pixi run test                                   # 1306 passed
pixi run lint                                   # 43, all pre-existing
pixi run check-head                             # needs pixi activation for playwright
pixi run event-harvester service doctor         # 2 findings, both known
git status --short                              # 35 modified, 14 new — uncommitted
```

`unverified` — `venue_location.detect()` has been run against no live venue site; it is written, imported by nothing, and untested.
