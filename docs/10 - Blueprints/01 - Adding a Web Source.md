# Blueprint — adding a web source

**Status:** CURRENT · **Date:** 2026-08-04 · **Applies to:** `data/web_sources.json`, `event-harvester web {test,add,probe}` · **Related:** `05 - Standards/EH-STD-WEBSRC-v0`, `03 - Architecture/03 - Sources — Web.md`

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
| `ics` | A real feed exists. Always prefer it |
| `calendar` | Dated event links on the page |
| `feed` | Infinite-scroll or XHR-driven; needs `api_pattern` |
| `search` | A **results** page that takes a query. Needs `queries` |
| `raw` | No links, but the page text carries the schedule — the weekly-venue case |

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
