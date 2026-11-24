# Nightly Harvest Health, 2026-11-17 to 2026-11-23

**Status:** CURRENT (one host, seven nights, read from logs and the message cache on 2026-11-24) · **Date:** 2026-11-24 · **Applies to:** `src/event_harvester/sources/web_fetch.py`, `web_utils.py`, `job_runner.py`, `data/web_sources.json` · **Depends on:** `docs/03 - Architecture/03 - Sources — Web.md`

## Verdict

1. Two of seven nightly harvests produced nothing. Both hung in one source and were discarded whole at the job's 90-minute limit.
2. `erobay` returned a Cloudflare challenge page as its only message on every night that finished.
3. `instagram` went from 30 messages to 0 and stayed there, with no error.
4. The nightly total did not move: 912 to 1021 messages on every night that finished. The total is not a health signal.
5. Fixed on 2026-11-24: a hung source now costs only its own browser group; a challenge page is never emitted; and a handler whose page is not the page it was written for reports a deviation and emits nothing. Not fixed: the `raw` sources, which have no expectation to deviate from, and telling a person that a source needs them.

## 1. Conditions

| | |
|---|---|
| Host | `smallblackbox`; harvests run in the system unit `event-harvester-mcp`, up since 2026-11-17 01:16 UTC |
| Evidence | `journalctl -u event-harvester-mcp`; `data/.message_cache.json` (web messages only); `http://127.0.0.1:9222/json/list` |
| Sources | 50 enabled; `WEB_PARALLEL=1`; four `native_chrome` sources (`erobay`, `ra-sf`, `midway`, `makerfaire`) |
| Code | the service process was running the code it loaded on 2026-11-17, before any change described in §4 |

## 2. Results

### Runs

| Start (UTC) | End | Web messages | Result |
|---|---|---|---|
| 11-17 01:16 | 02:08 | 1021 | done |
| 11-18 01:16 | 01:57 | 995 | done |
| 11-19 01:57 | 03:27 | — | **failed** at the 90-minute limit |
| 11-20 03:27 | 04:08 | 963 | done |
| 11-21 04:08 | 04:49 | 965 | done |
| 11-22 04:49 | 06:19 | — | **failed** at the 90-minute limit |
| 11-23 06:19 | 07:05 | 912 | done |

Each run from 11-19 on started about 24 hours after the previous one ended, so the start time moved from 01:16 to 06:19 in a week.

### The two failed runs

| Fact | Evidence |
|---|---|
| Both stopped in the same place | last native-group line on each night: `ra-sf: N event links to process.` (13 on 11-19, 16 on 11-22); `ra-sf: N messages.` never followed |
| Everything else had finished | on 11-22 the last standard source logged at 05:24:04; nothing was logged for 55 minutes until `-> failed` at 06:19:21 |
| The pages themselves had loaded | the Chrome on port 9222 still held `ra.co` listing and event tabs on 2026-11-24; each answered a `Runtime.evaluate` in 10–20 ms with `document.readyState == "complete"` and a real event title |
| Which call hung | **unknown.** `_fetch_event_details_parallel` logs nothing between tabs. The calls in it with no timeout are `context.new_page()`, `page.content()` and `page.close()` |

The whole run was lost because `fetch_web_sources` waited on every group with no deadline, and the job runner discards a job that exceeds its limit.

### Sources that looked alive and were not

| Source | Messages per night | What the message was |
|---|---|---|
| `erobay` | 1 | `Just a moment... erobay.com Performing security verification …` (374 characters). Log: `Cloudflare did not resolve in 15s. Skipping.` then `erobay: 1 messages.` |
| `instagram` | 30 on 11-17; 0 on 11-20, 11-21, 11-23 | nothing. Log, for each of 16 accounts: `0 post(s) from 0 recorded response(s)` and `produced no GraphQL at all`. No login wall was detected. Cause `unverified` |
| `midway` | 1 | page chrome beginning `RENTAL Event Photos Discover Contact FAQ … Seems JavaScript is Disabled` |
| `makerfaire` | 1 | navigation text |
| `badlands` | 1 | `Redirecting... The domain has expired` |

`erobay` is the source the Calcium parser, the month walk and most of `docs/03 - Architecture/03 - Sources — Web.md` were written for; that doc measured 198 upcoming events from it on 2026-11-05.

## 3. What the numbers mean

| Finding | Consequence |
|---|---|
| The total hid every failure above | Health has to be judged per source. A count of 1 from a `raw` source is normal and says nothing about whether the message is an event |
| One hung call discarded 49 finished sources | The unit of loss must be the source, or at worst its browser, never the run |
| A challenge page became a message | A bot check is a state of the source, not content |
| The challenge needs a click, and the wait is 15 seconds at about 06:00 UTC | The browser is meant to be reached by a person over KVM. A check that needs a person has to wait for one, in a browser that outlives the run, and the person has to be told |
| Every junk message came out of a fallback | The handlers already knew what page they were written for: `calendar` looks for a Calcium marker or dated links, `instagram` for recorded queries. When that failed they fell back to scraping what was there. The knowledge to detect the deviation was already in the client; only the fallback hid it |

## 4. What changed on 2026-11-24

`fetch_web_sources` now records one outcome per source, enforces a per-source deadline, and never emits a challenge page. Handlers raise `SourceDeviated` where their own expectation fails, with a diagnosis of what the page looks like instead. Detail in `docs/03 - Architecture/03 - Sources — Web.md`, "Per-source outcomes and the deadline".

*Measured 2026-11-24 against the live sites, on the changed code run by hand in a fresh native Chrome:*

| Source | State | What the report said |
|---|---|---|
| `erobay` | `blocked` | bot check did not clear (17 s) |
| `midway` | `deviated` | 2 dated links found; page looks like `cookie_banner`: a consent dialog is on top. Native Chrome loads no engine, so nothing dismisses it there |
| `makerfaire` | `deviated` | 0 dated links; page looks like ordinary `content`. The source's URL is the site's home page |
| `stud` | `ok` | 44 messages |
| `dnalounge`, `the-mix` | `ok` | 65 and 1 |

The hung case and the `instagram` deviation are covered by tests with fake pages (`tests/test_web_fetch_outcomes.py`, `tests/test_instagram.py`); neither has been observed live on the new code.

## 5. What was not measured

1. **Events, as opposed to messages.** Whether a source's messages become events is the real yield. Nothing here joins extraction output back to its source.
2. **Why `instagram` records nothing.** Signed out without a login redirect, a changed page, or a cached response would all log the same lines.
3. **Nights before 2026-11-17.** The journal for this unit starts there.
4. **Which call hangs.** See §2.
5. **Whether `erobay` passes its check when a person clicks it.** No one was at the browser.

## Verification

```bash
journalctl -u event-harvester-mcp --since "-8 days" --no-pager \
  | grep -E 'Web sources: [0-9]+ messages|-> failed|needing attention'
jq '.runs[-1].sources | to_entries | map(select(.value.state != "ok")) | from_entries' data/.web_source_report.json
```

The first shows which nights finished and, once the service runs the new code, a `needing attention` line on any night a source was blocked, hung, skipped, failed or low. The second lists the sources of the last run that were not `ok`. A night with `-> failed` and no `needing attention` line before it means a hang somewhere the per-source deadline does not reach; start with the stage that logged last.
