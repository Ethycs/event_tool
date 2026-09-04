"""The calendar — the same events, arranged by when rather than by rank.

Two views on one page, chosen by a toggle (remembered per-viewer in
``localStorage``): a **month grid** (every day, including empty ones) and a
**week** time grid — day columns over an hour axis, each event a block placed by
its start time, with overlapping events lane-packed side by side and timeless
events in an all-day row. Both read the same filtered projection and share the
day panel, filters, and ICS export; only the layout differs. The week grid
assumes a fixed block length (events carry only a start) and, like the month
grid, is assembled from local ``new Date(y, m, d)`` values — never an ISO string
— so it does not shift a day west of Greenwich.

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
from event_harvester.outputs.nav import NAV_STYLE, base_script, nav_html
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
/* `display` above beats the user-agent `[hidden]` rule, so the Export button
   rendered as a dead "Export" whenever the month held nothing to export.
   Same trap the list page hit and documented; it belongs next to the rule
   that causes it. */
.btn[hidden], button[hidden] { display: none; }
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
/* The two arrows travel together. Left to wrap freely they separated on a
   360px screen — `←` at the end of one line and `→` orphaned on the next. */
.stepper { display: flex; gap: 6px; flex: 0 0 auto; margin-left: auto; }
.nav-btn { padding: 7px 13px; line-height: 1; min-width: 44px; }

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

/* Declared before the media query that reveals it: same specificity, so the
   later rule wins on source order and putting this after would hide the
   button on exactly the screens that need it. It did, for one build. */
#filter-toggle { display: none; }
#filter-toggle.on { background: var(--accent); border-color: var(--accent); color: #fff; }

/* The count badge sits under the day number and was reading as a second date
   — "12" then "30". A filled pill says "this many things", not "the 30th". */
.badge {
  background: var(--accent); color: #fff; border-radius: 999px;
  min-width: 20px; padding: 1px 6px; font-weight: 700; line-height: 1.5;
}

/* A seven-column grid on a phone leaves ~44px per cell — too narrow for a
   title, wide enough for a number and a count. The pills are hidden rather
   than shrunk, and the day panel does the reading. */
@media (max-width: 700px) {
  header { padding: 12px 14px 0; }
  h1 { font-size: 18px; }
  main, .controls { padding-left: 14px; padding-right: 14px; }
  .cell { min-height: 58px; align-items: center; gap: 2px; padding: 4px 2px; }
  .pill, .more { display: none; }
  .badge { display: block; }
  .daynum { font-size: 13px; }
  .grid, .dow { gap: 3px; }
  /* Same measurement as the list page: the filter chips were most of a
     354px control block on a 320px screen, and the grid — the whole point of
     this page — began 611px down, below the fold on every phone tested. */
  #filter-toggle { display: inline-flex; }
  .chips.filter { display: none; }
  body.filters-open .chips.filter { display: flex; }
  .monthbar { margin-top: 12px; }
  .monthbar h2 { flex: 1 1 100%; }
}

/* ── View toggle (Month / Week) ─────────────────────────────────────────── */
.viewseg { display: inline-flex; border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
.viewseg button { border: 0; border-radius: 0; padding: 9px 14px; background: var(--card); }
.viewseg button + button { border-left: 1px solid var(--line); }
.viewseg button[aria-pressed="true"] { background: var(--accent); color: #fff; }
.viewseg button:hover { border-color: transparent; }

/* ── Week view ──────────────────────────────────────────────────────────── */
/* `.grid`/`.dow` set `display: grid`, which beats the user-agent `[hidden]`
   rule — the same trap the Export button hit above — so switching to the week
   view would leave the month grid showing underneath. Hide them explicitly. */
.grid[hidden], .dow[hidden] { display: none; }
#weekwrap[hidden] { display: none; }
/* One horizontal scroller so a 7-column time grid stays usable on a phone;
   the inner min-width keeps the head, all-day row and body columns aligned. */
#weekwrap { overflow-x: auto; }
.weekinner { min-width: 660px; }
.weekhead, .weekallday, .weekgrid { display: grid; grid-template-columns: 52px repeat(7, 1fr); }
.weekhead .wh {
  text-align: center; font-size: 12px; color: var(--muted); padding: 5px 0 6px;
  border-radius: 8px 8px 0 0; border-bottom: 2px solid transparent;
}
.weekhead .wh .wd { font-weight: 700; text-transform: uppercase; letter-spacing: .03em; }
.weekhead .wh .wn { display: block; font-size: 17px; font-weight: 700; color: var(--fg); }
.weekhead .wh.today { color: var(--accent); border-bottom-color: var(--accent); background: var(--today); }
.weekallday {
  border-top: 1px solid var(--line); border-bottom: 1px solid var(--line);
  min-height: 28px; align-items: stretch;
}
.weekallday .adlabel {
  font-size: 10px; color: var(--muted); text-transform: uppercase;
  padding: 5px 6px 0 0; text-align: right; letter-spacing: .03em;
}
.weekallday .adcell {
  padding: 3px; display: flex; flex-direction: column; gap: 3px;
  border-left: 1px solid var(--line); min-height: 28px;
}
.weekallday .adcell.today { background: var(--today); }
.weekbody { max-height: 66vh; overflow-y: auto; }
.tglabel { height: 44px; font-size: 11px; color: var(--muted); text-align: right; padding-right: 6px; }
.tglabel span { position: relative; top: -7px; }
.daycol {
  position: relative; border-left: 1px solid var(--line);
  background-image: repeating-linear-gradient(
    to bottom, var(--line) 0, var(--line) 1px, transparent 1px, transparent 44px);
}
.daycol.today { background-color: var(--today); }
.wevent {
  position: absolute; text-align: left; overflow: hidden; cursor: pointer;
  border-radius: 6px; padding: 2px 5px; font-size: 11px; line-height: 1.25;
  background: var(--accent); color: #fff; border: 1px solid rgba(0,0,0,.18);
  box-shadow: var(--shadow);
}
.wevent:hover { filter: brightness(1.08); z-index: 5; }
.wevent .wt { font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.wevent .wn { opacity: .95; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.weekchip {
  font-size: 11px; padding: 3px 7px; border-radius: 999px; text-align: left;
  background: var(--chip); color: var(--fg); border: 1px solid var(--line);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; cursor: pointer;
}
.weekchip:hover { border-color: var(--accent); }

/* Last in the file on purpose — same specificity as the base rules, so an
   earlier position loses on source order and only on a touch device. */
@media (pointer: coarse) {
  .chip { min-height: 40px; padding: 8px 13px; }
  button, .btn, input[type=search] { min-height: 44px; }
  .cell { min-height: 62px; }
  .ev .actions .btn { min-height: 40px; }
  .viewseg button { min-height: 44px; }
}
"""

