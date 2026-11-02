# Blueprint — adding a web source

**Status:** CURRENT · **Date:** 2026-08-06 (added step 3d, Instagram) · **Applies to:** `data/web_sources.json`, `event-harvester web {test,add,probe,instagram}` · **Related:** `05 - Standards/EH-STD-WEBSRC-v0`, `03 - Architecture/03 - Sources — Web.md`

Someone hands you a URL. Seven steps, cheapest first, and the whole point is
step 3.

## The one thing to internalise

**The URL you are given is usually not the page that has the events.**

Two for two on 2026-08-04:

| Given | Configured | Why |
|---|---|---|
| `thebitterendsf.com` | `thebitterendsf.com/calendar` | Homepage: 4619 chars of nav/gallery/menu, **0 event links**. The `/calendar` section's own URL: 821 chars of pure schedule |
| `ma.to` | `ma.to/events/san-francisco` | Homepage defaults to **New York** — every event link on it was NYC |

Both would have "worked" if configured as given. One would have fed the LLM a
menu every night; the other would have filled an SF calendar with New York.
Neither would have thrown an error.

---

## What `web add` / `web test` now do for you (2026-11-01)

The seven manual steps below are still the model, but the command runs the first
pass itself and **leaves a record**:

1. **Edge-wall probe first** (`onboard.probe_block`, browser-free). It names the
   wall rather than failing silently: `datadome` (terminal — a real browser,
   `native_chrome` included, does not clear it; the site's API is the only way
   in), `cloudflare-challenge` (clearable — added *with* `native_chrome`, the
   erobay path), or `forbidden-403`/`rate-limited-429`/`server-error-5xx`.
2. **Then** the ICS feed probe and the browser diagnostic, as before.
3. **It logs the attempt** to `data/.source_attempts.json` before it exits —
   mode, link/feed counts, block reason, whether it saved — so "why is this
   source empty" is answered once, not re-diagnosed by hand. The `/sources` page
   reads the same log.
4. **`web add` refuses to save a terminal-walled or empty (`raw`, nothing found)
   source**, so a dead source never lands in config silently. `--force` overrides
   and still logs. A Cloudflare site is *not* refused — it is saved with
   `native_chrome`.

So for a hostile site the loop is now one command: `web test <url>` names the
wall and logs it. tixr, for instance, logs `datadome` and stops — the finding is
durable instead of a terminal scrollback you lose.

---

## 1 — Is it already there?

```powershell
Select-String -Path data\web_sources.json -Pattern "<name-or-domain>"
```

## 2 — Is it even a real site? (no browser, ~1 s)

```bash
curl -sIL --max-time 20 -A "Mozilla/5.0" https://<domain>/ | grep -iE "^HTTP|^location|^server"
```

Read the **redirect chain**, not just the status.

> **Caught one on 2026-08-04.** `mato.com` → `www.mato.com` →
> `brandforce.com/domain/Mato.com/` — a parked domain for sale. `web test`
> would have launched a browser, classified the broker's sale page as `raw`,
> and cheerfully handed it to the LLM on every harvest.

`cloudflare` in `Server:` is a *hint*, not a verdict — decide it in step 5.

## 3 — Find the page that actually has the events

Three cheap probes, in order. Stop when one answers.

**a. Structured feed.** Free and beats scraping outright:

```powershell
pixi run event-harvester web probe https://<domain>/
```

**b. Sitemap** — reveals city routes, event collections, and whether a
one-pager is hiding sections:

```bash
curl -sL --max-time 15 https://<domain>/sitemap.xml | grep -oE '<loc>[^<]*</loc>' | sed 's/<[^>]*>//g' | head -30
```

**c. Nav links** — the highest-yield probe for the two failure modes above:

```bash
curl -sL --max-time 20 -A "Mozilla/5.0" https://<domain>/ -o /tmp/page.html
grep -oiE 'href="/[^"]{0,60}"' /tmp/page.html | sort -u | head -40
```

