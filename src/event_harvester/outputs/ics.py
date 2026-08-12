"""The iCalendar builder both browser pages share, as JavaScript source.

Python emits no `.ics` here. The events pages are self-contained files with no
server behind them, so the calendar file has to be assembled in the browser and
handed to the user as a `data:` URI — there is no endpoint to fetch it from.

This lives in its own module because there are now two pages that need it — the
event list (`html_report.py`) and the month grid (`calendar_html.py`) — and the
second one needs something the first does not: **a single file holding many
events**, so "add this month to my calendar" is one click rather than thirty.
A copy-paste would have drifted the moment either page changed its date
handling, and date handling is exactly where this is delicate:

* `time` is free text scraped from a venue page — `"9pm"`, `"7:30 PM"`,
  `"doors 8"`. It is parsed leniently and an unparseable time degrades to an
  all-day event rather than dropping the event.
* An event with no date cannot be exported at all, and says so by returning
  `null` rather than emitting a VEVENT at epoch zero.
* Line folding is deliberately **not** implemented. RFC 5545 caps lines at 75
  octets, and every calendar client tested accepts longer ones; a wrong fold
  corrupts the file, whereas a long line is merely non-conformant.
"""

from __future__ import annotations

# Exported as `icsFor` (one event) and `icsForMany` (a calendar of many). Both
# return a `data:` URI ready for an <a download>, or null when nothing in the
# input carries a date.
ICS_JS = r"""
function _icsEscape(s) {
  return String(s || '').replace(/([,;\\])/g, '\\$1').replace(/\n/g, '\\n');
}

// 'YYYY-MM-DD' + free-text time -> an iCalendar stamp, or null. An 8-character
// return means all-day; 15 means a wall-clock local time. No timezone is
// attached on purpose: these are local dates scraped from local venues, and a
// floating time is what a 9pm show in SF actually means.
function _icsStamp(d, t) {
  if (!d) return null;
  const day = String(d).replace(/-/g, '');
  if (!t) return day;
  const m = String(t).match(/(\d{1,2})(?::(\d{2}))?\s*(am|pm)?/i);
  if (!m) return day;
  let h = parseInt(m[1], 10);
  const min = m[2] || '00';
  const ap = (m[3] || '').toLowerCase();
  if (ap === 'pm' && h < 12) h += 12;
  if (ap === 'am' && h === 12) h = 0;
  return day + 'T' + String(h).padStart(2, '0') + min + '00';
}

// The VEVENT block for one event, or null if it has no date.
function icsEvent(ev) {
  const dt = _icsStamp(ev.date, ev.time);
  if (!dt) return null;
  const allDay = dt.length === 8;
  const lines = [
    'BEGIN:VEVENT',
    'UID:eh-' + ev.i + '-' + dt + '@event-harvester',
    allDay ? 'DTSTART;VALUE=DATE:' + dt : 'DTSTART:' + dt,
    'SUMMARY:' + _icsEscape(ev.title),
  ];
  if (ev.location) lines.push('LOCATION:' + _icsEscape(ev.location));
  const desc = [ev.details, ev.link].filter(Boolean).join('\n\n');
  if (desc) lines.push('DESCRIPTION:' + _icsEscape(desc));
  lines.push('END:VEVENT');
  return lines;
}

function _icsWrap(blocks) {
  if (!blocks.length) return null;
  const lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//event-harvester//EN'];
  for (const b of blocks) lines.push.apply(lines, b);
  lines.push('END:VCALENDAR');
  return 'data:text/calendar;charset=utf-8,' + encodeURIComponent(lines.join('\r\n'));
}

function icsFor(ev) {
  const block = icsEvent(ev);
  return block ? _icsWrap([block]) : null;
}

function icsForMany(events) {
  return _icsWrap((events || []).map(icsEvent).filter(Boolean));
}

// A filename a file manager will not mangle, for the <a download> attribute.
function icsName(label) {
  return (String(label || '').slice(0, 40).replace(/[^\w -]/g, '').trim() || 'events') + '.ics';
}
"""
