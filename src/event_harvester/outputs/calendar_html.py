"""The month grid — the same events, arranged by when rather than by rank.

**Why a second page and not a toggle on the first.** The event list answers
"what is there?" and sorts by relevance; a calendar answers "what is on
Thursday?" and cannot sort at all, because position *is* the date. They want
opposite things from the same data: the list wants the best events first and is
happy to hide the rest behind a filter, while the grid must show every day
including the empty ones, or it is not a calendar. Merging them would have meant
a mode flag threaded through rendering, sorting, filtering and the URL. Two
pages over one projection is less code and neither compromises.

It is built from `html_report._prepare`, so both pages see byte-identical event
records. If a field appears on a card it is available here for free, and if the
projection drops something neither page can show it — which is the property that
keeps them from drifting.

**Dates are local wall-clock, and the grid is where that bites hardest.**
`new Date('2026-08-12')` parses as **UTC midnight** and renders as the 11th
anywhere west of Greenwich — which is everywhere this is used. So the grid is
built with `new Date(y, m, d)` (local) and day keys are assembled by string
formatting, never `toISOString()`. This is the same defect that once put every
event tonight in "Past" from 5pm PDT onward; it is worth stating twice because
the calendar renders 42 cells and would have been wrong in all of them.

**No thumbnails.** The list page embeds 95 inline JPEGs — 1276 KB of its
1562 KB — and a grid cell is 96px tall with room for three lines of text, so it
could not show one usefully anyway. Dropping them puts this page at **247 KB
for the same 240 events**, measured 2026-08-12. It is the one to open on a
phone.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime
from html import escape
from pathlib import Path

from event_harvester.observability.metrics import count, record
from event_harvester.observability.tracing import set_span_attrs, traced
from event_harvester.outputs.html_report import _prepare, _safe_json
from event_harvester.outputs.ics import ICS_JS
from event_harvester.outputs.nav import NAV_STYLE, nav_html
from event_harvester.outputs.regions import HINTS as REGION_HINTS
from event_harvester.outputs.regions import LABELS as REGION_LABELS
from event_harvester.outputs.regions import REGIONS

_TRACER = "event_harvester.outputs.calendar_html"
logger = logging.getLogger(_TRACER)

_STYLE = """
:root {
  --bg: #ffffff; --fg: #16181d; --muted: #5b6472; --line: #e4e7ec;
  --card: #ffffff; --accent: #7c3aed; --chip: #f2f4f7;
  --today: #fef3c7; --weekend: #fafbfc; --shadow: 0 1px 2px rgba(16,24,40,.06);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1115; --fg: #e8eaed; --muted: #98a2b3; --line: #242832;
    --card: #171a21; --accent: #a78bfa; --chip: #212530;
    --today: #3b2f0b; --weekend: #13161c; --shadow: none;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  -webkit-text-size-adjust: 100%;
}
header { padding: 16px 16px 0; max-width: 1100px; margin: 0 auto; }
h1 { font-size: 20px; margin: 0 0 2px; }
.meta { color: var(--muted); font-size: 13px; }
main { max-width: 1100px; margin: 0 auto; padding: 12px 16px 48px; }

.controls { max-width: 1100px; margin: 0 auto; padding: 10px 16px 0; }
.bar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
#q { flex: 1 1 200px; min-width: 0; }
input[type=search], select, button, .btn {
  font: inherit; color: var(--fg); background: var(--card);
  border: 1px solid var(--line); border-radius: 10px; padding: 9px 12px;
  cursor: pointer;
}
input[type=search] { cursor: text; }
button:hover, .btn:hover { border-color: var(--accent); }
.btn { text-decoration: none; display: inline-block; }
.btn.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
.chips { display: flex; gap: 6px; flex-wrap: wrap; margin: 10px 0 0; }
.chip {
  border-radius: 999px; padding: 5px 12px; font-size: 13px; font-weight: 600;
  background: var(--chip); border: 1px solid transparent; color: var(--muted);
}
.chip[aria-pressed="true"] {
  background: var(--accent); color: #fff; border-color: var(--accent);
}
.tally { opacity: .7; margin-left: 6px; font-weight: 500; }

