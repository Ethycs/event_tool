# Blueprint — re-running web sources fast

**Status:** CURRENT · **Date:** 2026-09-02 · **Applies to:** `event-harvester harvest`, `EH_WEB_CHECKPOINT`, `data/.web_checkpoints/`, `data/web_sources.json` · **Related:** `01 - Adding a Web Source.md`, `03 - Architecture/03 - Sources — Web.md`

You are iterating on **one** web source — its parser, its mode, or a pipeline
stage it exercises — and you do **not** want to re-run the whole list to see the
effect. A full harvest launches a browser per source, clears Cloudflare, and
spends LLM tokens on every platform; a loop of those is minutes each and, worse,
has side effects that bite (below). This blueprint is how to cut a change→result
loop to seconds, and the three traps that make people stop trusting the result.

## The one thing to internalise

**A scoped re-run is not free of side effects.** Two, every time, and both hit
this session on 2026-09-02:

- **It cools the source.** `fetch_web_sources` writes `data/.web_cooldowns.json`
  on every fetch, even under `--no-cooldown` (that flag skips the *check*, not
  the *write*). Re-run erobay ten times and it — and anything else you touched —
  is now "recently fetched".
- **The event cache accumulates (default on; `EH_ACCUMULATE=0` to disable).**
  A harvest no longer replaces the events wholesale — it unions its output with
  the prior cache's still-future events and expires only what has passed
  (`_save_latest_events`, `servers/mcp_server.py`). This is what stops the
  historical failure mode: a *later* cooldown-starved harvest (only the un-cooled
  sources eligible) used to overwrite the full file with a sparse one — measured
  once as **138 events → 4** after a boot harvest with 37 sources on cooldown.
  With accumulation the cooled sources' still-future events survive that run.

So: **iterate with the CLI/scripts below, never by bouncing the live `site`
service** — and expect to clear cooldowns before the *next* real full harvest.

---

## The three layers, cheapest first

Pick the lowest layer that exercises the thing you changed.

| Changed | Layer | Cost |
|---|---|---|
| Nothing yet — is this URL even the events page? | **0. Page probe** | one browser, no LLM |
| A **parser / mode** (`calendar.py`, a `_do_*`) | **1. Page checkpoint** (`EH_WEB_CHECKPOINT`) | zero network after one record |
| An **extraction stage** (rerank, dedup, date/link carry, prompt) | **2. Message checkpoint** (`--save`/`--load`) | zero fetch, LLM only |

### Layer 0 — one URL, no pipeline

```powershell
pixi run event-harvester web test https://<candidate>
```

Reports links-with-dates / raw-chars / feed. Use it before you touch code, to
know whether the page or your parser is at fault. *Prevents:* rewriting a parser
for a page that was never the events page (see `01 - Adding a Web Source.md`).

### Layer 1 — page checkpoint (parser/mode iteration)

Record the fetched page once, then replay it from disk with **no browser, no
Cloudflare, deterministic bytes**. This is the big win for `native_chrome`
sources — erobay's live fetch is a ~100 s Cloudflare dance; replay is **10.2 s**
(measured 2026-09-02), and the page can't shift under you as dates roll.

```powershell
# record once (does one real fetch, then snapshots each page it opens)
$env:EH_WEB_CHECKPOINT = "record"
pixi run event-harvester harvest --only web --web-source erobay --no-cooldown --dry-run

# then iterate on the parser as many times as you like — instant, offline
$env:EH_WEB_CHECKPOINT = "replay"
pixi run event-harvester harvest --only web --web-source erobay --no-cooldown --dry-run

$env:EH_WEB_CHECKPOINT = ""     # back to live
```

(Bash: prefix inline — `EH_WEB_CHECKPOINT=replay pixi run …`.)

- Snapshots live in `data/.web_checkpoints/<stem>.html` (the raw DOM the parsers
  read) plus a `.mhtml` archive. **Keyed by URL**, so a source that opens several
  pages (the Calcium month grids — current plus `calendar_months - 1` forward)
  snapshots each. Delete
  the file to force a re-record; re-record whenever the site's content should
  have changed.
- `replay` **falls through to a live fetch** for any URL with no snapshot, so a
  new page in your parser still works — you just re-record to capture it.

*Prevents:* burning a minute and a Cloudflare challenge on every one-line parser
tweak — and chasing a bug that moved because "today" changed between runs.

### Layer 2 — message checkpoint (extraction iteration)

