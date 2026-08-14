"""`/sources` — what is configured, in three kinds, and a form to add another.

Two things the CLI could not do, which is the whole justification for a page:

**Group by how a source must be *run*, not by what it is.** A venue, an
aggregator and a bar are the same problem; a Cloudflare-fronted site and an
open one are not. The three kinds in ``onboard.SOURCE_KINDS`` are the
distinction that actually predicts work:

* **direct** — publishes its calendar. A feed, or HTML that already lists dated
  events. Nothing has to be driven.
* **search** — has the events and will not show them until asked.
* **special** — needs machinery of its own: a bot challenge only a real browser
  clears, a platform with a dedicated mode, or a listing that arrives by XHR.

**Keep the attempts.** Adding a source is a sequence of judgements — this URL
not that one, this mode because the other under-yields — and until now every
one of them lived in a terminal scrollback or a commit message. The blueprint
says so itself about the one override that was written down: *"that is one `git
log` away from lost."* Attempts are recorded to disk, successes and failures
alike, because a source you tried and rejected is information the next person
needs more than a source you added.

Nothing here writes `web_sources.json`. The page proposes; a human still runs
blueprint step 7 — a real `--dry-run` that produces real events — before
anything is configured. A page that could add a source on the strength of a
scoring function would be a page that adds sources nobody proved.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from event_harvester.utils import data_path, load_json, save_json

logger = logging.getLogger("event_harvester.servers.sources_page")

ATTEMPTS_FILE = data_path(".source_attempts.json")

#: Keep the log bounded. This is a working record, not an audit trail, and an
#: unbounded JSON file read on every page load is a slow leak.
MAX_ATTEMPTS = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_attempts() -> list[dict]:
    data = load_json(ATTEMPTS_FILE, default={"attempts": []}) or {}
    items = data.get("attempts") if isinstance(data, dict) else data
    return list(items or [])


def record_attempt(entry: dict) -> list[dict]:
    """Append an attempt, newest first, and return the trimmed log."""
    attempts = [entry] + load_attempts()
    attempts = attempts[:MAX_ATTEMPTS]
    save_json(ATTEMPTS_FILE, {"attempts": attempts})
    return attempts


def configured_sources() -> list[dict]:
    """Every configured source, tagged with its kind and why."""
    from event_harvester.sources.onboard import classify_kind
    from event_harvester.sources.web_utils import _load_web_sources

    out: list[dict] = []
    for src in _load_web_sources() or []:
        mode = str(src.get("mode") or "auto")
        native = bool(src.get("native_chrome"))
        kind = classify_kind(
            mode=mode, native_chrome=native, has_query=bool(src.get("queries") or src.get("query")),
        )
        reasons = []
        if native:
            reasons.append("native_chrome — a bot challenge only a real browser clears")
        if mode in ("luma", "partiful", "instagram"):
            reasons.append(f"{mode} has a dedicated mode; scraping it under-yields badly")
        if mode == "feed":
            reasons.append("listing arrives by XHR, needs api_pattern")
        if src.get("queries") or src.get("query"):
            reasons.append("needs a query before anything appears")
        out.append({
            "name": src.get("name") or "?",
            "url": src.get("url") or "",
            "mode": mode,
            "kind": kind,
            "why": reasons,
            "category": src.get("category") or "",
            "cooldown_hours": src.get("cooldown_hours"),
        })
    out.sort(key=lambda s: (s["kind"], s["name"]))
    return out


async def _explore(url: str, **kw) -> dict[str, Any]:
    """Run the browser-free reasoning off the event loop.

    `onboard.explore` is synchronous `requests` and takes seconds — called
    directly it would block every other request to this server for the whole
    sweep, which is the same mistake `orchestrator` fixed by moving extraction
    onto `asyncio.to_thread`.
    """
    from event_harvester.sources.onboard import explore
    from event_harvester.sources.onboard_handoff import build_brief

    report = await asyncio.to_thread(explore, url, **kw)
    payload = report.to_dict()
    brief = build_brief(report)
    payload["agent_brief"] = brief.to_dict() if brief else None
    payload["agent_prompt"] = brief.to_prompt() if brief else None
    return payload


def routes():
    from starlette.responses import HTMLResponse, JSONResponse
    from starlette.routing import Route

    async def sources_page(request):
        return HTMLResponse(render_page())

    async def api_state(request):
        from event_harvester.sources.onboard import SOURCE_KINDS
        sources = configured_sources()
        counts: dict[str, int] = {k: 0 for k in SOURCE_KINDS}
        for s in sources:
            counts[s["kind"]] = counts.get(s["kind"], 0) + 1
        return JSONResponse({
            "ok": True, "kinds": SOURCE_KINDS, "counts": counts,
            "sources": sources, "attempts": load_attempts()[:40],
        })

    async def api_explore(request):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        url = str((body or {}).get("url") or "").strip()
        if not url:
            return JSONResponse({"ok": False, "error": "no url"}, status_code=400)
        if len(url) > 500:
            return JSONResponse({"ok": False, "error": "url too long"}, status_code=400)

        try:
            payload = await _explore(url)
        except Exception as e:  # noqa: BLE001 - a bad URL must not 500 the page
            logger.exception("sources: explore failed for %s", url)
            record_attempt({
                "at": _now(), "url": url, "outcome": "error",
                "detail": f"{type(e).__name__}: {e}",
            })
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"},
                                status_code=502)

        if payload.get("already_configured"):
            outcome = "already-configured"
        elif payload.get("agent_brief"):
            outcome = "needs-agent"
        elif payload.get("mode"):
            outcome = "proposed"
        else:
            outcome = "undecided"

        record_attempt({
            "at": _now(), "url": url, "outcome": outcome,
            "mode": payload.get("mode"), "kind": payload.get("kind"),
            "chose_url": (payload.get("best") or {}).get("url"),
            "changed_url": payload.get("changed_url"),
            "warnings": (payload.get("warnings") or [])[:2],
        })
        payload["ok"] = True
        payload["outcome"] = outcome
        return JSONResponse(payload)

    async def api_attempts(request):
        return JSONResponse({"ok": True, "attempts": load_attempts()[:100]})

    return [
        Route("/sources", sources_page),
        Route("/sources/api/state", api_state),
        Route("/sources/api/explore", api_explore, methods=["POST"]),
        Route("/sources/api/attempts", api_attempts),
    ]


_STYLE = """
:root {
  --bg:#fff; --fg:#16181d; --muted:#5b6472; --line:#e4e7ec; --card:#fff;
  --accent:#7c3aed; --chip:#f2f4f7; --ok:#0f7b3d; --warn:#a1560a; --bad:#b3261e;
  --fs-sm: clamp(13px, 12.4px + 0.16vw, 15px);
  --fs-md: clamp(14px, 13.4px + 0.16vw, 16px);
  --fs-base: clamp(16px, 15.4px + 0.16vw, 18px);
  --fs-lg: clamp(18px, 15.6px + 0.65vw, 26px);
}
@media (prefers-color-scheme: dark) {
  :root { --bg:#0f1115; --fg:#e8eaed; --muted:#98a2b3; --line:#242832;
          --card:#171a21; --accent:#a78bfa; --chip:#212530; --ok:#4ade80;
          --warn:#fbbf24; --bad:#f87171; }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
  font:var(--fs-base)/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
  Helvetica,Arial,sans-serif; -webkit-text-size-adjust:100%; }
header { padding:20px 16px 12px; border-bottom:1px solid var(--line); }
h1 { margin:0 0 4px; font-size:var(--fs-lg); letter-spacing:-0.01em; }
.meta { color:var(--muted); font-size:var(--fs-sm); }
main { padding:0 16px 48px; }
section { margin:22px 0; min-width:0; }
h2 { font-size:var(--fs-md); text-transform:uppercase; letter-spacing:.06em;
     color:var(--muted); margin:0 0 4px; }
.kindnote { color:var(--muted); font-size:var(--fs-sm); margin:0 0 10px;
            max-width:70ch; }
.grid { display:grid; gap:10px;
        grid-template-columns:repeat(auto-fill,minmax(clamp(260px,20vw,340px),1fr)); }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px;
        padding:12px 14px; min-width:0; }