_SCRIPT = ICS_JS + r"""
const DATA = JSON.parse(document.getElementById('events-data').textContent);
const pad = (n) => String(n).padStart(2, '0');
// The only correct way to name a day here. `toISOString()` would convert to UTC
// first and shift the whole grid back a day for anyone west of Greenwich.
const keyOf = (y, m, d) => y + '-' + pad(m + 1) + '-' + pad(d);

// **The browser's date, not the build date.** This used to read
// `document.body.dataset.today` on the reasoning that the outlined cell should
// agree with the "generated" line in the header. It does not survive contact
// with how the page is served: it is rebuilt on a schedule — every 24h by
// default — and served unchanged in between, so a page built at 16:46 on the
// 18th went on outlining the 18th for the whole of the 19th. The header still
// says when it was generated, which is where a build time belongs; the
// outlined cell is a claim about *today*, and only the viewer's clock knows
// that. `data-today` stays as the fallback.
function localToday() {
  const n = new Date();
  return keyOf(n.getFullYear(), n.getMonth(), n.getDate());
}
let TODAY = localToday() || document.body.dataset.today;
const MONTHS = ['January','February','March','April','May','June','July',
                'August','September','October','November','December'];

const $ = (id) => document.getElementById(id);
const search = $('q');
let activeSource = null;
let activeRegion = null;
let picked = null;               // a 'YYYY-MM-DD' key, or 'undated', or null

// A single local-midnight cursor anchors both views: the month view shows the
// cursor's month, the week view shows the cursor's week. Stepping is done on it.
let cursor = new Date(Number(TODAY.slice(0, 4)), Number(TODAY.slice(5, 7)) - 1,
                      Number(TODAY.slice(8, 10)));
// Month or week. Remembered per-viewer; storage may throw (private mode), so
// every read/write is guarded and falls back to 'month'.
let view = 'month';
try { if (localStorage.getItem('cal-view') === 'week') view = 'week'; } catch (e) {}

const HOURPX = 44;          // px per hour in the week grid; matches the CSS line spacing
const WEEK_DUR = 75;        // assumed event length (min) — events carry only a start
const WDOW = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];

// Local-midnight start (Sunday) of the week containing `d`.
function weekStartOf(d) {
  const s = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  s.setDate(s.getDate() - s.getDay());
  return s;
}
// The seven 'YYYY-MM-DD' keys of the week containing `d`.
function weekKeys(d) {
  const s = weekStartOf(d);
  const out = [];
  for (let i = 0; i < 7; i++) {
    const x = new Date(s.getFullYear(), s.getMonth(), s.getDate() + i);
    out.push(keyOf(x.getFullYear(), x.getMonth(), x.getDate()));
  }
  return out;
}
function hourLabel(h) {
  const ap = h < 12 || h === 24 ? 'am' : 'pm';
  const hh = h % 12 === 0 ? 12 : h % 12;
  return hh + ap;
}

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

  // Only one view's DOM is shown at a time; both read the same filtered map.
  $('grid').hidden = view === 'week';
  $('dow').hidden = view === 'week';
  $('weekwrap').hidden = view !== 'week';

  if (view === 'week') renderWeek(events, map);
  else renderMonth(events, map);

  // The undated bucket belongs to neither day nor hour, so it is a button in
  // both views rather than a cell.
  const undated = (map.get('undated') || []).length;
  const btn = $('undated');
  btn.hidden = !undated;
  btn.textContent = undated + ' undated';
  btn.setAttribute('aria-pressed', String(picked === 'undated'));

  renderDay(map);
  renderExport(events);
}

function renderMonth(events, map) {
  const viewY = cursor.getFullYear(), viewM = cursor.getMonth();
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

  $('count').textContent = inMonth + ' this month · ' + events.length + ' total';
}

// Split a day's timed events into non-overlapping lanes so concurrent events sit
// side by side instead of on top of each other. Each event is placed in the
// first lane whose previous event has already ended; a fresh lane opens only
// when none has. Returns the placements and the lane count (the block width
// denominator). Events an hour apart reuse one lane, so a quiet day is full
// width and only genuine overlaps narrow.
function packLanes(evs) {
  const laneEnd = [];
  const placed = [];
  for (const ev of evs) {
    const s = minutes(ev.time), e = s + WEEK_DUR;
    let lane = laneEnd.findIndex(end => end <= s);
    if (lane < 0) { lane = laneEnd.length; laneEnd.push(e); }
    else laneEnd[lane] = e;
    placed.push({ ev, lane, s, e });
  }
  return { placed, lanes: Math.max(1, laneEnd.length) };
}

function renderWeek(events, map) {
  const keys = weekKeys(cursor);
  const start = weekStartOf(cursor);
  const days = keys.map((k, i) => new Date(start.getFullYear(), start.getMonth(),
                                           start.getDate() + i));

  // Label the span, e.g. "Aug 31 – Sep 6, 2026".
  const a = days[0], b = days[6];
  const fmt = (d) => MONTHS[d.getMonth()].slice(0, 3) + ' ' + d.getDate();
  $('month-label').textContent = fmt(a) + ' – ' + fmt(b) + ', ' + b.getFullYear();

  // Which hours to show: cover the week's timed events, default 8am–10pm, and
  // never taller than the full day. Undated-time events go to the all-day row.
  let minM = 8 * 60, maxM = 22 * 60;
  for (const k of keys) {
    for (const ev of (map.get(k) || [])) {
      const m = minutes(ev.time);
      if (m >= 1e6) continue;
      minM = Math.min(minM, m);
      maxM = Math.max(maxM, m + WEEK_DUR);
    }
  }
  const startH = Math.max(0, Math.floor(minM / 60));
  const endH = Math.min(24, Math.max(startH + 6, Math.ceil(maxM / 60)));
  const bodyH = (endH - startH) * HOURPX;

  const wrap = $('weekwrap');
  wrap.textContent = '';
  const inner = document.createElement('div');
  inner.className = 'weekinner';

  // ── Header row: day name + date, today highlighted ──
  const head = document.createElement('div');
  head.className = 'weekhead';
  head.appendChild(document.createElement('div'));            // corner over the gutter
  days.forEach((d, i) => {
    const wh = document.createElement('div');
    wh.className = 'wh' + (keys[i] === TODAY ? ' today' : '');
    const wd = document.createElement('span');
    wd.className = 'wd'; wd.textContent = WDOW[d.getDay()];
    const wn = document.createElement('span');
    wn.className = 'wn'; wn.textContent = d.getDate();
    wh.appendChild(wd); wh.appendChild(wn);
    head.appendChild(wh);
  });
  inner.appendChild(head);

  // ── All-day / timeless row ──
  const allday = document.createElement('div');
  allday.className = 'weekallday';
  const adlabel = document.createElement('div');
  adlabel.className = 'adlabel'; adlabel.textContent = 'all-day';
  allday.appendChild(adlabel);
  let anyAllDay = false;
  keys.forEach((k, i) => {
    const cell = document.createElement('div');
    cell.className = 'adcell' + (k === TODAY ? ' today' : '');
    for (const ev of (map.get(k) || [])) {
      if (minutes(ev.time) < 1e6) continue;
      anyAllDay = true;
      const chip = document.createElement('button');
      chip.type = 'button'; chip.className = 'weekchip';
      chip.textContent = ev.title;
      chip.onclick = () => { picked = k; render(); };
      cell.appendChild(chip);
    }
    allday.appendChild(cell);
  });
  allday.hidden = !anyAllDay;
  inner.appendChild(allday);

  // ── Time grid: gutter of hour labels + seven positioned day columns ──
  const body = document.createElement('div');
  body.className = 'weekbody';
  const grid = document.createElement('div');
  grid.className = 'weekgrid';

  const gutter = document.createElement('div');
  gutter.className = 'timegutter';
  gutter.style.height = bodyH + 'px';
  for (let h = startH; h < endH; h++) {
    const lab = document.createElement('div');
    lab.className = 'tglabel';
    const s = document.createElement('span');
    s.textContent = hourLabel(h);
    lab.appendChild(s);
    gutter.appendChild(lab);
  }
  grid.appendChild(gutter);

  keys.forEach((k) => {
    const col = document.createElement('div');
    col.className = 'daycol' + (k === TODAY ? ' today' : '');
    col.style.height = bodyH + 'px';
    const timed = (map.get(k) || []).filter(ev => minutes(ev.time) < 1e6);
    const { placed, lanes } = packLanes(timed);
    for (const p of placed) {
      const el = document.createElement('button');
      el.type = 'button'; el.className = 'wevent';
      el.style.top = ((p.s - startH * 60) / 60 * HOURPX) + 'px';
      el.style.height = Math.max(20, (p.e - p.s) / 60 * HOURPX - 2) + 'px';
      el.style.left = 'calc(' + (p.lane * 100 / lanes) + '% + 1px)';
      el.style.width = 'calc(' + (100 / lanes) + '% - 3px)';
      const t = document.createElement('div');
      t.className = 'wt'; t.textContent = p.ev.time;
      const n = document.createElement('div');
      n.className = 'wn'; n.textContent = p.ev.title;
      el.appendChild(t); el.appendChild(n);
      el.onclick = () => { picked = k; render(); };
      col.appendChild(el);
    }
    grid.appendChild(col);
  });

  body.appendChild(grid);
  inner.appendChild(body);
  wrap.appendChild(inner);

  const wk = keys.reduce((acc, k) => acc + (map.get(k) || []).length, 0);
  $('count').textContent = wk + ' this week · ' + events.length + ' total';
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
  let scope, label;
  if (view === 'week') {
    const keys = new Set(weekKeys(cursor));
    scope = events.filter(ev => ev.date && keys.has(ev.date));
    label = 'week of ' + weekKeys(cursor)[0];
  } else {
    const prefix = keyOf(cursor.getFullYear(), cursor.getMonth(), 1).slice(0, 7);
    scope = events.filter(ev => ev.date && ev.date.slice(0, 7) === prefix);
    label = MONTHS[cursor.getMonth()] + ' ' + cursor.getFullYear();
  }
  const link = $('export');
  const href = icsForMany(scope);
  link.hidden = !href;
  if (!href) return;
  link.href = href;
  link.setAttribute('download', icsName(label));
  link.textContent = 'Export ' + scope.length + ' to calendar';
}

function step(delta) {
  if (view === 'week') cursor.setDate(cursor.getDate() + 7 * delta);
  else cursor = new Date(cursor.getFullYear(), cursor.getMonth() + delta, 1);
  render();
}

function toToday() {
  cursor = new Date(Number(TODAY.slice(0, 4)), Number(TODAY.slice(5, 7)) - 1,
                    Number(TODAY.slice(8, 10)));
  picked = TODAY;
  render();
}

function setView(v) {
  view = v;
  try { localStorage.setItem('cal-view', v); } catch (e) {}
  $('view-month').setAttribute('aria-pressed', String(v === 'month'));
  $('view-week').setAttribute('aria-pressed', String(v === 'week'));
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
      syncFilterToggle();
      render();
    };
  }
}

// Collapsed on a phone, so the button carries what the chips would have said —
// "Oakland · web" rather than a count. Which filter is on is the thing you
// cannot otherwise see.
const filterToggle = $('filter-toggle');

function activeChipLabel(attr) {
  const chip = document.querySelector(
    '.chip[data-' + attr + '][aria-pressed="true"]');
  if (!chip || !chip.dataset[attr]) return null;
  const copy = chip.cloneNode(true);
  const tally = copy.querySelector('.tally');
  if (tally) tally.remove();
  return copy.textContent.trim();
}

function syncFilterToggle() {
  const parts = [activeChipLabel('region'), activeChipLabel('source')].filter(Boolean);
  filterToggle.textContent = parts.length ? parts.join(' · ') : 'Filters';
  filterToggle.classList.toggle('on', parts.length > 0);
}

filterToggle.onclick = () => {
  const open = document.body.classList.toggle('filters-open');
  filterToggle.setAttribute('aria-expanded', String(open));
};

$('prev').onclick = () => step(-1);
$('next').onclick = () => step(1);
$('today-btn').onclick = toToday;
$('view-month').onclick = () => setView('month');
$('view-week').onclick = () => setView('week');
// Reflect the remembered view before the first paint.
$('view-month').setAttribute('aria-pressed', String(view === 'month'));
$('view-week').setAttribute('aria-pressed', String(view === 'week'));
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

// A tab left open overnight — the phone in a pocket, reopened next morning —
// has to notice the day turned over. Re-render only when it actually has;
// `render()` rebuilds all 42 cells, and doing that every minute for nothing
// would fight with a click on a day cell.
function checkRollover() {
  const now = localToday();
  if (now === TODAY) return;
  TODAY = now;
  render();
}

setInterval(checkRollover, 60000);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) checkRollover();
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
{base_script("calendar" if nav else "")}
<title>{escape(title)} — {dated}</title>
<style>{_STYLE}{NAV_STYLE if nav else ""}</style>
</head>
<body data-today="{today.isoformat()}">
<header>
  {nav_html("calendar" if nav else "")}
  <h1>{escape(title)}</h1>
  <div class="meta">{dated} dated of {len(prepared)} event(s) · generated
    {generated}</div>
</header>
<div class="controls">
  <div class="bar">
    <input id="q" type="search" placeholder="Search events, venues, people"
           autocomplete="off" aria-label="Search events">
    <div class="viewseg" role="group" aria-label="Calendar view">
      <button id="view-month" type="button" aria-pressed="true">Month</button>
      <button id="view-week" type="button" aria-pressed="false">Week</button>
    </div>
    <button id="today-btn" type="button">Today</button>
    <button id="filter-toggle" type="button" aria-expanded="false">Filters</button>
    <button id="undated" type="button" aria-pressed="false" hidden></button>
    <a id="export" class="btn" hidden>Export</a>
  </div>
  <div class="chips filter" role="group" aria-label="Filter by place">
    <button class="chip" type="button" aria-pressed="true"
            data-region="">Everywhere<span class="tally">{len(prepared)}</span></button>
    {region_chips}
  </div>
  <div class="chips filter" role="group" aria-label="Filter by source">
    <button class="chip" type="button" aria-pressed="true"
            data-source="">All<span class="tally">{len(prepared)}</span></button>
    {chips}
  </div>
</div>
<main>
  <div class="monthbar">
    <h2 id="month-label"></h2>
    <span class="meta" id="count"></span>
    <span class="stepper">
      <button class="nav-btn" id="prev" type="button" aria-label="Previous">&larr;</button>
      <button class="nav-btn" id="next" type="button" aria-label="Next">&rarr;</button>
    </span>
  </div>
  <div class="dow" id="dow" aria-hidden="true">{dow}</div>
  <div class="grid" id="grid" role="grid" aria-label="Month"></div>
  <div id="weekwrap" hidden></div>
  <section id="day" aria-live="polite"></section>
</main>
<footer>Generated by event-harvester. Arrow keys change the range, T jumps to
today. This file contains the event data — forwarding it forwards them.</footer>
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
