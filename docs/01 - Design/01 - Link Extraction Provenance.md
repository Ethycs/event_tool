# Why link extraction records where a link came from

**Status:** CURRENT · **Date:** 2026-07-30 · **Applies to:** `ext/helpers/extract_event_links.js`, `src/event_harvester/sources/web_utils.py` (`LINK_STRATEGIES`, `region_key`, `_extract_event_links`), `tests/test_link_provenance.py`, `tests/test_link_extraction_dom.py` · **Related:** `00 - Theory/01 - Site Navigation as a Planning Problem.md` §3.1, roadmap 4.6 / 4.12 / 4.13

## The reported problem

On Meetup and Eventbrite the extractor returns promo carousel cards, "browse by category" tiles and neighbourhood links as if they were events. They reach the LLM, cost tokens, and sometimes reach the review queue.

## The diagnosis, which is sharper than "the heuristic is fuzzy"

`extract_event_links.js` has **five** strategies — dated card, image-wrapped card, JS-embedded data, repeated-child fallback, last-resort walk up five ancestors — of very different reliability. `js-embedded` reads the date and id out of the href itself; `near-date-fallback` accepts a link whose only qualification may be that a date appeared within five ancestors, or that the URL merely *looks* like a detail page.

All five pushed into one flat list of `{url, text, date_hint}`.

Two consequences, and the second is the real one:

1. **Nothing downstream could tell a near-certain link from a guess**, because the strategy that produced it was discarded at the point of production.
2. **A carousel card is structurally identical to an event card** under `findCard`, which defines a card as *"nearest ancestor with ≥2 same-tag siblings that contains a link"*. A promo tile is a repeated sibling, with a date, containing a link. It is not that the heuristic is imprecise — it is that the two things are the same shape, and the only thing that distinguishes them is **which region of the page they sit in**, which was never recorded.

So the failure is not primarily a bad heuristic. It is missing provenance: there was no trail to reinforce, no region to score, and nothing to learn on even if every downstream signal had been available.

## What was built

Every one of the five push sites now emits `strategy`, and every link carries a `region`:

```js
{url, text, date_hint, strategy: 'dated-card', region: {heading, landmark, depth, path}}
```

`region_key(link)` (Python side) canonicalises that to one string, **at mint time** rather than by clustering afterwards. That ordering is deliberate and is the same lesson recorded in the theory doc §5.2: `events_match` is symmetric but not transitive and `dedup_events` is greedy first-match, so post-hoc clustering is order-dependent — open as roadmap 2.6 and still unpinned by a test. For events, wrong clustering is a nuisance. For regions it would merge a carousel with a results list and silently poison everything learned from either.

### Why the heading is the primary key, and not class names

Meetup and Eventbrite ship build-hashed class names (`css-1x2y3z`). A signature keyed on them changes on every deploy, and — this is the dangerous part — a region whose key changed is indistinguishable from a region that stopped producing. It would read as decay and be trusted as evidence.

The nearest *preceding* heading — "Search results", "Popular near you", "Explore by neighborhood" — is human-authored, changes rarely, and is semantically exactly the distinction being learned.

## Two bugs found by measurement, not by reasoning

**A card's own title is not its region.** The first implementation called `querySelector('h1..h6')` on the card itself. An event card's title *is* an `<h3>`, so every link resolved to a region of one. Measured on `eventbrite.com/d/ca--san-francisco/events/`: **27 links, 27 regions.** Perfectly useless, and it looked like it worked.

**Peer cards leak their titles sideways.** After the first fix, the walk over previous siblings found the *previous card's* `<h3>`, so card two adopted card one's title and a list fragmented one card at a time. Previous siblings with the same tag as the current node are now skipped as peers, and only a heading that is a **direct child** of a sibling counts as a section label.

Both were invisible to unit tests and obvious within one live page load. That is now four times in this codebase that a live run caught what the suite could not, which is the argument for the fixture harness below.

## Measured result

