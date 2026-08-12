# Session Report — a month grid, and the page that shipped without a link

**Date**: 2026-08-12 (second pass) · **Status**: ✅ LANDED · ⚠️ **the event cache is empty — see §4b**
**Scope**: `/calendar` · shared nav · profile editor · the `/admin` cap-clamping defect · a destroyed config file · the sub-path mount · **the mobile pass**
**Tests**: 1411 passed, 0 failed (1314 at start) · **Lint**: 43, unchanged
**Follows**: [2026-08-12_email-extraction-discovery-and-the-percent-bug.md](2026-08-12_email-extraction-discovery-and-the-percent-bug.md)

---

## 1. The question that was really a bug report

> *"where is the profile + keyword manager?"*

`/admin` had shipped two days earlier — mounted, tested, working, holding the
caps equalizer and the keyword editor that had been asked for by name. The
person who asked for it built could not find it, because **nothing anywhere
pointed at it**: not the events page, not the CLI banner, not the doc table.

A page with no inbound link has not shipped. That is the finding, and it cost
nothing to fix and would have cost the whole feature to leave.

`outputs/nav.py` now renders one bar for all three pages. `event-harvester
site` prints all three URLs on startup. `01 - Design/02 - The Control UI.md`
§0a leads with the route table instead of burying it.

**The nav is opt-in on the two generated pages** (`nav=False` by default;
`servers/site.py` passes `True`). Those files are meant to be forwarded, and a
forwarded copy has no origin — `/admin` in an emailed file resolves against
whatever site the recipient has open, or against `file:///admin`. `/admin`
itself renders it unconditionally, since it only ever exists on the origin.

---

## 2. `/calendar` — the same events, by when instead of by rank

`outputs/calendar_html.py`, built from `html_report._prepare` so both pages see
byte-identical records, written in the same `_write` pass so they cannot show
different harvests.

**Why a page and not a toggle.** The list sorts by relevance and is happy to
hide events behind a filter. The grid cannot sort at all — position *is* the
date — and must render every day including the empty ones or it is not a
calendar. Merging them meant a mode flag threaded through rendering, sorting,
filtering and the URL.

| | |
|---|---|
| Grid | Six rows always; a grid that changed height between months would move the buttons under the cursor. Sunday-first, weekends tinted, today outlined |
| Cell | 3 pills (time + title) then `+N more`; click opens the day panel, time-ordered, unparseable times last |
| Undated | Its own tray — a grid cannot place them and dropping them would be a silent loss. **1 of 240** |
| Export | `icsForMany` — one `.ics` for the visible month. Thirty downloads is not an import |
| Phone | Below 700px seven columns leave ~44px each, so pills hide and each day shows a count badge |
| Keys | `←`/`→` month, `T` today — suppressed while the search box has focus |

**247 KB against the list's 1562 KB**, same 240 events. The difference is
almost exactly the 95 inline JPEGs (1276 KB); a 96px cell could not show one
usefully anyway. This is the page to open on a phone.

**Live distribution, measured:** 239 of 240 dated · 152 in August · busiest day
26 events · 218 carry a time. The grid opens on a month that is full, which is
the only reason it is worth opening.

### The hazard it is most exposed to

`new Date('2026-08-12')` parses as **UTC midnight** and renders as the 11th
anywhere west of Greenwich. Same defect as the `Past`-bucket bug of 2026-07-30,
except a calendar renders 42 cells and would have been wrong in all of them.
The grid uses `new Date(y, m, d)` and builds day keys by string formatting;
`toISOString` appears nowhere. Pinned by asserting the *absence* of both
constructs, since pytest has no DOM here.

> The first version of that test failed on its own documentation — the comment
> explaining why `toISOString` is wrong contains the word `toISOString`. Hence
> the `_code()` helper that strips `//` comments before asserting.

`outputs/ics.py` was extracted from `html_report._SCRIPT` in the same pass, so
the two pages cannot drift on exactly this date handling. It gained
`icsForMany`, which the grid needed and the list did not.

---

## 3. The profile editor, and the defect it exposed

Profiles were editable through `POST /admin/api/profiles` and by hand, with no
UI — the gap in *"a profile creater that shows different searchs"*. The panel
now edits name, description, sources and keyword lists.

**Sources and lists are checkboxes, not text fields.** Both vocabularies are
closed and already on the page, and a typed name that matches nothing fails
silently at harvest time, hours later.

Building it made a latent bug reachable. `api_profiles` normalised caps with
`max(0, min(CAP_MAX, int(v)))` — so **a profile carrying `-1` was rewritten to
`0` on every save**: "unlimited" became "drop this source entirely", the exact
misreading `config._clamp_cap` was written to prevent and which axiom 8 turns
on. Unreachable while profiles were hand-edited; reachable the instant the
editor could round-trip one.

