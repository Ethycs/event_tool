# Session Report — Asking Instagram for the posts instead of waiting for them

**Date**: 2026-08-06 · **Status**: 🟡 LANDED, one half unverified
**Scope**: `sources/instagram.py` (new) · `web_modes/instagram.py` (new mode) · `web_modes/feed.py` walker replaced · `web instagram` CLI · `data/web_sources.json` instagram entry rewritten
**Tests**: 989 passed, 0 failed (was 938) · **Lint**: 43, unchanged
**Follows**: [2026-07-30_feed-probe-and-otel.md](2026-07-30_feed-probe-and-otel.md) — this is the other half of its 1-of-27 result

## 1. The problem the last session left

`feed_probe` converted **1 of 27** sources to a calendar feed. Its own report named why: SF gay bars do not run WordPress + The Events Calendar, they run an Instagram account. Roadmap 4.2 recorded that as "needs a judgment call, not code."

Meanwhile the Instagram source that already existed could not serve those venues either, and the reason is worth stating precisely: **it read the home feed**. Open `instagram.com`, scroll for 25 seconds, parse whatever GraphQL happened to fly past. Meta ranks that timeline, so "did we see the Oasis lineup this week" had no answer — only a probability, redrawn every run.

## 2. What changed: name the accounts, and replay the query

`mode: "instagram"` walks a configured `accounts` list, one profile page each. The profile load itself carries ~12 posts; further pages come from replaying Instagram's **own persisted GraphQL query** with a patched cursor.

Nothing about that query is hardcoded. `QueryRecorder` attaches before navigation and records the requests the real client issues — `doc_id`, `variables`, and the dozen session parameters (`lsd`, `jazoest`, `__csr`, `av`) nobody wants to reverse-engineer. `with_cursor` then reuses that body **verbatim** and changes exactly one field, `variables.after`, which is what the page itself does when you scroll.

**That is the whole design, and it is a bet about maintenance rather than about cleverness.** Every hand-rolled Instagram client dies the day Meta rotates a `doc_id`. Here a rotation costs one run's staleness: the next run records the new query. The replay goes through `fetch(..., {credentials: "include"})` *inside the page*, so cookies, TLS fingerprint and origin belong to the session that was already open — a request that looks like it came from somewhere else is a request Instagram is entitled to refuse.

## 3. The bug that was worth the whole session

The old walker keyed on **container** shape — `edges` and `items` — and recursed only through dict values. **It never walked lists.**

A payload nesting posts one array deeper returned `[]`. Not an error, not a warning: an empty list, which downstream is indistinguishable from a venue that posted nothing that week. The deferred timeline is exactly such a payload.

The replacement keys on the **object**: a dict carrying a caption and an id is a post, wherever it sits. That is also what makes it survive Instagram's routine renaming of connection fields (`edge_owner_to_timeline_media` → `xdt_api__v1__feed__user_timeline_graphql_connection` — both are in the test file, both work, and neither name appears in the walker).

Two more silent losses fixed in the same pass, both inherited by `feed` mode:

- **Multi-chunk bodies.** Deferred GraphQL arrives as newline-delimited JSON objects. `json.loads` raised, the caller caught `JSONDecodeError` and dropped the response — so the bodies carrying the deferred timeline were precisely the ones discarded. `loads_multi` falls back to line-by-line.
- **No link back.** Posts had no permalink, so an approved Instagram event was unclickable. `content` now leads with `https://www.instagram.com/p/<code>/`. Leading, not trailing: `build_prompt` truncates content to 400 chars, so a link after a long caption is a link the LLM never sees.

## 4. Discovery: the venues already know their own handles

Guessing handles was never an option. Oasis is **`@theoasissf`**; the obvious guess, `@sfoasis`, is someone else. A wrong handle harvests a stranger's posts forever and never errors.

So `web instagram` reads them off the venues — one HTTP request per site, no browser, reusing `feed_probe`'s fetcher. **Measured 2026-08-06: 17 of 23** venue sites name their account clearly enough to adopt, against feed discovery's 1 of 27 on the same segment.

**The structural difference from `feed_probe` is that validation is impossible here, and that changed the design.** A feed candidate is proven by fetching and parsing it — which is what lets that module's pattern table be optimistic. A handle cannot be: checked today, logged out, `instagram.com/<anything>/` returns **200 with an identical JS shell** for a real account and for `zzzznotarealhandle999` alike. Ranking has to carry the weight validation carries there.

**Frequency would have been the obvious ranking, and it is wrong.** `beauxsf.com` links `@beauxdragbrunch` **18 times** and its own `@lesbeauxsf` twice. Ranking by count adopts the party; ranking by *affinity* — the longest run of characters shared with the venue's name or domain — adopts the venue. `MIN_AFFINITY = 5` is set by that case: "beaux" is exactly five characters, and nothing shorter separated a venue from a promoter it links to.

`--apply` adopts confident matches only and prints the rest as `unsure`, which is the same "report nothing rather than adopt something wrong" rule `feed_probe --apply` uses for 0-event feeds.

## 5. The cap that would have hidden fifteen venues

`max_events` is 30 per source. The account list is 16. Concatenate-and-truncate would spend the whole budget on the first two accounts **in config order** — so the same two venues would be the only ones ever seen, and the other fourteen would look like they never post. Nothing would error; the report would just be wrong in a way that reads as a quiet week at fourteen bars.

`_interleave` takes every venue's newest post before any venue's second. Follow-on decision: the shipped config sets `max_pages: 0`, because a profile load already carries ~12 posts and the trim keeps ~2 — a replayed page would be fetched and discarded. **Pagination earns its keep on a narrow account list**, which is what the knob is for, not on a 16-venue sweep.

## 6. What is not verified

**The live replay.** Every other part is exercised — recording, the walker, cursor tracking, the interleave, discovery (against 23 live sites), and dispatch — but no run has issued a real replayed `fetch`, because that needs a logged-in session this environment does not have. It degrades to the recorded first page on a non-200, a non-JSON body, or a missing cursor, and `tests/test_instagram.py` pins that degradation with a fake page. Roadmap **4.15** records what a first live run should check: a 200 rather than a checkpoint, a cursor that advances, and a second page holding posts the first did not.

That the *default* config sets `max_pages: 0` is worth saying plainly: on the shipped venue sweep, the replay path does not run at all. It is there for the narrow case and for anyone who raises `WEB_MAX_EVENTS`.

## 7. State at handoff

| Fact | Value |
|---|---|
| Tests | 989 passed (was 938); 51 new in `tests/test_instagram.py` |
| Lint | 43 errors, all pre-existing (unchanged by this session) |
| Accounts configured | 16 venues, every one read off the venue's own site |
| Venue sites with no discoverable handle | `hi-tops`, `badlands`, `the-edge`, `wild-side-west`, `mothership`, `powerhouse` |
| Removed by hand after `--apply` | `@qcal.app` — an aggregator's product account, not a venue |
| Next | `web login`, then one supervised `--web-source instagram --no-cooldown` run (roadmap 4.15) |