On the same Eventbrite listing after both fixes: **17 links → 8 regions** (from 17 → 17). Grouping works, and that is the thing learning requires.

But the honest half: **only 1 of 8 groups got a heading.** The other 7 fell back to the structural path, and those seven differ *only in the final sibling index* — `div:1` through `div:7` — because Eventbrite renders each row as a sibling `<div>` with no heading near the cards.

That is a real limitation, on precisely the site that motivated the work:

- Heading coverage is good on well-structured pages and poor on the two hardest targets.
- A key of `l:section|div>div>div>div:1>div>div:6` **will** shift when Eventbrite reorders or A/B-tests its sections, which is the exact rot the heading key was chosen to avoid.

The conclusion this points to is that the next step is *not* more DOM heuristics. It is the cross-page invariance test — **chrome appears on every page; results appear on one** — which needs no heading, no learning, and only two page loads.

## Decisions

| Decision | Rationale |
|---|---|
| Record provenance now, learn from it later | Nothing can be reinforced that was not recorded. This is a prerequisite, and it is useless-free: a pure addition to the returned dict |
| Canonical key at mint time, never post-hoc clustering | Roadmap 2.6's order-dependence would be inherited, and for regions it is a correctness failure rather than a nuisance |
| Heading first, structural path as fallback | Class names are build-hashed; heading text is authored |
| Reinforce regions and URL templates, **never individual URLs** | Event URLs are one-shot. Per-URL learning is true and worthless |
| Model it as a per-source bandit, not literal ACO | ~3–8 regions per page and a shallow tree. Keep the two useful pheromone intuitions — evaporation (which *is* rot detection) and a floor (so a newly-added section is never locked out) — and drop the combinatorial machinery that does not apply |
| Never punish a region for an infrastructure failure | `_fetch_event_details_parallel` returns empty text on a timeout. "Fetched fine, yielded no event" is evidence; "the fetch died" is not |

## On JSPyBridge (considered, not adopted)

[JSPyBridge](https://github.com/extremeheat/JSPyBridge) was raised as a way to make the JS testable from Python. It would work, and the underlying need is real — this file's bugs were previously findable only against live sites.

Not adopted, because the capability already exists without a new dependency. **Playwright is already required**, and a real Chromium over fixture HTML is strictly more faithful than a Node DOM shim: jsdom and friends return zeros from `getBoundingClientRect`, and this codebase's visibility checks (`web_primitives.find_pagination`, `hints._SIGNALS_JS`) filter on exactly that — so a shim would pass tests that a browser fails. Adding Node to a pixi/Python toolchain also has standing cost for every contributor and every deploy.

`tests/test_link_extraction_dom.py` does it with what is already installed. One wrinkle worth writing down: `page.set_content()` leaves the page on `about:blank`, so relative hrefs do not resolve and the extractor's same-host check drops every link — the fixture is served at a fake origin via `page.route` interception instead. Nothing touches the network, and the file skips cleanly when no browser is installed.

JSPyBridge would be the right answer to a different question: running npm-ecosystem libraries inside Python, or sharing code in both directions. Neither is the need here.

## Explicitly not built

- **Any learning.** No region store, no reinforcement, no decay. Provenance is recorded and currently unread by anything except tests.
- **The cross-page chrome test.** Next, and the one that fixes the reported problem without waiting on accumulated runs.
- **Ranking by strategy.** `LINK_STRATEGIES` is ordered by trustworthiness and nothing consumes the order yet.

## Verification

```powershell
pixi run test tests/test_link_provenance.py -v        # 10 passed, no browser needed
pixi run test tests/test_link_extraction_dom.py -v    # 8 passed, needs a chromium
```

`unverified` — the claim that heading text is stable across deploys is reasoning from how these sites are built, not a measurement. Nothing has yet observed the same region across two Eventbrite deploys, because the store that would record it does not exist.

`inferred` — "17 links → 8 regions" is one page load on one day. Eventbrite's listing is personalised and A/B-tested; another load may partition differently.