.monthbar {
  display: flex; align-items: center; gap: 8px; margin: 16px 0 8px; flex-wrap: wrap;
}
.monthbar h2 { font-size: 18px; margin: 0; flex: 1 1 auto; }
.nav-btn { padding: 7px 13px; line-height: 1; }

.dow {
  display: grid; grid-template-columns: repeat(7, 1fr); gap: 4px;
  color: var(--muted); font-size: 12px; font-weight: 700;
  text-transform: uppercase; letter-spacing: .04em; padding: 0 0 4px;
}
.dow div { text-align: center; }
.grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 4px; }
.cell {
  background: var(--card); border: 1px solid var(--line); border-radius: 8px;
  min-height: 96px; padding: 5px 5px 6px; text-align: left; overflow: hidden;
  display: flex; flex-direction: column; gap: 3px; cursor: default;
  box-shadow: var(--shadow);
}
.cell.has { cursor: pointer; }
.cell.has:hover { border-color: var(--accent); }
.cell.weekend { background: var(--weekend); }
.cell.other { opacity: .42; }
.cell.today { border-color: var(--accent); border-width: 2px; }
.cell.picked { outline: 2px solid var(--accent); outline-offset: -2px; }
.daynum { font-size: 12px; font-weight: 700; color: var(--muted); }
.cell.today .daynum { color: var(--accent); }
.pill {
  font-size: 11.5px; line-height: 1.3; padding: 2px 5px; border-radius: 5px;
  background: var(--chip); color: var(--fg);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.pill b { font-weight: 700; color: var(--accent); margin-right: 4px; }
.more { font-size: 11px; color: var(--muted); font-weight: 600; }
.badge { display: none; font-size: 11px; font-weight: 700; color: var(--accent); }

#day { margin-top: 20px; }
#day h3 { font-size: 16px; margin: 0 0 10px; }
.ev {
  background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 12px 14px; margin-bottom: 8px;
}
.ev h4 { margin: 0 0 3px; font-size: 15px; }
.ev .when { color: var(--accent); font-weight: 600; font-size: 13px; }
.ev .where, .ev .src { color: var(--muted); font-size: 13px; }
.ev .actions { display: flex; gap: 6px; margin-top: 9px; flex-wrap: wrap; }
.ev .actions .btn { padding: 6px 11px; font-size: 13px; border-radius: 8px; }
.empty { color: var(--muted); font-size: 14px; }
footer {
  max-width: 1100px; margin: 0 auto; padding: 0 16px 32px;
  color: var(--muted); font-size: 12px;
}

/* A seven-column grid on a phone leaves ~44px per cell — too narrow for a
   title, wide enough for a number and a count. The pills are hidden rather
   than shrunk, and the day panel does the reading. */
@media (max-width: 700px) {
  .cell { min-height: 58px; align-items: center; }
  .pill, .more { display: none; }
  .badge { display: block; }
  .daynum { font-size: 13px; }
}
"""

_SCRIPT = ICS_JS + r"""
const DATA = JSON.parse(document.getElementById('events-data').textContent);
// The build date, not the browser's. They agree in practice, but the outlined
// cell should match the "generated" line in the header even for a page opened
// tomorrow from disk.
const TODAY = document.body.dataset.today;
const MONTHS = ['January','February','March','April','May','June','July',
                'August','September','October','November','December'];

const $ = (id) => document.getElementById(id);
const search = $('q');
let activeSource = null;
let activeRegion = null;
let picked = null;               // a 'YYYY-MM-DD' key, or 'undated', or null

// The view month, held as plain numbers rather than a Date so that stepping
// past December is arithmetic instead of timezone-sensitive.
let viewY = Number(TODAY.slice(0, 4));
let viewM = Number(TODAY.slice(5, 7)) - 1;

const pad = (n) => String(n).padStart(2, '0');
// The only correct way to name a day here. `toISOString()` would convert to UTC
// first and shift the whole grid back a day for anyone west of Greenwich.
const keyOf = (y, m, d) => y + '-' + pad(m + 1) + '-' + pad(d);

function matching() {
  const q = search.value.trim().toLowerCase();
  const terms = q ? q.split(/\s+/) : [];
  return DATA.filter(ev => {
    if (activeSource && ev.platform !== activeSource) return false;
    if (activeRegion && ev.region !== activeRegion) return false;
    if (!terms.length) return true;
    const hay = (ev.title + ' ' + ev.location + ' ' + ev.details + ' ' +
                 ev.source + ' ' + ev.date).toLowerCase();
    return terms.every(t => hay.includes(t));
  });
}

// Minutes since midnight, for ordering a day's events. Anything unparseable
// sorts last rather than at 00:00, so "8pm, 10pm, time unknown" reads in the
// order someone would actually use it.
function minutes(t) {
  const m = String(t || '').match(/(\d{1,2})(?::(\d{2}))?\s*(am|pm)?/i);
  if (!m) return 1e6;
  let h = parseInt(m[1], 10);
  const ap = (m[3] || '').toLowerCase();
  if (ap === 'pm' && h < 12) h += 12;
  if (ap === 'am' && h === 12) h = 0;
  return h * 60 + parseInt(m[2] || '0', 10);
}

function byDay(events) {
  const map = new Map();
  for (const ev of events) {
    const k = ev.date || 'undated';
    if (!map.has(k)) map.set(k, []);
    map.get(k).push(ev);
  }
  for (const list of map.values()) list.sort((a, b) => minutes(a.time) - minutes(b.time));
  return map;
}

function render() {
  const events = matching();
  const map = byDay(events);

  $('month-label').textContent = MONTHS[viewM] + ' ' + viewY;

  const first = new Date(viewY, viewM, 1);
  const lead = first.getDay();                       // 0 = Sunday
  const days = new Date(viewY, viewM + 1, 0).getDate();
  // Always six rows. A grid that changes height as you page through months
  // makes the buttons move under the cursor.
  const cells = 42;
  const grid = $('grid');
  grid.textContent = '';

  let inMonth = 0;
  for (let i = 0; i < cells; i++) {
    const offset = i - lead;
    const d = new Date(viewY, viewM, offset + 1);
    const key = keyOf(d.getFullYear(), d.getMonth(), d.getDate());
    const here = offset >= 0 && offset < days;
    const list = map.get(key) || [];
    if (here) inMonth += list.length;

    const cell = document.createElement(list.length ? 'button' : 'div');
    cell.className = 'cell' +
      (here ? '' : ' other') +
      ([0, 6].includes(d.getDay()) ? ' weekend' : '') +
      (key === TODAY ? ' today' : '') +
      (list.length ? ' has' : '') +
      (key === picked ? ' picked' : '');
    if (list.length) {
      cell.type = 'button';
      cell.onclick = () => { picked = (picked === key ? null : key); render(); };
      cell.setAttribute('aria-label',
        list.length + ' event(s) on ' + key);
    }

    const num = document.createElement('div');
    num.className = 'daynum';
    num.textContent = d.getDate();
    cell.appendChild(num);

    for (const ev of list.slice(0, 3)) {
      const pill = document.createElement('div');
      pill.className = 'pill';
      if (ev.time) {
        const b = document.createElement('b');
        b.textContent = ev.time;
        pill.appendChild(b);
      }
      pill.appendChild(document.createTextNode(ev.title));   // never innerHTML
      cell.appendChild(pill);
    }
    if (list.length > 3) {
      const more = document.createElement('div');
      more.className = 'more';
      more.textContent = '+' + (list.length - 3) + ' more';
      cell.appendChild(more);
    }
    if (list.length) {
      const badge = document.createElement('div');
      badge.className = 'badge';
      badge.textContent = list.length;
      cell.appendChild(badge);
    }
    grid.appendChild(cell);
  }

  const undated = (map.get('undated') || []).length;
  const btn = $('undated');
  btn.hidden = !undated;
  btn.textContent = undated + ' undated';
  btn.setAttribute('aria-pressed', String(picked === 'undated'));

  $('count').textContent = inMonth + ' this month · ' + events.length + ' total';
  renderDay(map);
  renderExport(events);
}

function renderDay(map) {
  const panel = $('day');
  panel.textContent = '';
  if (!picked) return;

  const list = map.get(picked) || [];
  const h = document.createElement('h3');
  h.textContent = picked === 'undated'
    ? 'No date given — ' + list.length + ' event(s)'
    : longDate(picked) + ' — ' + list.length + ' event(s)';
  panel.appendChild(h);

  if (!list.length) {
    const p = document.createElement('p');
    p.className = 'empty';
    p.textContent = 'Nothing here with the current filters.';
    panel.appendChild(p);
    return;
  }
  for (const ev of list) panel.appendChild(eventCard(ev));
  panel.scrollIntoView({behavior: 'smooth', block: 'nearest'});
}

// Parsed by hand into local numbers, for the same reason keyOf exists.
function longDate(key) {
  const [y, m, d] = key.split('-').map(Number);
  return new Date(y, m - 1, d).toLocaleDateString(undefined,
    {weekday: 'long', month: 'long', day: 'numeric'});
}

function eventCard(ev) {
  const el = document.createElement('article');
  el.className = 'ev';

  const h = document.createElement('h4');
  h.textContent = ev.title;
  el.appendChild(h);

  if (ev.time) {
    const w = document.createElement('div');
    w.className = 'when'; w.textContent = ev.time; el.appendChild(w);
  }
  if (ev.location) {
    const w = document.createElement('div');
    w.className = 'where'; w.textContent = ev.location; el.appendChild(w);
  }
  const who = ev.author || ev.source;
  if (who) {
    const s = document.createElement('div');
    s.className = 'src';
    s.textContent = ev.platform ? who + ' · ' + ev.platform : who;
    el.appendChild(s);
  }

  const actions = document.createElement('div');
  actions.className = 'actions';
  const ics = icsFor(ev);
  if (ics) {
    const a = document.createElement('a');
    a.className = 'btn primary'; a.href = ics;
    a.setAttribute('download', icsName(ev.title));
    a.textContent = 'Add to calendar';
    actions.appendChild(a);
  }
  if (ev.link) {
    const a = document.createElement('a');
    a.className = 'btn'; a.href = ev.link;
    a.target = '_blank'; a.rel = 'noopener noreferrer';
    a.textContent = 'Open';
    actions.appendChild(a);
  }
  if (actions.children.length) el.appendChild(actions);
  return el;
}

// One .ics for everything visible this month. Thirty separate downloads is not
// an import; this is the button that makes the page worth opening on a laptop.
function renderExport(events) {
  const prefix = keyOf(viewY, viewM, 1).slice(0, 7);
  const month = events.filter(ev => ev.date && ev.date.slice(0, 7) === prefix);
  const link = $('export');
  const href = icsForMany(month);
  link.hidden = !href;
  if (!href) return;
  link.href = href;
  link.setAttribute('download', icsName(MONTHS[viewM] + ' ' + viewY));
  link.textContent = 'Export ' + month.length + ' to calendar';
}

function step(delta) {
  const d = new Date(viewY, viewM + delta, 1);
  viewY = d.getFullYear(); viewM = d.getMonth();
  render();
}

function toToday() {
  viewY = Number(TODAY.slice(0, 4));
  viewM = Number(TODAY.slice(5, 7)) - 1;
  picked = TODAY;
  render();
}

function bindChips(attr, set) {
  const buttons = document.querySelectorAll('[data-' + attr + ']');
  for (const b of buttons) {
    b.onclick = () => {
      const value = b.dataset[attr] || null;
      set(value);
      for (const other of buttons) {
        other.setAttribute('aria-pressed', String(other === b));
      }
      render();
    };
  }
}

$('prev').onclick = () => step(-1);
$('next').onclick = () => step(1);
$('today-btn').onclick = toToday;
$('undated').onclick = () => {
  picked = (picked === 'undated' ? null : 'undated');
  render();
};
search.oninput = () => { render(); };
bindChips('source', v => { activeSource = v; });
bindChips('region', v => { activeRegion = v; });

// Arrow keys page the month, but not while someone is typing in the search box.
document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
  if (e.key === 'ArrowLeft') step(-1);
  else if (e.key === 'ArrowRight') step(1);
  else if (e.key.toLowerCase() === 't') toToday();
});

