"""Self-contained, mobile-first HTML view of a harvest.

One file, no server, no network. That combination is the whole design:

* **Forwardable** — a single `.html` can be emailed, AirDropped, dropped in a
  shared folder, or hosted anywhere. The Markdown report is forwardable too but
  renders as a wall of text on a phone; the Flet UI is interactive but is an
  application, not something you can send someone.
* **Mobile-first** — the phone is where you actually read "what's on this
  weekend", and it is where the two actions that matter (add to calendar, open
  the listing) are one tap.
* **Offline** — no CDN, no fonts, no analytics. Inline CSS and JS only. A page
  that needs the network to render is not forwardable, it is a bookmark.

**Untrusted input.** Event titles, locations and details are scraped from
arbitrary websites. Nothing here interpolates that text into an HTML string —
the payload is embedded as JSON inside a `<script type="application/json">`
block and rendered through `textContent` at runtime, so a venue whose event is
named `<img onerror=...>` renders as that literal string. The only escaping
this file does is on the JSON itself, for `</script>` and the two Unicode line
separators that are legal in JSON and fatal in a script block.

**Forwarding this file forwards the data in it.** It embeds every event,
including the source channel each came from. That is the point, and it is worth
knowing before sending one on.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import date, datetime
from html import escape
from pathlib import Path
from typing import Any

from event_harvester.observability.metrics import count, record
from event_harvester.observability.tracing import set_span_attrs, traced
from event_harvester.outputs.deeplinks import ticktick_deep_link
from event_harvester.outputs.regions import HINTS as REGION_HINTS
from event_harvester.outputs.regions import LABELS as REGION_LABELS
from event_harvester.outputs.regions import REGIONS
from event_harvester.outputs.regions import classify as classify_region

_TRACER = "event_harvester.outputs.html_report"
logger = logging.getLogger(_TRACER)

_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def _safe_json(payload: Any) -> str:
    """Serialize for embedding inside a <script> block.

    `</script>` inside a JSON string would close the block early — the classic
    way an "escaped" payload escapes anyway. U+2028/U+2029 are valid JSON but
    are line terminators to a JS parser.
    """
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return (
        text.replace("</", "<\\/")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def _bucket(iso_date: str | None, today: date) -> str:
    """Group an event into a human bucket. Unknown dates sort last, not first."""
    if not iso_date:
        return "Undated"
    match = _ISO_DATE_RE.match(str(iso_date))
    if not match:
        return "Undated"
    try:
        when = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return "Undated"
    delta = (when - today).days
    if delta < 0:
        return "Past"
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Tomorrow"
    if delta <= 7:
        return "This week"
    if delta <= 30:
        return "This month"
    return "Later"


_BUCKET_ORDER = ("Today", "Tomorrow", "This week", "This month", "Later", "Undated", "Past")


_KNOWN_PLATFORMS = ("discord", "telegram", "gmail", "signal", "web")


def _platform_of(ev: dict, source: str) -> str:
    """Which platform an event came from — for the filter chips.

    **This used to be `source.split(" in ")[0]`, i.e. the author.** With 33
    events that produced **22 chips**, one per sender, several of them raw
    email addresses, and the filter row ran off the side of the page. A filter
    with one option per row is not a filter.

    Three sources of truth, strongest first:

    1. an explicit `platform` on the event;
    2. `_platforms`, recorded by `analysis.attach_source_ids` from the batch
       the event was extracted in;
    3. **the channel half of `source`** — `"@someone in Gmail / INBOX"`. This
       one is the quiet win: `build_prompt` groups messages under
       ``f"{platform.capitalize()} / {channel}"`` and the model echoes that
       group key back, so the platform has been sitting in the string all
       along, on the *other* side of the `" in "` the old code split on.
       It needs no re-harvest, which is why it is worth having as well as (2).
    """
    # Only a value from the known vocabulary counts. `data/.site/events.json`
    # is *already-prepared* output, so re-rendering it feeds this function the
    # `platform` an older build wrote — which, before this fix, was the
    # author's email address. Trusting any non-empty string would make the old
    # chips survive every rebuild and heal only after a fresh harvest.
    explicit = str(ev.get("platform") or "").strip().lower()
    if explicit in _KNOWN_PLATFORMS or explicit == "mixed":
        return explicit

    plats = [p for p in (ev.get("_platforms") or []) if p in _KNOWN_PLATFORMS]
    if plats:
        return plats[0] if len(plats) == 1 else "mixed"

    return _platform_from_channel(source)


def _platform_from_channel(source: str) -> str:
    """Read the platform out of a `source` string.

    Two shapes occur, because the model writes this field freely. The prompt
    asks for `"@author in #channel"`, and it often obliges — but it also
    frequently echoes the group header verbatim, giving a bare
    `"Telegram / Frolic Party SF"` with no `" in "` at all. Requiring the
    separator left **148 of 240 events with no source chip** (2026-08-12).
    Take the channel half when there is one, otherwise read the whole string.
    """
    channel = source.split(" in ", 1)[1] if " in " in source else source
    head = channel.strip().split("/", 1)[0].strip().lower()
    if head in _KNOWN_PLATFORMS:
        return head
    # Instagram arrives as `instagram/@handle` — it is a web sub-source, and
    # giving it its own chip would start the per-sender sprawl over again.
    if head == "instagram":
        return "web"
    return ""


def _clean_author(source: str) -> str:
    """Human-readable sender, for the card — not the raw From header.

    `"Ben Holfeld <BenHolfeld@user.luma-mail.com>"` becomes `"Ben Holfeld"`,
    and a bare `"<noreply@reminder.eventbrite.com>"` becomes the address
    without its angle brackets. Those brackets were not merely ugly: rendered
    unescaped they were parsed as an HTML tag, which is why three filter chips
    appeared completely blank.
    """
    who = source.split(" in ")[0].strip()
    if "<" in who and ">" in who:
        name = who[:who.index("<")].strip().strip('"')
        addr = who[who.index("<") + 1:who.rindex(">")].strip()
        who = name or addr
    return who.strip().strip('"')


def _prepare(events: list[dict], today: date) -> list[dict[str, Any]]:
    """Flatten harvest events into the shape the page renders.

    Deliberately narrow: only fields the page shows. Everything else stays out
    of the file, because the file gets forwarded.
    """
    out: list[dict[str, Any]] = []
    for index, ev in enumerate(events):
        title = str(ev.get("title") or ev.get("content") or "Untitled")[:200]
        iso = str(ev.get("date") or ev.get("best_date") or "")[:10]
        details = str(ev.get("details") or ev.get("notes") or ev.get("content") or "")
        source = str(ev.get("source") or ev.get("channel") or "")
        out.append({
            "i": index,
            "title": title,
            "date": iso,
            "time": str(ev.get("time") or ""),
            "location": str(ev.get("location") or ""),
            "link": str(ev.get("link") or ""),
            "details": details[:1200],
            "source": source,
            "author": _clean_author(source),
            "platform": _platform_of(ev, source),
            "region": classify_region(str(ev.get("location") or ""), source),
            "priority": ev.get("priority", ev.get("score", 3)),
            "bucket": _bucket(iso, today),
            "ticktick": ticktick_deep_link(title, iso or None, not ev.get("time"), details[:400]),
        })
    return out


_STYLE = """
:root {
  --bg: #ffffff; --fg: #16181d; --muted: #5b6472; --line: #e4e7ec;
  --card: #ffffff; --accent: #7c3aed; --chip: #f2f4f7; --shadow: 0 1px 2px rgba(16,24,40,.06);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1115; --fg: #e8eaed; --muted: #98a2b3; --line: #242832;
    --card: #171a21; --accent: #a78bfa; --chip: #212530; --shadow: none;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        Helvetica, Arial, sans-serif;
  -webkit-text-size-adjust: 100%;
}
header { padding: 20px 16px 12px; border-bottom: 1px solid var(--line); }
h1 { margin: 0 0 4px; font-size: 20px; letter-spacing: -0.01em; }
.meta { color: var(--muted); font-size: 13px; }
.controls {
  position: sticky; top: 0; z-index: 5; background: var(--bg);
  border-bottom: 1px solid var(--line); padding: 10px 16px;
}
input[type=search] {
  width: 100%; padding: 11px 12px; font-size: 16px; color: var(--fg);
  background: var(--chip); border: 1px solid var(--line); border-radius: 10px;
}
/* One row, search + sort + layout, because they are all "how do I look at
   this" and were previously stacked across three rows. */
