# Session Report — Tier 4.7: do the platform APIs delete the navigation work?

**Date**: 2026-07-30 · **Status**: ✅ ANSWERED (desk research; one item needs a credential to finish)
**Scope**: Eventbrite, Meetup, Luma — public API coverage vs. what browser navigation is being built to scrape
**Follows**: [2026-07-30_tier1-fix-pass.md](2026-07-30_tier1-fix-pass.md)

## 1. Why this ran before 4.4

Tier 4.4 designs a `steps[]` route schema and 4.5 builds an agent explorer to author routes into it. Both are expensive. The premise underneath them is that Meetup, Eventbrite and Luma *have* to be navigated. If any of the three exposes a public discovery API, that site never needs a route, and the schema should not be designed to carry it.

**The answer is no — the APIs do not delete the work.** Two of the three are definitively closed to discovery. One is live and unresolved.

## 2. Findings

| Platform | Discovery via API | Detail |
|---|---|---|
| **Eventbrite** | ❌ **Closed** | `GET /v3/events/search/` was removed on **2020-02-20**. What remains is retrieval, not search: by event ID, by venue ID, by organization ID — all of which presuppose you already know the entity. There is no way to ask "what is happening in San Francisco this week" |
| **Luma** | ❌ **Closed** | An API exists but is the wrong shape twice over: it requires a paid **Luma Plus** subscription, and keys are **scoped to a single calendar you own**. It manages your events and guests. `lu.ma/discover` is not reachable through it |
| **Meetup** | 🟡 **Live, unresolved** | GraphQL has a real discovery query. `keywordSearch` was **split into `groupSearch` and `eventSearch`**, so `eventSearch` is the current spelling. Auth is OAuth2 bearer tokens. **What is not settled is entitlement** — secondary sources contradict each other on whether event search needs a paid Meetup Pro organizer subscription or works on a free account with `basic` scope |

## 3. What this means for 4.4 / 4.5

**Build `steps[]`.** The hypothesis that an API path could delete the hardest third of the navigation work is falsified for Eventbrite and Luma, which were two of the three hardest sites. They can only be reached by navigating them.

**But the shape of what `steps[]` must express may be narrower than assumed.** Meetup is the search-driven site of the three — type into a box, submit, read results. Eventbrite and Luma are *listing* pages: filter, paginate, scroll. If Meetup resolves to the API, `steps[]` never has to model form-filling as a first-class step, and the route vocabulary shrinks toward traversal and pagination. Worth knowing before the schema is fixed, which is why this is the one open thread that matters.

## 4. The one thing desk research cannot settle

Whether `eventSearch` is entitled on a free account is an empirical question, and answering it requires **registering an OAuth client on a Meetup account** — a credential action that is the user's to take, not mine. That is where this spike stops.

The test, once a token exists, is one request: an `eventSearch` for San Francisco within a radius, and see whether it returns events or an entitlement error. Ten minutes. If it returns events, `meetup` becomes an API source and drops out of the browser path entirely — which is worth ten minutes given it is currently one of the slowest sources in a harvest (five queries × a page load and a 10s scroll each, as of the config change below).

## 5. Also landed this session

- **Two commits** on branch `fix/tier0-tier1-and-feed-probe`, on top of `ed3a4ee`: the source/test changes from all three fix passes (59 files), then the doc reconciliation (26 files). Split by kind rather than by pass because the passes overlap at file level — `analysis.py` and `ticktick.py` were each touched by two of them, so a pass-boundary split would need hunk surgery and would produce commits that do not individually pass tests. `.dvc/`, `.dvcignore` and the six `*.dvc` pointers remain **untracked and uncommitted**.
- **`data/web_sources.json`**: `meetup` moved from `"query": "AI meetup"` to five `queries`. Tier 4.3 shipped the capability on 2026-07-30 but left the config at one string, so the coverage win had not actually been taken. The five are inferred from the two lanes visible in the rest of the source list (tech/AI, and queer/community) — `unverified` as a statement of intent, and a one-line edit to change.

## Verification

```powershell
pixi run test                                     # 542 passed
pixi run python -c "import json; print(len(json.load(open('data/web_sources.json'))))"   # 29
git log --oneline -3                              # 2 new commits on ed3a4ee
```

`unverified` — every claim in §2 is desk research against vendor documentation and secondary sources, not against a live API call. No request was made to any of the three platforms. The Eventbrite and Luma findings are from primary sources (the platform changelog and `docs.luma.com`) and are firm; the Meetup entitlement question is exactly the part that secondary sources disagree on, which is why §4 exists rather than a conclusion.
