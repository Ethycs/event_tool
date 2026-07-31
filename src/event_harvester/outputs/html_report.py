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
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from event_harvester.observability.metrics import count, record
from event_harvester.observability.tracing import set_span_attrs, traced
from event_harvester.outputs.deeplinks import ticktick_deep_link

_TRACER = "event_harvester.outputs.html_report"

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
            "platform": str(ev.get("platform") or source.split(" in ")[0] or ""),
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
.chips { display: flex; gap: 8px; overflow-x: auto; padding-top: 10px; scrollbar-width: none; }
.chips::-webkit-scrollbar { display: none; }
.chip {
  flex: 0 0 auto; padding: 7px 13px; border-radius: 999px; border: 1px solid var(--line);
  background: var(--chip); color: var(--muted); font-size: 13px; cursor: pointer;
  min-height: 34px;
}
.chip[aria-pressed="true"] { background: var(--accent); border-color: var(--accent); color: #fff; }
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
}
.card .actions { margin-top: auto; }
.toolbar { display: flex; gap: 8px; align-items: center; padding-top: 10px; flex-wrap: wrap; }
.toolbar select, .toolbar button {
  padding: 7px 11px; min-height: 34px; font-size: 13px; color: var(--fg);
  background: var(--chip); border: 1px solid var(--line); border-radius: 8px;
  cursor: pointer;
}
.count { color: var(--muted); font-size: 13px; margin-left: auto; }
.card h3 { margin: 0 0 6px; font-size: 16px; line-height: 1.35; }
.when { color: var(--accent); font-weight: 600; font-size: 14px; }
.where, .src { color: var(--muted); font-size: 13px; }
.src { margin-top: 6px; }
.details { margin: 10px 0 0; color: var(--fg); font-size: 14px; white-space: pre-wrap; }
.details[hidden] { display: none; }
.actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
a.btn, button.btn {
  flex: 1 1 auto; min-width: 120px; min-height: 40px; padding: 9px 12px;
  border-radius: 9px; border: 1px solid var(--line); background: var(--chip);
  color: var(--fg); font-size: 14px; text-align: center; text-decoration: none;
  cursor: pointer; display: inline-flex; align-items: center; justify-content: center;
}
a.btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
.empty { color: var(--muted); padding: 40px 0; text-align: center; }
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

function card(ev) {
  const el = document.createElement('article');
  el.className = 'card';

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
  if (ev.source) {
    const s = document.createElement('div');
    s.className = 'src'; s.textContent = ev.source; el.appendChild(s);
  }

  const actions = document.createElement('div');
  actions.className = 'actions';

  if (ev.details) {
    const body = document.createElement('p');
    body.className = 'details'; body.textContent = ev.details; body.hidden = true;
    el.appendChild(body);
    const toggle = document.createElement('button');
    toggle.className = 'btn'; toggle.textContent = 'Details';
    toggle.setAttribute('aria-expanded', 'false');
    toggle.onclick = () => {
      body.hidden = !body.hidden;
      toggle.setAttribute('aria-expanded', String(!body.hidden));
      toggle.textContent = body.hidden ? 'Details' : 'Hide';
    };
    actions.appendChild(toggle);
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
    if (activeSource && ev.platform !== activeSource) return false;
    if (!terms.length) return true;
    const hay = (ev.title + ' ' + ev.location + ' ' + ev.details + ' ' +
                 ev.source + ' ' + ev.date).toLowerCase();
    return terms.every(t => hay.includes(t));
  });
}

function render() {
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

  for (const bucket of BUCKETS) {
    const inBucket = matches.filter(ev => ev.bucket === bucket);
    if (!inBucket.length) continue;
    const h = document.createElement('h2');
    h.className = 'bucket';
    h.textContent = bucket + ' · ' + inBucket.length;
    main.appendChild(h);
    const shelf = document.createElement('div');
    shelf.className = 'shelf';
    for (const ev of inBucket) shelf.appendChild(card(ev));
    main.appendChild(shelf);
  }
}

document.querySelectorAll('.chip').forEach(chip => {
  chip.onclick = () => {
    const value = chip.dataset.source || null;
    activeSource = (activeSource === value) ? null : value;
    document.querySelectorAll('.chip').forEach(c =>
      c.setAttribute('aria-pressed', String(c.dataset.source === activeSource)));
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
}
layoutBtn.onclick = () => setLayout(layoutBtn.dataset.mode === 'carousel' ? 'grid' : 'carousel');
let saved = 'grid';
try { saved = localStorage.getItem('eh-layout') || 'grid'; } catch (e) { /* file:// */ }
setLayout(saved);

search.oninput = render;
sortBy.onchange = render;
render();
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

    platforms = sorted({ev["platform"] for ev in prepared if ev["platform"]})
    # Local, to match the buckets. A header reading "generated 2026-07-31 UTC"
    # above a card headed "TODAY · 2026-07-30" invites exactly the doubt the
    # bucketing fix was about.
    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    counts = source_counts or {}
    summary = ", ".join(f"{name} {n}" for name, n in counts.items() if n) or "no source counts"

    chips = "\n".join(
        f'<button class="chip" type="button" aria-pressed="false" '
        f'data-source="{p}">{p}</button>'
        for p in platforms
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
  <div class="meta">{len(prepared)} event(s) · generated {generated}</div>
  <div class="meta">{summary} · {total_messages} message(s) scanned</div>
</header>
<div class="controls">
  <input id="q" type="search" placeholder="Search events" autocomplete="off"
         aria-label="Search events">
  <div class="chips">{chips}</div>
  <div class="toolbar">
    <label for="sort" class="meta">Sort</label>
    <select id="sort" aria-label="Sort events">
      <option value="date">Date</option>
      <option value="priority">Priority</option>
      <option value="title">Title</option>
    </select>
    <button id="layout" type="button" data-mode="grid"
            aria-label="Toggle grid or carousel layout">Grid</button>
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
        "report.platforms": len(platforms),
    })
    count("report.generated", fmt="html", description="Reports written")
    record("report.size", len(html), unit="By", fmt="html",
           description="Size of a generated report")
    return str(path)