.bar { display: flex; gap: 8px; align-items: center; }
.bar input[type=search] { flex: 1 1 auto; min-width: 0; }
.filters { display: flex; gap: 12px; align-items: center; padding-top: 10px; }
/* Chips wrap rather than scroll. Horizontal scroll hid every filter past the
   viewport edge with no affordance saying so — and there are now few enough
   to fit, since they are platforms and not one chip per sender. */
.chips { display: flex; gap: 8px; flex-wrap: wrap; flex: 1 1 auto; }
.chip {
  flex: 0 0 auto; display: inline-flex; align-items: center; gap: 7px;
  padding: 7px 13px; border-radius: 999px; border: 1px solid var(--line);
  background: var(--chip); color: var(--muted); font-size: 13px; cursor: pointer;
  min-height: 34px;
}
.chip:hover { border-color: var(--accent); color: var(--fg); }
.chip[aria-pressed="true"] { background: var(--accent); border-color: var(--accent); color: #fff; }
/* The count rides inside the chip so the filter says what it will show
   before you click it. */
.tally {
  font-size: 11px; font-weight: 700; padding: 1px 6px; border-radius: 999px;
  background: rgba(127,127,127,.22); color: inherit; font-variant-numeric: tabular-nums;
}
.chip[aria-pressed="true"] .tally { background: rgba(255,255,255,.25); }
main { padding: 0 16px 48px; max-width: 1400px; margin: 0 auto; }
h2.bucket {
  font-size: 13px; text-transform: uppercase; letter-spacing: .06em;
  color: var(--muted); margin: 24px 0 10px;
}

/* n x m: n comes from the viewport, m from how many events are in the bucket.
   auto-fill rather than a fixed column count so the same file is one column on
   a phone and five on a desktop without a media-query ladder. */
.shelf {
  display: grid; gap: 10px;
  grid-template-columns: repeat(auto-fill, minmax(270px, 1fr));
}
/* Carousel: one horizontally-snapping row per bucket. Same cards, same DOM —
   only the container changes, so nothing re-renders when you toggle. */
body.carousel .shelf {
  display: flex; overflow-x: auto; scroll-snap-type: x mandatory;
  padding-bottom: 6px; scrollbar-width: thin;
}
body.carousel .shelf .card {
  flex: 0 0 min(86%, 300px); scroll-snap-align: start; margin-bottom: 0;
}
.card {
  background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  padding: 14px; box-shadow: var(--shadow);
  display: flex; flex-direction: column;
  /* Skip layout, paint and image decode for cards that are off screen. With
     240 cards and ~95 inline JPEGs this is the single largest win available,
     and it costs one line. `contain-intrinsic-size` supplies a placeholder
     height so the scrollbar does not lurch as cards are skipped and restored;
     `auto` lets the browser remember each card's real size once measured.
     Unsupported browsers simply render everything, as before. */
  content-visibility: auto;
  contain-intrinsic-size: auto 260px;
}
.card .actions { margin-top: auto; }
.bar select, .bar button {
  flex: 0 0 auto; padding: 9px 12px; min-height: 40px; font-size: 13px;
  color: var(--fg); background: var(--chip); border: 1px solid var(--line);
  border-radius: 10px; cursor: pointer;
}
.count {
  flex: 0 0 auto; color: var(--muted); font-size: 13px; white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
@media (max-width: 560px) {
  /* The layout toggle is the least-used control and the first to go. */
  .bar #layout { display: none; }
  .filters { flex-direction: column; align-items: flex-start; gap: 6px; }
}
/* Fixed aspect box, so a card with a picture and one without stay the same
   width and the grid does not jump as images load. */
.thumb {
  display: block; width: calc(100% + 2px); margin: -1px -1px 10px; aspect-ratio: 300 / 168;
  object-fit: cover; border-radius: 11px 11px 0 0; background: var(--chip);
}
.card h3 { margin: 0 0 6px; font-size: 16px; line-height: 1.35; }
.when { color: var(--accent); font-weight: 600; font-size: 14px; }
.where, .src { color: var(--muted); font-size: 13px; }
.src { margin-top: 6px; }
/* Detail is shown, not hidden behind a button. Clamped to three lines so a
   long one cannot stretch its card taller than its neighbours in the grid;
   `.open` releases the clamp. Most details in practice are ~90 characters
   and fit in one line, so most cards never show a toggle at all. */
.details {
  margin: 10px 0 0; color: var(--fg); font-size: 14px; line-height: 1.45;
  white-space: pre-wrap; overflow-wrap: anywhere;
  display: -webkit-box; -webkit-box-orient: vertical;
  -webkit-line-clamp: 3; line-clamp: 3; overflow: hidden;
}
.details.open {
  -webkit-line-clamp: unset; line-clamp: unset; overflow: visible;
}
.actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
a.btn, button.btn {
  flex: 1 1 auto; min-width: 120px; min-height: 40px; padding: 9px 12px;
  border-radius: 9px; border: 1px solid var(--line); background: var(--chip);
  color: var(--fg); font-size: 14px; text-align: center; text-decoration: none;
  cursor: pointer; display: inline-flex; align-items: center; justify-content: center;
}
/* `display: inline-flex` above beats the user-agent `[hidden]` rule, so a
   hidden button would still render. This puts it back. */
a.btn[hidden], button.btn[hidden] { display: none; }
a.btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
.empty { color: var(--muted); padding: 40px 0; text-align: center; }
#toast {
  position: fixed; left: 50%; bottom: 20px; transform: translate(-50%, 80px);
  background: var(--accent); color: #fff; padding: 10px 16px; border-radius: 999px;
  font-size: 14px; opacity: 0; pointer-events: none; transition: transform .25s, opacity .25s;
  max-width: 90vw; text-align: center;
}
#toast.show { transform: translate(-50%, 0); opacity: 1; }
@media (prefers-reduced-motion: reduce) { #toast { transition: none; } }
footer { color: var(--muted); font-size: 12px; padding: 24px 16px; text-align: center; }
"""

# Rendering happens here, in the browser, from JSON — never by interpolating
# scraped text into markup. See the module docstring.
_SCRIPT = r"""
const DATA = JSON.parse(document.getElementById('events-data').textContent);
const BUCKETS = JSON.parse(document.getElementById('bucket-order').textContent);
const main = document.getElementById('list');
const search = document.getElementById('q');
let activeSource = null;
let activeRegion = null;

function icsFor(ev) {
  const esc = (s) => String(s || '').replace(/([,;\\])/g, '\\$1').replace(/\n/g, '\\n');
  const stamp = (d, t) => {
    if (!d) return null;
    const day = d.replace(/-/g, '');
    if (!t) return day;
    const m = String(t).match(/(\d{1,2})(?::(\d{2}))?\s*(am|pm)?/i);
    if (!m) return day;
    let h = parseInt(m[1], 10);
    const min = m[2] || '00';
    const ap = (m[3] || '').toLowerCase();
    if (ap === 'pm' && h < 12) h += 12;
    if (ap === 'am' && h === 12) h = 0;
    return day + 'T' + String(h).padStart(2, '0') + min + '00';
  };
  const dt = stamp(ev.date, ev.time);
  if (!dt) return null;
  const allDay = dt.length === 8;
  const lines = [
    'BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//event-harvester//EN',
    'BEGIN:VEVENT',
    'UID:eh-' + ev.i + '-' + dt + '@event-harvester',
    allDay ? 'DTSTART;VALUE=DATE:' + dt : 'DTSTART:' + dt,
    'SUMMARY:' + esc(ev.title),
  ];
  if (ev.location) lines.push('LOCATION:' + esc(ev.location));
  const desc = [ev.details, ev.link].filter(Boolean).join('\n\n');
  if (desc) lines.push('DESCRIPTION:' + esc(desc));
  lines.push('END:VEVENT', 'END:VCALENDAR');
  return 'data:text/calendar;charset=utf-8,' + encodeURIComponent(lines.join('\r\n'));
}

// The extractor emits details as "prose | location | url". On a card the
// location already has its own line and the URL already has an Open button,
// so inline those segments are duplication rather than detail — strip them
// and keep the prose. Search still runs against the raw `ev.details`.
function detailText(ev) {
  const loc = String(ev.location || '').trim().toLowerCase();
  return String(ev.details || '')
    .split('|')
    .map(s => s.trim())
    .filter(Boolean)
    .filter(s => s.toLowerCase() !== loc)
    .filter(s => !/^https?:\/\/\S+$/i.test(s))
    .join(' — ');
}

function card(ev) {
  const el = document.createElement('article');
  el.className = 'card';

  // Thumbnail first, and only when there is one — an empty box where a
  // picture should be reads as broken, whereas no box just reads as text.
  if (ev.thumb) {
    const fig = document.createElement('img');
    fig.className = 'thumb';
    fig.src = ev.thumb;          // a data: URI, embedded at build time
    fig.alt = '';                // decorative: the title says the same thing
    fig.loading = 'lazy';
    fig.decoding = 'async';
    el.appendChild(fig);
  }

  const h = document.createElement('h3');
  h.textContent = ev.title;                       // textContent, never innerHTML
  el.appendChild(h);

  const when = [ev.date, ev.time].filter(Boolean).join(' · ');
  if (when) {
    const w = document.createElement('div');
    w.className = 'when'; w.textContent = when; el.appendChild(w);
  }
  if (ev.location) {
    const w = document.createElement('div');
    w.className = 'where'; w.textContent = ev.location; el.appendChild(w);
  }
  // `author` is the From header with the angle-bracket address stripped;
  // `source` is the raw string, kept for search. Show the readable one.
  const who = ev.author || ev.source;
  if (who) {
    const s = document.createElement('div');
    s.className = 'src';
    s.textContent = ev.platform ? `${who} · ${ev.platform}` : who;
    el.appendChild(s);
  }

  const actions = document.createElement('div');
  actions.className = 'actions';

  const detail = detailText(ev);
  if (detail) {
    const body = document.createElement('p');
    body.className = 'details';
    body.textContent = detail;                    // textContent, never innerHTML
    el.appendChild(body);

    // Built now but hidden; `syncDetailToggles` measures after layout and
    // reveals it only for the cards whose text is actually clipped.
    const toggle = document.createElement('button');
    toggle.className = 'btn detail-toggle';
    toggle.type = 'button';
    toggle.textContent = 'More';
    toggle.hidden = true;
    toggle.setAttribute('aria-expanded', 'false');
    toggle.onclick = () => {
      const open = body.classList.toggle('open');
      toggle.setAttribute('aria-expanded', String(open));
      toggle.textContent = open ? 'Less' : 'More';
    };
    actions.appendChild(toggle);
    // Register for measurement instead of measuring now: `card()` runs inside
    // the render loop, and reading layout there would reintroduce exactly the
    // per-card reflow this replaced.
    watchToggle(body);
  }

  const ics = icsFor(ev);
  if (ics) {
    const a = document.createElement('a');
    a.className = 'btn primary'; a.href = ics;
    a.setAttribute('download', (ev.title.slice(0, 40).replace(/[^\w -]/g, '') || 'event') + '.ics');
    a.textContent = 'Add to calendar';
    actions.appendChild(a);
  }
  if (ev.link) {
    const a = document.createElement('a');
    a.className = 'btn'; a.href = ev.link; a.target = '_blank'; a.rel = 'noopener noreferrer';
    a.textContent = 'Open';
    actions.appendChild(a);
  }
  if (ev.ticktick) {
    const a = document.createElement('a');
    a.className = 'btn'; a.href = ev.ticktick; a.textContent = 'TickTick';
    actions.appendChild(a);
  }

  el.appendChild(actions);
  return el;
}

const sortBy = document.getElementById('sort');
const layoutBtn = document.getElementById('layout');
const countEl = document.getElementById('count');

const SORTS = {
  date: (a, b) => (a.date || '9999').localeCompare(b.date || '9999'),
  priority: (a, b) => (b.priority || 0) - (a.priority || 0),
  title: (a, b) => a.title.localeCompare(b.title),
};

function matching() {
  const q = search.value.trim().toLowerCase();
  // Every term must appear somewhere, so "queer friday" narrows instead of
  // matching either word.
  const terms = q ? q.split(/\s+/) : [];
  return DATA.filter(ev => {
    // The two chip rows are independent axes and AND together: "SF" plus
    // "web" means SF events from web sources, not one or the other.
    if (activeSource && ev.platform !== activeSource) return false;
    if (activeRegion && ev.region !== activeRegion) return false;
    if (!terms.length) return true;
    const hay = (ev.title + ' ' + ev.location + ' ' + ev.details + ' ' +
                 ev.source + ' ' + ev.date).toLowerCase();
    return terms.every(t => hay.includes(t));
  });
}

// Cards are built across animation frames rather than in one pass. At 33
// events the difference was invisible; at 240 — with ~95 inline JPEGs to
// decode — building them all synchronously blocked the main thread until the
// whole set was done, so the page appeared frozen rather than progressively
// filling. A render token cancels an in-flight build, because typing in the
// search box starts a new render on nearly every keystroke.
let renderToken = 0;
const FRAME_BUDGET_MS = 8;   // half a 60fps frame, leaving room to paint

function render() {
  const token = ++renderToken;
  main.textContent = '';
  const matches = matching().sort(SORTS[sortBy.value] || SORTS.date);
  countEl.textContent = matches.length + ' of ' + DATA.length;

  if (!matches.length) {
    const p = document.createElement('p');
    p.className = 'empty';
    p.textContent = 'Nothing matches.';
    main.appendChild(p);
    return;
  }

  // Flatten to a work list first so slicing is by *card*, not by bucket — one
  // bucket can hold a hundred events, which would defeat the whole point.
  const work = [];
  for (const bucket of BUCKETS) {
    const inBucket = matches.filter(ev => ev.bucket === bucket);
    if (!inBucket.length) continue;
    work.push({heading: bucket + ' · ' + inBucket.length});
    for (const ev of inBucket) work.push({ev});
  }

  let i = 0;
  let shelf = null;
  const step = () => {
    if (token !== renderToken) return;   // superseded by a newer render
    const started = performance.now();
    while (i < work.length && performance.now() - started < FRAME_BUDGET_MS) {
      const item = work[i++];
      if (item.heading !== undefined) {
        const h = document.createElement('h2');
        h.className = 'bucket';
        h.textContent = item.heading;
        main.appendChild(h);
        shelf = document.createElement('div');
        shelf.className = 'shelf';
        main.appendChild(shelf);
      } else {
        (shelf || main).appendChild(card(item.ev));
      }
    }
    if (i < work.length) requestAnimationFrame(step);
  };
  step();
}

// Whether the clamp actually bit can only be known after layout, and it
// changes with card width — so it re-runs on layout switch and on resize.
//
// **It is measured per card, when the card is near the viewport.** The old
// version looped every `.details` in the document reading `scrollHeight` and
// `clientHeight`, which forces a synchronous layout each time: 240 cards meant
// 240 forced reflows in a tight loop, and that — more than the images — was
// what froze the page. An IntersectionObserver defers each measurement to the
// moment it can matter, and pairs with `content-visibility: auto`, under which
// an offscreen card has no layout to measure anyway.
function measureToggle(body) {
  const toggle = body.parentElement.querySelector('.detail-toggle');
  if (!toggle) return;
  if (body.classList.contains('open')) { toggle.hidden = false; return; }
  // +1 absorbs sub-pixel rounding, which otherwise shows a "More" button
  // that expands to reveal nothing.
  toggle.hidden = body.scrollHeight <= body.clientHeight + 1;
}

const toggleObserver = ('IntersectionObserver' in window)
  ? new IntersectionObserver((entries, obs) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        measureToggle(entry.target);
        obs.unobserve(entry.target);   // one measurement per card per layout
      }
    }, {rootMargin: '400px'})          // measure just before it scrolls in
  : null;