Look for `calendar`, `events`, `schedule`, `whats-on`, `/events/<city>`.

> `ma.to` exposed `/events/{alameda,atlanta,chicago,los-angeles,new-york,san-francisco}`
> this way. `thebitterendsf.com` exposed a `#calendar-section` anchor whose
> section had its own URL and its own Squarespace collection.

**Squarespace one-pagers** hide sections behind anchors. `<div id="x-section"
data-url-id="x">` means `/x` exists as a page — and `/x?format=json` returns
just that section's content, which is how you compare candidates without a
browser at all.

**d. Instagram** — for a venue whose site has no calendar at all:

```powershell
pixi run event-harvester web instagram https://<domain>/
```

A `@handle` printed in green means the site names its account clearly enough
to adopt (added 2026-08-06). Add it to the `instagram` source's `accounts`
list — `--apply` does that for configured sources — **rather than typing a
handle you guessed**. Oasis is `@theoasissf`, not `@sfoasis`, and Instagram
answers 200 for handles that do not exist, so a guess fails silently and
forever. `unsure` lines are candidates for a human, not for `--apply`.

## 4 — Diagnose each candidate

```powershell
pixi run event-harvester web test https://<candidate>
```

Read four numbers:

| Signal | Means |
|---|---|
| **Event links, with dates** | The number that decides it. Dated + fetchable is the good case |
| **Raw text chars** | Only matters when links are 0. High text + 0 links = a `raw` candidate, but check *what* the text is |
| **Feed items** | >1 means a real feed was found; prefer `ics` |
| **Recommendation** | A starting point, not a verdict — see step 5 |

Run it on **every** candidate from step 3 and pick on the numbers.

```
thebitterendsf.com            4619 chars,  0 links   ← reject
thebitterendsf.com/calendar    821 chars,  0 links   ← accept: all signal
ma.to/events/san-francisco    3756 chars, 18 links   ← accept
```

Note the Bitter End row: **fewer characters won.** More text is not more
signal when the extra text is a nav menu.

## 5 — Decide the mode, and override with a reason

| Mode | When |
|---|---|
| `ics` | A real iCalendar feed exists. Always prefer it |
| `rss` | An RSS/Atom feed exists. Prefer it over scraping — one item per event |
| `luma` / `partiful` | The site *is* Luma or Partiful. Never scrape these — see below |
| `calendar` | Dated event links on the page |
| `feed` | Infinite-scroll or XHR-driven; needs `api_pattern` |
| `search` | A **results** page that takes a query. Needs `queries` |
| `raw` | No links, and the page text carries a **short** schedule — the weekly-venue case |

**`raw` does not scale with the number of events on the page**, and this is the trap. It produces exactly one message however many shows the page lists, and the LLM returns roughly one event from one message. Measured 2026-08-07 on Bottom of the Hill: a 338 KB calendar page listing ~40 shows became a 30,164-character message and yielded **one** event — *"Calendar and tickets page; includes merch and newsletter sign-up links"*. The same venue's RSS yielded **65**.

So when step 4 recommends `raw` on a page you can see is dense, treat that as a signal to go back to step 3a and look harder for a feed. `web probe` only detects **iCalendar**; grep the HTML for `RSS.xml`, `feed`, `atom` yourself:

```bash
grep -oiE '(rss|atom)[^"<>]{0,30}\.xml' /tmp/page.html | sort -u
```

Beware false positives — `ical` matches CSS `vertical-align`.

**Before concluding "no feed", check whether the page is a modern JS app that
server-renders its data anyway.** Added 2026-08-10, after `luma` — configured
as `calendar` — was found returning 1 event from a listing of 80.

```bash
curl -sL --max-time 20 -A "Mozilla/5.0" https://<domain>/<page> \
  | grep -o '__NEXT_DATA__\|__NUXT__\|application/ld+json' | sort -u
```

A hit means the event list is probably sitting in the HTML as JSON, already
typed, with no browser required. Two things it is worth checking next, because
both paid off immediately:

- **Does an API sit behind it?** Luma's city page led to
  `api.lu.ma/discover/get-paginated-events`, keyless, cursor-paginated, 45
  events per call. Open devtools' network tab, or look for `api.` hostnames in
  the payload.
- **Is the payload city-bucketed?** Partiful's is, `NYC` first. See step 3's
  warning — this is the same failure in a new place.

`application/ld+json` is the cheapest win of all: `web_utils._extract_structured_data`
already parses schema.org `Event`, and Gmail link expansion uses it to pull
exact date/time/venue off Luma and Eventbrite pages **without a browser**,
despite Luma being a React SPA. Do not assume a SPA renders client-side —
measure it.

Two overrides worth knowing:

- **`search` suggested for a venue homepage → use `raw`.** `search` wants a
  query and is meant for result pages. (`the-cafe` is configured this way.)
- **Cloudflare challenge → `native_chrome: true`** plus a `cooldown_hours`.
  Only `erobay` needs this. Cloudflare *fronting* a site is common and usually
  harmless — `ma.to` is behind Cloudflare and Playwright walks straight in.

**Record the reason in the commit message.** The `the-cafe` override's
rationale lives only in a commit; that is one `git log` away from lost.

## 6 — Configure

```jsonc
{
  "url": "https://ma.to/events/san-francisco",  // the page from step 4, not step 1
  "name": "mato",                                // short, lowercase, hyphenated
  "mode": "calendar",
  "scroll_seconds": 8,                           // lazy-loading pages only
  "category": "venues",                          // venues only; aggregators carry none
  "cooldown_hours": 6                            // venues; omit for aggregators
}
```

- **Name** in house style: `the-cafe`, `wild-side-west`, `mato` — not the
  auto-suggested `ma` or `thebitterendsf`.
- **Cooldown** is about change rate, not politeness. Bar calendars change
  weekly → 6 h. City aggregators turn over daily → none.
- `category` is currently **inert** — written, read by nothing. Set it anyway
  for the migration to tags (`01 - Design/02 - The Control UI.md` §2.1).

## 7 — Prove it end to end

Never finish on `web test`. It measures the *page*; this measures the
*pipeline*:

```powershell
pixi run event-harvester --only web --web-source <name> --days 3 --dry-run --no-cooldown
```

Expect a stage line and real events:

```
54 -> 54 (classifier) -> 54 (caps) -> 54 (rerank/score) -> LLM
Events (16)
```

`--dry-run` means no TickTick write. It **does** spend LLM tokens — that is
the point, since extraction is what you are verifying.

Check the titles are events and not nav items, and that dates and venues are
populated. A source that yields links but garbage events is worse than one
that yields nothing, because it costs tokens every night.

## 8 — Commit with the numbers

The next reader needs to know what was true when you added it:

```
sources: add <name>, pointed at <the page you actually chose>

<why the given URL was wrong, with both char/link counts>
web test: N links, M chars, feed?
Live --dry-run: X messages -> Y events
<mode override reason, if any>
```

---

## Known rough edges this blueprint does not fix

- **Recurring schedules under-extract.** A `raw` venue page advertising four
  weekly nights typically yields one event — the LLM mints the next instance
  of one recurring night. Affects all 12 `raw` bars. Not a per-source problem;
  do not spend time tuning one source for it.
- **Step 3 is manual.** Finding the better URL is exactly the work
  `01 - Design/02 - The Control UI.md` §5 proposes automating in
  `/admin/sources/new` — surface candidate subpaths with their link counts.
  Until then it is `curl` and judgement.

## Verification

```powershell
pixi run event-harvester web list                       # the source appears
pixi run event-harvester --only web --web-source <name> --days 3 --dry-run
```

`verified 2026-08-04` — this procedure as written produced `the-bitter-end`
(1 event from an 821-char page) and `mato` (16 events from 54 messages), and
rejected `mato.com` at step 2 before any browser started.