render();
"""

_DOW = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


@traced("report.calendar", tracer_name=_TRACER)
def generate_calendar_html(
    validated_events: list[dict],
    output_path: str = "events_calendar.html",
    title: str = "Calendar",
    today: date | None = None,
    nav: bool = False,
) -> str:
    """Write the self-contained month-grid page. Returns the path.

    Same contract as `generate_html_report`: one file, no network, untrusted
    text rendered through `textContent` only. ``today`` is the **local** date
    and decides which month opens and which cell is outlined; see the module
    docstring for why that is not a detail.
    """
    today = today or datetime.now().astimezone().date()
    prepared = _prepare(validated_events or [], today)
    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")

    dated = sum(1 for ev in prepared if ev["date"])
    tallies = Counter(ev["platform"] for ev in prepared if ev["platform"])
    chips = "\n".join(
        f'<button class="chip" type="button" aria-pressed="false" '
        f'data-source="{escape(p, quote=True)}">{escape(p)}'
        f'<span class="tally">{tallies[p]}</span></button>'
        for p, _ in tallies.most_common()
    )
    region_counts = Counter(ev["region"] for ev in prepared)
    region_chips = "\n".join(
        f'<button class="chip" type="button" aria-pressed="false" '
        f'data-region="{r}" title="{escape(REGION_HINTS[r], quote=True)}">'
        f'{escape(REGION_LABELS[r])}'
        f'<span class="tally">{region_counts[r]}</span></button>'
        for r in REGIONS if region_counts[r]
    )
    dow = "".join(f"<div>{d}</div>" for d in _DOW)

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>{escape(title)} — {dated}</title>
<style>{_STYLE}{NAV_STYLE if nav else ""}</style>
</head>
<body data-today="{today.isoformat()}">
<header>
  {nav_html("/calendar" if nav else "")}
  <h1>{escape(title)}</h1>
  <div class="meta">{dated} dated of {len(prepared)} event(s) · generated
    {generated}</div>
</header>
<div class="controls">
  <div class="bar">
    <input id="q" type="search" placeholder="Search events, venues, people"
           autocomplete="off" aria-label="Search events">
    <button id="today-btn" type="button">Today</button>
    <button id="undated" type="button" aria-pressed="false" hidden></button>
    <a id="export" class="btn" hidden>Export</a>
  </div>
  <div class="chips" role="group" aria-label="Filter by place">
    <button class="chip" type="button" aria-pressed="true"
            data-region="">Everywhere<span class="tally">{len(prepared)}</span></button>
    {region_chips}
  </div>
  <div class="chips" role="group" aria-label="Filter by source">
    <button class="chip" type="button" aria-pressed="true"
            data-source="">All<span class="tally">{len(prepared)}</span></button>
    {chips}
  </div>
</div>
<main>
  <div class="monthbar">
    <h2 id="month-label"></h2>
    <span class="meta" id="count"></span>
    <button class="nav-btn" id="prev" type="button" aria-label="Previous month">&larr;</button>
    <button class="nav-btn" id="next" type="button" aria-label="Next month">&rarr;</button>
  </div>
  <div class="dow" aria-hidden="true">{dow}</div>
  <div class="grid" id="grid" role="grid" aria-label="Month"></div>
  <section id="day" aria-live="polite"></section>
</main>
<footer>Generated by event-harvester. Arrow keys change month, T jumps to today.
This file contains the event data — forwarding it forwards them.</footer>
<script type="application/json" id="events-data">{_safe_json(prepared)}</script>
<script>{_SCRIPT}</script>
</body>
</html>
"""

    path = Path(output_path)
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")

    set_span_attrs(**{
        "calendar.events": len(prepared),
        "calendar.dated": dated,
        "calendar.bytes": len(html),
    })
    count("report.generated", fmt="calendar", description="Reports written")
    record("report.size", len(html), unit="By", fmt="calendar",
           description="Size of a generated report")
    return str(path)