function watchToggle(body) {
  if (toggleObserver) toggleObserver.observe(body);
  else measureToggle(body);            // no observer: behave as before
}

// A width change invalidates every previous measurement, so re-observe rather
// than re-measure — the observer will fire for whatever is on screen now.
function syncDetailToggles() {
  for (const body of main.querySelectorAll('.details')) watchToggle(body);
}

let resizeTimer = null;
addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(syncDetailToggles, 150);
});

// One handler for both rows. A chip declares which axis it belongs to by
// which data attribute it carries, so adding a third axis later needs a row
// of markup and an entry here, not a parallel copy of this logic.
const AXES = [
  {attr: 'source', get: () => activeSource, set: v => { activeSource = v; }},
  {attr: 'region', get: () => activeRegion, set: v => { activeRegion = v; }},
];

document.querySelectorAll('.chip').forEach(chip => {
  const axis = AXES.find(a => a.attr in chip.dataset);
  if (!axis) return;
  chip.onclick = () => {
    // The "All"/"Everywhere" chip carries an empty value, so it clears its
    // axis by the same path as re-clicking an active chip. One rule, not two.
    const value = chip.dataset[axis.attr] || null;
    axis.set(axis.get() === value ? null : value);
    const active = axis.get();
    document.querySelectorAll(`.chip[data-${axis.attr}]`).forEach(c => {
      const mine = c.dataset[axis.attr] || null;
      c.setAttribute('aria-pressed',
        String(active === null ? mine === null : mine === active));
    });
    render();
  };
});