`/admin` had **no tests at all**. It has 25 now, and
`TestProfileCaps::test_an_unlimited_cap_survives_a_round_trip` is the
regression.

---

## 4. I destroyed a config file, and this is how

The first draft of `tests/test_admin.py` chdir'd into `tmp_path` and posted to
`/admin/api/keywords`. `data/profiles.json` and `data/settings.json` are
CWD-relative, so chdir isolated them. **`data/keywords.json` is repo-anchored
through `utils.data_path`, so chdir did nothing** — the test overwrote the
operator's four real keyword lists with its own two-term fixture. The file is
untracked, so there was no git copy.

Recovered in full from the session transcript, where the original `Write` call
still held the content. Restored and verified live: `['ai', 'tech', 'queer',
'music']`.

Three things worth carrying forward:

1. **`monkeypatch.chdir` is not isolation** on a codebase with a repo-root
   anchor — and this one has one *on purpose* (axiom 5, after two silent path
   bugs). The two conventions coexist in modules a route calls in one request.
   `tests/test_admin.py` now redirects `keywords.KEYWORDS_FILE` explicitly, in
   an `autouse` fixture, with the reason written above it.
2. **`data/` is gitignored except `web_sources.json`.** `keywords.json`,
   `profiles.json` and `settings.json` are operator state with no backup and no
   history. Anything that writes them in a test needs an explicit redirect.
3. The transcript is a real recovery path, and it worked — but only because the
   file had been created in the same session.

---

## 4a. The nav I had just built was wrong behind the proxy