.card h3 { margin:0 0 4px; font-size:var(--fs-md); word-break:break-word; }
.card .u { color:var(--muted); font-size:var(--fs-sm); word-break:break-all; }
.tag { display:inline-block; padding:1px 8px; border-radius:999px;
       background:var(--chip); font-size:var(--fs-sm); margin-right:6px; }
.why { margin:8px 0 0; padding-left:18px; color:var(--muted);
       font-size:var(--fs-sm); }
form.add { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
input[type=url] { flex:1 1 320px; min-width:0; padding:11px 12px;
  font-size:var(--fs-base); color:var(--fg); background:var(--chip);
  border:1px solid var(--line); border-radius:10px; }
button { padding:10px 16px; min-height:44px; font-size:var(--fs-md);
  color:#fff; background:var(--accent); border:0; border-radius:10px;
  cursor:pointer; }
button[disabled] { opacity:.55; cursor:progress; }
pre { background:var(--chip); border:1px solid var(--line); border-radius:10px;
      padding:12px; overflow-x:auto; font-size:var(--fs-sm); }
table { border-collapse:collapse; width:100%; font-size:var(--fs-sm); }
th,td { text-align:left; padding:6px 10px; border-bottom:1px solid var(--line);
        vertical-align:top; }
th { color:var(--muted); font-weight:600; }
.tablewrap { overflow-x:auto; }
.ok{color:var(--ok)} .warn{color:var(--warn)} .bad{color:var(--bad)}
.pill { font-size:var(--fs-sm); padding:1px 8px; border-radius:999px;
        background:var(--chip); }
.empty { color:var(--muted); }
@media (max-width:700px){ header{padding:12px 14px 8px} main{padding:0 14px 48px} }
/*NAV-STYLE*/
"""

_PAGE = """<!--BASE-->
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sources</title>
<style>__STYLE__</style>
<header>
  <!--NAV-->
  <h1>Sources</h1>
  <div class="meta" id="summary">loading…</div>
</header>
<main>
  <section>
    <h2>Add a site</h2>
    <p class="kindnote">Paste a URL. This runs the browser-free reasoning —
      redirect chain, feeds, nav and sitemap candidates, date density — and
      proposes what to configure. <strong>It does not configure anything.</strong>
      A source is only proven by a real <code>--dry-run</code> that produces
      real events.</p>
    <form class="add" id="addform">
      <input type="url" id="url" placeholder="https://venue.example/" required>
      <button type="submit" id="go">Examine</button>
    </form>
    <div id="result"></div>
  </section>

  <section>
    <h2>Attempts</h2>
    <p class="kindnote">Every examination, kept. A source you tried and
      rejected is information the next person needs more than one you added.</p>
    <div class="tablewrap"><table id="attempts">
      <thead><tr><th>When</th><th>URL</th><th>Outcome</th><th>Proposed</th></tr></thead>
      <tbody><tr><td colspan="4" class="empty">none yet</td></tr></tbody>
    </table></div>
  </section>

  <div id="kinds"></div>
</main>
<script>__SCRIPT__</script>
"""

_SCRIPT = r"""
const $ = s => document.querySelector(s);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function when(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString();
}

const OUTCOME_CLASS = {
  'proposed': 'ok', 'already-configured': '', 'needs-agent': 'warn',
  'undecided': 'warn', 'error': 'bad',
};

function renderAttempts(rows) {
  const body = $('#attempts tbody');
  if (!rows || !rows.length) {
    body.innerHTML = '<tr><td colspan="4" class="empty">none yet</td></tr>';
    return;
  }
  body.innerHTML = '';
  for (const a of rows) {
    const tr = document.createElement('tr');
    const proposed = a.mode
      ? esc(a.mode) + (a.kind ? ' · ' + esc(a.kind) : '') +
        (a.changed_url ? ' <span class="pill">moved URL</span>' : '')
      : '<span class="empty">—</span>';
    tr.innerHTML =
      '<td>' + esc(when(a.at)) + '</td>' +
      '<td class="u">' + esc(a.url) + '</td>' +
      '<td class="' + (OUTCOME_CLASS[a.outcome] || '') + '">' + esc(a.outcome) + '</td>' +
      '<td>' + proposed + '</td>';
    body.appendChild(tr);
  }
}

function renderKinds(state) {
  const wrap = $('#kinds');
  wrap.innerHTML = '';
  for (const [kind, blurb] of Object.entries(state.kinds)) {
    const list = state.sources.filter(s => s.kind === kind);
    const sec = document.createElement('section');
    const cards = list.map(s => {
      const why = (s.why || []).map(w => '<li>' + esc(w) + '</li>').join('');
      return '<div class="card"><h3>' + esc(s.name) + '</h3>' +
        '<div><span class="tag">' + esc(s.mode) + '</span>' +
        (s.cooldown_hours ? '<span class="tag">' + esc(s.cooldown_hours) + 'h</span>' : '') +
        '</div><div class="u">' + esc(s.url) + '</div>' +
        (why ? '<ul class="why">' + why + '</ul>' : '') + '</div>';
    }).join('');
    sec.innerHTML = '<h2>' + esc(kind) + ' · ' + list.length + '</h2>' +
      '<p class="kindnote">' + esc(blurb) + '</p>' +
      (list.length ? '<div class="grid">' + cards + '</div>'
                   : '<p class="empty">none</p>');
    wrap.appendChild(sec);
  }
}

function renderReport(r) {
  const out = [];
  if (r.already_configured) {
    out.push('<p class="ok">Already configured as <strong>' +
      esc(r.already_configured) + '</strong>.</p>');
  }
  if (r.mode) {
    out.push('<p><strong>Proposed:</strong> <span class="tag">' + esc(r.mode) +
      '</span>' + (r.kind ? '<span class="tag">' + esc(r.kind) + '</span>' : '') +
      (r.changed_url ? ' <span class="pill">moved off the URL you gave</span>' : '') +
      '</p>');
  }
  if (r.best) {
    out.push('<p class="u">' + esc(r.best.url) + ' — ' + r.best.dated_links +
      ' dated link(s), ' + r.best.chars + ' chars, ' + r.best.date_density +
      ' dates/1k</p>');
  }
  for (const w of (r.warnings || [])) {
    out.push('<p class="warn">⚠ ' + esc(w) + '</p>');
  }
  for (const n of (r.notes || [])) {
    out.push('<p class="empty">' + esc(n) + '</p>');
  }
  if ((r.candidates || []).length > 1) {
    const rows = r.candidates.map(c =>
      '<tr><td class="u">' + esc(c.url) + '</td><td>' + esc(c.origin) +
      '</td><td>' + c.dated_links + '</td><td>' + c.chars + '</td><td>' +
      c.date_density + '</td></tr>').join('');
    out.push('<div class="tablewrap"><table><thead><tr><th>candidate</th>' +
      '<th>found via</th><th>dated links</th><th>chars</th><th>dates/1k</th>' +
      '</tr></thead><tbody>' + rows + '</tbody></table></div>');
  }
  if (r.agent_prompt) {
    out.push('<p class="warn">This one needs a browser. Hand this brief to an ' +
      'agent — it carries what has already been established, so nothing is ' +
      're-derived:</p><pre>' + esc(r.agent_prompt) + '</pre>');
  }
  $('#result').innerHTML = out.join('\n');
}

async function load() {
  const r = await fetch('sources/api/state');
  const state = await r.json();
  const c = state.counts || {};
  $('#summary').textContent = state.sources.length + ' configured — ' +
    Object.entries(c).map(([k, n]) => n + ' ' + k).join(' · ');
  renderKinds(state);
  renderAttempts(state.attempts);
}

$('#addform').addEventListener('submit', async ev => {
  ev.preventDefault();
  const url = $('#url').value.trim();
  if (!url) return;
  $('#go').disabled = true;
  $('#result').innerHTML = '<p class="empty">examining ' + esc(url) +
    ' — this fetches a handful of pages…</p>';
  try {
    const res = await fetch('sources/api/explore', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url}),
    });
    const data = await res.json();
    if (!data.ok) {
      $('#result').innerHTML = '<p class="bad">' + esc(data.error) + '</p>';
    } else {
      renderReport(data);
    }
  } catch (e) {
    $('#result').innerHTML = '<p class="bad">' + esc(e) + '</p>';
  } finally {
    $('#go').disabled = false;
    const a = await (await fetch('sources/api/attempts')).json();
    renderAttempts(a.attempts);
  }
});

load();
"""


def render_page() -> str:
    from event_harvester.outputs.nav import NAV_STYLE, base_script, nav_html

    return (_PAGE
            .replace("<!--BASE-->", base_script("sources"))
            .replace("<!--NAV-->", nav_html("sources"))
            .replace("__STYLE__", _STYLE.replace("/*NAV-STYLE*/", NAV_STYLE))
            .replace("__SCRIPT__", _SCRIPT))