// Layout is a body class, so switching it costs a repaint rather than a
// re-render — the cards themselves are identical in both modes.
function setLayout(mode) {
  document.body.classList.toggle('carousel', mode === 'carousel');
  layoutBtn.textContent = mode === 'carousel' ? 'Carousel' : 'Grid';
  layoutBtn.dataset.mode = mode;
  try { localStorage.setItem('eh-layout', mode); } catch (e) { /* file:// */ }
  // Card width differs between the modes, so the same text can be clipped in
  // one and not the other.
  requestAnimationFrame(syncDetailToggles);
}
layoutBtn.onclick = () => setLayout(layoutBtn.dataset.mode === 'carousel' ? 'grid' : 'carousel');
let saved = 'grid';
try { saved = localStorage.getItem('eh-layout') || 'grid'; } catch (e) { /* file:// */ }
setLayout(saved);

search.oninput = render;
sortBy.onchange = render;
render();

// ── Live update ────────────────────────────────────────────────────
// Only when served. The identical file opened from disk has no server to ask,
// and polling from file:// would fail on CORS and log an error every minute
// for no reason — the page has to stay useful as an email attachment.
const LIVE = location.protocol === 'http:' || location.protocol === 'https:';
const POLL_MS = 60000;
let lastStamp = null;

function toast(message) {
  let el = document.getElementById('toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    document.body.appendChild(el);
  }
  el.textContent = message;
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 4000);
}