The site is served at **`central-dev.zt/harvester`** through a reverse proxy,
not only on `:8800`. I had written the nav with root-relative links —
`/calendar`, `/admin` — and the docstring justifying them ("reached by several
names, so no hard-coded base URL") was right about the goal and wrong about the
mechanism: **a root-relative URL is still absolute about the mount point.**
Under the proxy they all resolve against `central-dev.zt/`, off the application
entirely.

`/admin` was the worst of it. Five root-relative `fetch()` calls meant the page
rendered under the proxy and could not reach its own backend — the panels would
have come up empty with nothing in the UI to say why.

Now nothing served emits a leading slash. 15 same-origin URLs across the three
pages, all relative, resolved against three mount spellings:

| | `/` | `/harvester/` | `/harvester` |
|---|---|---|---|
| index · calendar · admin | ✓ | ✓ | ✓ |

The third column is the one relative URLs cannot do alone: with no trailing
slash the browser's base is `/`, so `calendar` resolves to `/calendar` again.
`base_script` closes it — each page strips its own name off
`location.pathname` and inserts a `<base>` while `<head>` is still parsing. The
static hrefs stay relative, so a page whose script never runs is still correct
wherever the path has a trailing slash. **Degradation, not dependence.**

`servers/site.py` also gained `--root-path` / `EH_SITE_ROOT_PATH`, which mounts
every route under the prefix *in addition to* the bare ones. That is for the
other proxy shape — one that forwards `/harvester/calendar` unchanged instead
of stripping it. **Not needed for a stripping proxy, and not how the pages find
each other.** Unset by default; the service command line is unchanged.

`tests/test_subpath_mount.py` (30) pins both halves. The regression that matters
is `TestNoAbsolutePaths`: a reintroduced `/` prefix breaks **only** behind the
proxy and looks correct in every local check, which is exactly how this one got
written in the first place.

---

## 4b. The mobile pass, and what it cost

`/` had claimed "mobile-first" since 2026-07-30 and had never been opened at a
phone width. Rendered in headless Chromium at 320×568, 360×740 and 390×844:

| | before | after |
|---|---|---|
| `/admin` doc width in a 360px viewport | **456–546px, scrolled sideways on every phone** | 360 |
| `/` sticky control block | **303px — 53% of an iPhone SE, permanently** | 61px |
| `/` first card at | **y=546, below the fold** | y=256 |
| `/calendar` grid at | y=611 | y=369 |
| undersized tap targets | 6–8 per page | 0 on `/`, 2 explained |

`/admin` was unusable rather than merely cramped: the number field and the ∞
toggle sat off the right edge of every phone. The cause was not the table's
`overflow-x`, which was already set and did nothing — **a grid item's automatic
minimum size is its min-content width**, so `section` would not shrink below the
33-row table whatever the table was told. Found by hiding elements one at a time
and watching `document.scrollWidth`, not by reasoning about the cascade. One
declaration: `section { min-width: 0 }`.

**Source order decided two silent failures.** The `pointer: coarse` block sat
above the base rules it overrides — same specificity, so it asked for 44px and
measured 41. And `#filter-toggle { display: none }` was written *after* the
media query that reveals it, hiding the filter button on exactly the screens
whose filters are collapsed. Both now last in their stylesheets, both pinned by
`TestSourceOrder`. Neither is visible except on a touch device.

### The cost, which was mine

**I restarted the site service eight times to pick up CSS changes. The service
harvests on start.** Eight full harvests of 20,716 messages against a paid API
in ninety minutes, for changes that touched only stylesheets.

Between 16:09 and 16:12 the OpenAI account ran out of credit
(`429 insufficient_quota`). The 16:12 and 16:13 harvests extracted **0 events**
and **overwrote `data/.mcp_events.json`, which held 251**. Those events are not
recoverable; the job records keep counts, not events. The message cache still
holds all 20,716 messages for its 4h TTL, so a harvest with working credit
re-extracts without re-scraping.

`site --build-only --no-harvest-on-start` regenerates both pages from the cache
and is what a stylesheet change needs. A `service restart` is not.

### The defect it exposed

**A harvest that extracts nothing overwrites a cache that held everything.**
`05 - Outputs.md` records "a failed harvest keeps the last good page", and that
holds for an *exception* — but 328 consecutive `429`s are not an exception at
this layer. Every batch was counted as `llm_no_events`, the run reported
`ok: true, events: 0`, and the empty result was written over 251 good ones.

**Recorded, not fixed.** The shape of a fix: `_write` should decline to replace
a non-empty cache with an empty one when the LLM stage reported errors, and
`llm_no_events` must stop counting transport failures as "the model found
nothing" — a distinction the reject buckets exist to make (axiom 3).

---

## 5. What is where now

Reachable both directly on `:8800` and through the proxy at
`central-dev.zt/harvester`:

| | |
|---|---|
| `/` | the list, 1562 KB, thumbnails |
| `/calendar` | the month grid, 247 KB, no thumbnails |
| `/admin` | caps · keywords · **profiles** · searches |
| `/events.json` | the projection both pages embed |

Live: 4 keyword lists, 3 profiles, 33 sources — **1 native (meetup), 1 category
(luma), 31 local**. That ratio is the honest answer to "search my keywords
everywhere", and the Searches panel exists to show it before a run rather than
after a disappointing one.

---

## 6. Still open

* **The OpenAI account has no credit** (`429 insufficient_quota`, first seen
  2026-08-12 ~16:10 local). Nothing extracts until it is topped up, and the
  site currently serves 0 events. §4b.
* **An empty harvest clobbers a good cache.** §4b. This is the one to fix first.
* **A `service restart` runs a full harvest**, because the installed command
  line has no `--no-harvest-on-start`. That is right for a code deploy and
  wrong for the eight times it happened today. Worth either a
  `service reload`-style verb or a note in the runbook.
* **`sources/venue_location.py` is still unwired** — unchanged from the last
  report §7, and still the intended fix for the hand-maintained venue table.
  Needs a `city` field in `EH-STD-WEBSRC-v0`, a hook in `_web_add`, a backfill
  for 33 sources, and `regions.classify` accepting it above the venue table.
* `Unplaced` is 66 of 240 — erobay puts `[SF]`/`[Oakland]` in the *title* while
  `location` is empty, and `classify` only reads `location`.
* The list page is still 1.5 MB fully embedded; chunked rendering fixed the
  freeze, not the download. The calendar is the interim answer.
* `GMAIL_TOKEN_JSON` still fails a refresh on every Gmail call.
* No profile is *applied* from the UI — the editor writes them, the run page
  that would select one is still unbuilt (`01 - Design/02` §4.4).
* **`pixi run check-head` has side effects.** It exits 0 and its verdict is
  sound, but importing the package evidently reaches live code: this run
  printed the source banners, attempted a web harvest, and collided with the
  running service over the Chromium profile lock. A checker that answers "does
  a bare checkout import?" should not be able to open a browser. `known rough
  edge`, unfixed.

## Verification

```powershell
pixi run test                                   # 1395 passed
pixi run lint                                   # 43, all pre-existing
pixi run event-harvester site --build-only      # writes index.html + calendar.html
curl http://127.0.0.1:8800/calendar             # 200, ~247 KB
curl http://127.0.0.1:8800/admin                # Caps / Keywords / Profiles / Searches
curl -s http://127.0.0.1:8800/admin | Select-String 'href="/'   # must find nothing
```

`unverified` — the grid has been built, served, and read from `/calendar`, but
its interaction (day panel, month paging, month export) has been exercised only
by reading the code, not by clicking it in a browser.

`unverified` — the sub-path fix is proven by resolving all 15 URLs against all
three mount spellings in Python, and by the served pages containing no leading
slash. **It has not been loaded through the actual proxy**, and which of the two
proxy shapes `central-dev.zt` uses is unknown here. If pages 404 rather than
mislink, it is the forwarding shape: add `--root-path /harvester` to the
service command line.