If your change is *after* the fetch — reranker, caps, dedup, the date/link
carry, the extraction prompt — don't fetch at all. Harvest the messages once,
then replay them through the pipeline from JSON:

```powershell
# capture the messages once (fetch happens here)
pixi run event-harvester harvest --only web --web-source erobay --no-cooldown --save data\ero.json --dry-run

# re-run extraction over the SAME messages, no fetch, as often as you like
pixi run event-harvester harvest --load data\ero.json --dry-run --show-rejects
```

`--load` skips harvesting entirely and runs filter → rerank → extract on the
file. Pair it with a hand-trimmed JSON (a single day, a single suspect event) to
isolate a stage. *Prevents:* re-fetching 200 messages to test a change that only
touches how they're scored or deduped.

> **`--show-rejects` does not show dedup.** The classifier/reranker/caps/LLM
> reject buckets are printed; the within-run dedup that collapses events is
> **silent**. On 2026-09-02 a dedup bug ate five of six same-day erobay events
> with **0 rejects logged** — it was only found by replaying the identical
> message set 5× and watching the pre/post-dedup counts. If events vanish and
> the reject buckets are all zero, suspect dedup (`pipeline/event_match.py`),
> not recall.

---

## A single source, end to end

The one command that fetches exactly one source and runs it all the way to the
(unwritten) events, ignoring cooldown:

```powershell
pixi run event-harvester harvest --only web --web-source erobay --no-cooldown --dry-run --show-rejects
```

- `--only web` — don't touch Discord/Telegram/Gmail/Signal.
- `--web-source NAME` — one source (the name in `data/web_sources.json`).
- `--no-cooldown` — or a source you fetched five minutes ago is silently skipped.
- `--dry-run` — no TickTick write. It **does** spend LLM tokens; that's the point.

## A *set* of sources

`--web-source` takes a **list** — comma-separate it, repeat the flag, or both
(added 2026-09-03). A scoped run (any `--web-source`, or `--only`) also bypasses
per-source caps, so a targeted source can't be silently trimmed:

```powershell
pixi run event-harvester harvest --only web `
  --web-source erobay,luma --web-source ra-sf `
  --no-cooldown --dry-run --show-rejects
```

Names are matched against `data/web_sources.json` at fetch time; an unknown name
is skipped with a warning, not a parse error. Pair with `EH_WEB_CHECKPOINT=replay`
(Layer 1) to iterate on the set offline.

The Python API is still there for programmatic use — `fetch_web_sources(sources=[…])`
for the fetch layer, or `harvest_messages(cfg, web_sources=[…])` for the full
pipeline — but for an operator re-run the CLI flag above is the one to reach for.

*Prevents:* running the whole list (and cooling it) to compare three sources —
without dropping to a scratchpad Python script.

---

## `web replay` is a different tool — don't reach for it here

```powershell
pixi run event-harvester web replay <name>        # NOT the page checkpoint
```

`web replay` replays a source's recorded **navigation route** (the click/scroll
trace under `data/.web_traces/`) and reports each step — it's for debugging *how
the browser moved through* a JS site, with `--diagnose` to vision-model a block.
`EH_WEB_CHECKPOINT` replays the **page content** for parser/extraction work.
Different artifact, different question. Using `web replay` to iterate a parser
just re-drives the browser — the thing the checkpoint exists to avoid.

---

## Before you walk away — undo the cooldown residue

Every re-run you did cooled a source. The next real full harvest (or the site's
boot harvest) will skip everything you touched and can overwrite a good
`events.json` with a sparse one. Clear the ledger so the next harvest is whole:

```powershell
Copy-Item data\.web_cooldowns.json data\.web_cooldowns.bak.json
Set-Content data\.web_cooldowns.json "{}"
```

*Prevents:* the **138 → 4** wipe — a rich site replaced by a cooldown-starved
re-harvest because iteration left half the list "recently fetched".

---

## Verification

```powershell
# Layer 1 replay is offline: no Cloudflare prompt, ~10s, same message count as the record run
$env:EH_WEB_CHECKPOINT = "replay"; pixi run event-harvester harvest --only web --web-source erobay --no-cooldown --dry-run
ls data\.web_checkpoints\          # one .html (+ .mhtml) per URL the source opened
```

`verified 2026-09-02` — erobay iterated at Layer 1 in ~10 s/loop (vs a ~100 s
live Cloudflare fetch), and a dedup fix was validated at Layer 2 by replaying one
saved 6-message day 5× until the count was stable.
