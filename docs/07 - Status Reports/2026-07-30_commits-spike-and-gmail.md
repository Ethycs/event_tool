# Session Report — Commit pass, the API spike, and a dark Gmail source

**Date**: 2026-07-30 · **Status**: ✅ LANDED
**Scope**: three commits off `ed3a4ee` · Tier 4.7 answered · Tier 4.3's config taken · Gmail found dark and fixed (new Tier 1b)
**Tests**: 549 passed, 0 failed (was 542) · **Follows**: [2026-07-30_tier1-fix-pass.md](2026-07-30_tier1-fix-pass.md) · **Detail on the spike**: [2026-07-30_api-spike-tier-4-7.md](2026-07-30_api-spike-tier-4-7.md)

## 1. The work got committed

Three fix passes had accumulated 92 uncommitted paths on top of `ed3a4ee "Docs Pass"` — the largest standing risk in the repo, since none of it had a restore point. Now on branch `fix/tier0-tier1-and-feed-probe`:

| Commit | Contents |
|---|---|
| `2aa8a33` | source + tests, 59 files — all of Tier 0/Tier 1, the side-channel refactor, feed probe, OTel |
| `2d58ded` | docs, 26 files — tree reconciliation, three status reports, README |
| `560771f` | the API spike, the roadmap update, the meetup config |

**Split by kind, not by pass.** The passes overlap at file level — `analysis.py` and `ticktick.py` were each touched by two of them — so a pass-boundary split would have needed hunk surgery and produced commits that do not individually pass tests. Code-then-docs was the clean cut. `main` still points at `ed3a4ee`; this fast-forwards.

`.dvc/`, `.dvcignore` and the six `*.dvc` pointers remain untracked, as they have been all along.

## 2. Tier 4.7: the APIs do not delete the navigation work

Full reasoning in the companion report. The short form:

- **Eventbrite — closed.** `GET /v3/events/search/` removed 2020-02-20. What remains retrieves by event, venue, or organization ID, each of which presupposes you already know the entity.
- **Luma — closed.** Paid Luma Plus required, *and* keys are scoped to one calendar you own.
- **Meetup — live, unresolved.** GraphQL `eventSearch` (ex-`keywordSearch`) is real discovery; whether it is entitled on a free account is contradicted across sources and needs an OAuth client to settle. Filed as 4.9.

**So Tier 4.4 is confirmed necessary rather than assumed** — which is the whole value of running the spike before building. One design consequence: Meetup is the only search-driven site of the three; Eventbrite and Luma are listing pages. If 4.9 moves Meetup to the API, `steps[]` never needs form-filling as a first-class step, so that decision should wait.

## 3. Tier 4.3's win had shipped but not been taken

Multi-query `search` mode landed earlier the same day, and `data/web_sources.json` still had `meetup` pinned to `"AI meetup"`. The capability was in the code and the config had not moved, so nothing changed in practice. Now five queries.

`unverified` — the five are inferred from the two lanes visible in the rest of the source list (tech/AI, and queer/community). A one-line edit to change.

## 4. Gmail was dark, and the cause was a credential-precedence bug

The question was "does the Gmail connector work, is it green now" — and checking rather than reasoning turned up a bug of the same class as everything in Tier 1: silent, and the run looked fine.

`_get_credentials` treated **"parsed successfully" as "claims the credential slot."** The env token was tried first, and the file fallback was guarded by `if creds is None` — so a `GMAIL_TOKEN_JSON` that merely parsed blocked `token.json` from ever being read. When that token's refresh came back `invalid_grant`, control fell straight past a working on-disk credential and into a fresh OAuth flow.

Measured on this checkout: the env token expired **2026-03-25**; `token.json` refreshed on the first attempt and reached the account. So a usable credential had been sitting one line away while Gmail returned nothing.

Two more defects fell out of looking:

- **The fallback was a hang, not a recovery.** `InstalledAppFlow.run_local_server()` opens a browser and blocks on human consent. Under the stdio MCP server or a scheduled harvest there is nobody to click. It now requires a TTY or an explicit `GMAIL_OAUTH_INTERACTIVE=1`; otherwise it logs what to do and returns `None`, so the source skips instead of wedging the run.
- **`--days-back` never reached Gmail.** `GmailConfig.query` defaulted to `"newer_than:7d"` and was ANDed with the `after:` filter derived from `--days-back`. `newer_than:7d` wins that AND, so **every run saw seven days no matter what was asked for.** The roadmap had this filed under 2.11 as *inert* config; it was not inert, it was actively overriding. Default is now empty, and `GMAIL_QUERY`/`GMAIL_MAX_RESULTS` populate it.

After the fix, against the real account: service built, profile reached, `fetch_messages(days=3)` returned **20 messages, 15 of them needing attention**. Green.

## 5. What is left for you

**`GMAIL_TOKEN_JSON` in `.env` is dead** and should be re-exported from the refreshed `token.json` or deleted outright. It is no longer fatal — that is what §4 fixed — but `_get_service` runs once per *operation*, not once per run, so every Gmail call now pays a failed network round-trip and logs a warning first. Filed as 1b.4. That file was not touched here.

## 6. State at handoff

| Fact | Value |
|---|---|
| Tests | 549 passed, 0 failed, ~12s (542 + 7 Gmail) |
| Branch | `fix/tier0-tier1-and-feed-probe`, 4 commits ahead of `main` at `ed3a4ee`. Working tree clean apart from the untracked `.dvc` pointers |
| Lint | 43, unchanged — the Gmail files add none |
| Roadmap | Tier 0 closed except 0.9 · Tier 1 closed · **Tier 1b new, 3 of 4 closed** · 2.1 closed · 2.11 partly closed · 4.1/4.3/4.7 closed |
| Sources | Gmail ✅ verified live · web feed probe 1/27 · Telegram/Discord/Signal unexercised this session |
| Not mine | `.dvc/`, `.dvcignore`, six `*.dvc` pointers — untracked. `.env` — not read, not edited. Three long-running `event-harvester` processes from 7/28–7/29 left alone |

**First action next session**: `pixi run test` (expect 549). Then either 4.9 (ten minutes, needs a Meetup OAuth client from you) or 2.2 (`ui/app.py`, 1456 lines, zero coverage, and the module that mutates TickTick).

**The method is the finding.** Gmail was not on any tier. It surfaced because someone asked whether a thing actually worked and it got checked against the real service instead of reasoned about. Every source other than Gmail and web is still in that unchecked state.

## Verification

```powershell
pixi run test                            # 549 passed
pixi run test tests/test_gmail_credentials.py -v   # 7 passed
git log --oneline -4                     # 3 commits on ed3a4ee
```

`unverified` — the 30-day window fix is pinned by a unit test and read-verified against `fetch_messages`, but **not** confirmed against the live API: Gmail's `resultSizeEstimate` saturated at 201 for both a 3-day and a 30-day query, so that comparison proved nothing either way. Telegram, Discord and Signal were not exercised at all; nothing here says anything about their state.