async function poll() {
  try {
    const res = await fetch('healthz', {cache: 'no-store'});
    if (!res.ok) return;
    const health = await res.json();
    const stamp = health.last_refresh_epoch;
    if (!stamp) return;
    if (lastStamp === null) { lastStamp = stamp; return; }
    if (stamp === lastStamp) return;

    const fresh = await fetch('events.json', {cache: 'no-store'});
    if (!fresh.ok) return;
    const events = await fresh.json();
    lastStamp = stamp;

    // Replace in place and re-render rather than reloading the page: a reload
    // would throw away the search term, the active filter and the scroll
    // position, which is exactly the state someone browsing has built up.
    DATA.length = 0;
    for (const ev of events) DATA.push(ev);
    render();

    const meta = document.getElementById('generated');
    if (meta) meta.textContent = new Date(stamp * 1000).toLocaleString();
    toast(events.length + ' event(s) — just updated');
  } catch (e) {
    // Offline, server restarting, or mid-deploy. Try again next tick.
  }
}

if (LIVE) {
  poll();
  setInterval(poll, POLL_MS);
  // Catch up immediately when a phone comes back from sleep, rather than
  // waiting out the rest of the interval.
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) poll();
  });
}
"""


@traced("report.generate_html", tracer_name=_TRACER)
def generate_html_report(
    validated_events: list[dict],
    raw_events: list[dict] | None = None,
    source_counts: dict[str, int] | None = None,
    total_messages: int = 0,
    output_path: str = "events_report.html",
    title: str = "Events",
    today: date | None = None,
    thumbnails: bool = False,
) -> str:
    """Write a self-contained mobile-friendly HTML report. Returns the path.

    Mirrors `report.generate_report`'s signature so it drops into the same call
    sites. `links` is deliberately absent — the Markdown report's "Top Links"
    section is a debugging aid, and this file is meant to be forwarded.

    ``today`` defaults to the **local** date, and that is load-bearing. Event
    dates are local wall-clock dates scraped from venue pages: a 9pm SF show is
    `2026-07-30`, not an instant. Bucketing those against the UTC date put every
    event tonight in "Past" from 5pm PDT onward — the exact hours someone opens
    this page to decide what to do that evening. Caught by rendering it, not by
    a test, because the tests computed both sides the same way.
    """
    events = validated_events or (raw_events or [])
    today = today or datetime.now().astimezone().date()
    prepared = _prepare(events, today)

    # Off by default: this is the only part of report generation that touches
    # the network, and a caller writing a report from cached events should not
    # silently acquire a dependency on the internet being up. The site turns it
    # on; `--build-only` and the tests do not.
    if thumbnails:
        from event_harvester.outputs.thumbnails import thumbnails_for

        try:
            by_link = thumbnails_for([ev["link"] for ev in prepared if ev["link"]])
            for ev in prepared:
                uri = by_link.get(ev["link"])
                if uri:
                    ev["thumb"] = uri
        except Exception as e:  # noqa: BLE001 -- a picture is never worth a report
            logger.warning("thumbnails skipped: %s", e)

    # Local, to match the buckets. A header reading "generated 2026-07-31 UTC"
    # above a card headed "TODAY · 2026-07-30" invites exactly the doubt the
    # bucketing fix was about.
    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    counts = source_counts or {}
    summary = ", ".join(f"{name} {n}" for name, n in counts.items() if n) or "no source counts"

    # Counts on the chips, so the filter says how much it will show before it
    # is clicked. Ordered by size — the useful one is rarely alphabetically
    # first.
    tallies = Counter(ev["platform"] for ev in prepared if ev["platform"])
    ordered = [p for p, _ in tallies.most_common()]

    # `escape` is not decoration. These labels come from LLM-extracted `source`
    # strings, and three of them were literally `<someone@example.com>`, which
    # the browser parsed as an unknown element and rendered as an empty chip.
    # Anything derived from model output reaches this file as data, never
    # markup.
    chips = "\n".join(
        f'<button class="chip" type="button" aria-pressed="false" '
        f'data-source="{escape(p, quote=True)}">{escape(p)}'
        f'<span class="tally">{tallies[p]}</span></button>'
        for p in ordered
    )

    # Region chips are a second, independent axis — they AND with the source
    # filter rather than replacing it. Rendered in a fixed order (SF, Oakland,
    # Broader, Unplaced) rather than by size, because unlike sources these are
    # a known scale the reader is navigating, not a discovered set.
    region_counts = Counter(ev["region"] for ev in prepared)
    region_chips = "\n".join(
        f'<button class="chip" type="button" aria-pressed="false" '
        f'data-region="{r}" title="{escape(REGION_HINTS[r], quote=True)}">'
        f'{escape(REGION_LABELS[r])}'
        f'<span class="tally">{region_counts[r]}</span></button>'
        for r in REGIONS if region_counts[r]
    )

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>{title} — {len(prepared)}</title>
<style>{_STYLE}</style>
</head>
<body>
<header>
  <h1>{title}</h1>
  <div class="meta">{len(prepared)} event(s) · generated
    <span id="generated">{generated}</span></div>
  <div class="meta">{summary} · {total_messages} message(s) scanned</div>
</header>
<div class="controls">
  <div class="bar">
    <input id="q" type="search" placeholder="Search events, venues, people"
           autocomplete="off" aria-label="Search events">
    <select id="sort" aria-label="Sort events">
      <option value="date">Date</option>
      <option value="priority">Priority</option>
      <option value="title">Title</option>
    </select>
    <button id="layout" type="button" data-mode="grid"
            aria-label="Toggle grid or carousel layout">Grid</button>
  </div>
  <div class="filters">
    <div class="chips" role="group" aria-label="Filter by place">
      <button class="chip" type="button" aria-pressed="true"
              data-region="">Everywhere<span class="tally">{len(prepared)}</span></button>
      {region_chips}
    </div>
  </div>
  <div class="filters">
    <div class="chips" role="group" aria-label="Filter by source">
      <button class="chip" type="button" aria-pressed="true"
              data-source="">All<span class="tally">{len(prepared)}</span></button>
      {chips}
    </div>
    <span class="count" id="count"></span>
  </div>
</div>
<main id="list"></main>
<footer>Generated by event-harvester. This file contains the event data —
forwarding it forwards them.</footer>
<script type="application/json" id="events-data">{_safe_json(prepared)}</script>
<script type="application/json" id="bucket-order">{_safe_json(list(_BUCKET_ORDER))}</script>
<script>{_SCRIPT}</script>
</body>
</html>
"""

    path = Path(output_path)
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")

    set_span_attrs(**{
        "report.events": len(prepared),
        "report.bytes": len(html),
        "report.platforms": len(tallies),
    })
    count("report.generated", fmt="html", description="Reports written")
    record("report.size", len(html), unit="By", fmt="html",
           description="Size of a generated report")
    return str(path)
