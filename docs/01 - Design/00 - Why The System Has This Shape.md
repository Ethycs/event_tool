# Why the System Has This Shape

**Status:** CURRENT · **Date:** 2026-07-29 · **Applies to:** whole system
**Depends on:** `docs/03 - Architecture/00 - System Overview.md`

The rationale behind the seven decisions that determine everything else. This is the *why*; the as-built *what* is in `03 - Architecture/`, and the *rules* are in `05 - Standards/`. Where a decision has since been contradicted by reality, that is said here rather than hidden.

## 1. One pipeline class, three control surfaces

**Decision.** `pipeline/orchestrator.py:Pipeline` owns harvest → filter → extract → match. The CLI, the Flet UI, and the MCP server call it; they do not reimplement it.

**Why.** They *did* reimplement it. Three entry points each carried ~100 lines of the same sequence and drifted from each other, so a fix to one silently left the other two wrong. The class exists to make that class of bug impossible rather than to be elegant.

**What each caller legitimately keeps** (this is real divergence, not duplication):

| Caller | Keeps | Because |
|---|---|---|
| CLI `harvest` | terminal printing, TickTick sync, report writing | one-shot batch, no session |
| Flet `serve` | `ProgressReporter`, the `quality_check` stage, `set_events()` | interactive, long-lived, cancellable |
| MCP | on-disk event cache, `include_known` filter, `public_event` projection | tools are stateless calls that index into a prior result |

**Cost accepted.** The reporter is threaded through as an optional Protocol, and every stage pays a `checkpoint()` call that is a no-op for two of the three callers.

## 2. Read the client, don't be the client

**Decision.** Discord is read from the desktop client's LevelDB cache; Signal from the local SQLCipher DB; Telegram via a user MTProto session; Gmail via API.

**Why.** No bot tokens, no server-side app registration, no per-guild install, and nothing that can be revoked by a platform noticing a bot. It also means the harvester sees exactly what the human sees.

**Cost accepted, and it is real.** Coverage equals whatever the desktop client has already fetched — a channel you have not opened recently has gaps, and no amount of harvester work fixes that. This is a *hard* limitation, not a bug to file.

## 3. Cheap filters before the expensive one

**Decision.** A regex gate, a trained classifier, a keyword heuristic, a cross-encoder reranker, and per-source caps all run before the LLM sees anything.

**Why.** Extraction is the only stage that costs money per message and the only one that is slow. Everything upstream exists to shrink its input. Caps in particular are not a quality mechanism — they stop one chatty source from consuming the whole budget.

**Cost accepted.** Five sequential filters mean five places a real event can be silently dropped. The mitigation is architectural: every drop lands in a named reject bucket, and all five buckets are rendered in the UI. **A filter that drops without recording is a bug**, regardless of how good its precision is.

**Known weakness.** The classifier fails *open* — if its model files cannot be loaded, every message is deemed actionable and only a log line says so. Fail-open was chosen so a broken model degrades cost rather than recall. Whether that is still the right trade is an open question (`06 - Roadmaps`).

## 4. Fingerprints, not IDs

**Decision.** An event's identity is a fingerprint computed from its content (title/date/location/link overlap), not a stable ID from a source system.

**Why.** The same event genuinely arrives as a Discord message, a Luma page, and a forwarded email. None of those carry a shared identifier. Content-derived identity is the only thing that can join them, and it is also what lets "I already declined this" survive across runs.

**Cost accepted.** The match relation is a hand-tuned 4-signal, threshold-2 rule. It is not transitive, and dedup is greedy first-match — so cluster behavior depends on insertion order. That is a known structural weakness, not a subtlety of the implementation. See `00 - Theory/00 - Ranking, Gating and Event Identity.md`.

## 5. A real browser, because the alternatives were tried and failed

**Decision.** Web sources are fetched through Playwright driving a real Chromium with the Universal Controller extension, with a separate native-Chrome path for Cloudflare-gated sites.

**Why.** Not preference — elimination. Eleven browser configurations were tried and the outcome of each is recorded with its lesson in `08 - Analysis/00 - Browser Configuration Attempt Log.md`. Live Chrome cookie extraction is closed (app-bound encryption since Chrome v130). Cloning a live profile is closed (SQLite locks). Playwright's default launch args are incompatible with sideloaded extensions. What survived is: launch the browser ourselves, connect over CDP on a port, and never pass `--no-sandbox` — that single flag is what Google's OAuth uses to detect automation.

**Rule that follows.** Before proposing a new browser configuration, read the attempt log. Re-litigating BC-002 through BC-009 is the most expensive mistake available in this repo.

## 6. Cache first, and make staleness visible

**Decision.** Harvest results cache to `data/.message_cache.json` for 4 hours, keyed on the active source set.

**Why.** Iterating on the pipeline should not re-scrape the web or re-hit Gmail. Keying on the source set (not just time) means `--only web` cannot be served a cache built from all five sources.

**Scar tissue.** The cache path was anchored by counting `.parent` calls and landed in a stray `src/data/`. Two silent failures followed: the "delete the cache to refresh" hint pointed at a nonexistent file, and a per-source harvest returned a different source's messages while looking successful. Fixed at `pipeline/harvest.py:40`; the *pattern* of counting parents is what to avoid, and it bit the classifier path the same way.

## 7. Debug artifacts are opt-in and layered

**Decision.** Three independent flags — `--debug-web-trace`, `--debug-web-screenshots`, `--debug-otel` — with screenshots implying traces.

**Why.** The web path fails in ways that are invisible from a message count: a login wall, a Cloudflare interstitial, and an empty page all produce "0 events". Screenshots plus a VLM verdict turn that into an attributable cause. Making it opt-in keeps the default run cheap and keeps image spend behind an explicit flag and a per-run image budget.

**Known gap.** The `quality_check` stage that produces those verdicts only runs from `serve`. `harvest` and `watch` can write screenshots but never score them. `known rough edge`

## Verification

```powershell
pixi run event-harvester --days 1 --dry-run --only discord
```

If that run re-fetches instead of using a cache built moments earlier from a different source set, decision 6 is working as designed. If it re-fetches when the source set is identical and under 4h old, the cache key or the path anchor has regressed — check `pipeline/harvest.py:40` first.
