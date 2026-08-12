"""The /admin surface: keyword lists, profiles, and the caps equalizer.

Mounted on the site's origin (`servers/site.py`), per `01 - Design/02 - The
Control UI.md` §2 — one origin, split routes.

**On access control.** These routes write config, unlike the read-only pages
beside them, and this origin carries no application-level auth. That is by
design and it is not an open door: **the services are served inside a ZeroTier
mesh whose peers are all authenticated** (confirmed 2026-08-10), so reaching
this port at all requires being an authorized member. Treat "add auth to
/admin" as answered, not outstanding.

The validation below is therefore about *mistakes*, not intruders, and is worth
keeping for that alone:

* Every write is validated against a closed vocabulary — cap keys, keyword-list
  name shape, profile field names — and clamped. A slider typo that set a cap
  to a million would cost real money at the LLM stage.
* Every write is atomic (temp + replace), because a half-written
  `keywords.json` breaks the *next harvest* rather than the current request —
  a failure that would surface hours later, far from its cause.

What would be a genuine escalation: binding beyond the mesh, or exposing any of
this to the public internet.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from event_harvester.config import (
    CAP_MAX,
    _clamp_cap,
    load_caps_overlay,
    save_caps_overlay,
)
from event_harvester.keywords import (
    KeywordError,
    KeywordList,
    load_keyword_lists,
    normalise_terms,
    plan_for_source,
    save_keyword_lists,
    terms_for,
    validate_name,
)

logger = logging.getLogger("event_harvester.admin")

# The "big categories" the equalizer controls. `total` is the ceiling applied
# after the per-source caps, so it is shown apart from the sliders.
CAP_SOURCES = ("discord", "telegram", "gmail", "signal", "web")

# Defaults mirror `CapConfig`; shown as the slider position when no overlay
# exists, so the UI never implies a value that is not in force.
CAP_DEFAULTS = {k: -1 for k in (*CAP_SOURCES, "total")}


def _web_sources() -> list[dict]:
    from event_harvester.sources.web_utils import _load_web_sources

    try:
        return _load_web_sources()
    except Exception as e:  # noqa: BLE001 -- the page must render regardless
        logger.error("admin: could not load web sources: %s", e)
        return []


def _profiles() -> list[dict]:
    p = Path("data/profiles.json")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    entries = raw.get("profiles", raw) if isinstance(raw, dict) else raw
    return [e for e in (entries or []) if isinstance(e, dict)]


def _save_profiles(profiles: list[dict]) -> None:
    p = Path("data/profiles.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"profiles": profiles}, indent=2) + "\n", encoding="utf-8",
    )
    tmp.replace(p)


def current_caps() -> dict[str, int]:
    """Effective caps: defaults, then whatever the overlay set."""
    caps = dict(CAP_DEFAULTS)
    caps.update(load_caps_overlay())
    return caps


def search_plan(profile: dict | None = None) -> list[dict]:
    """What every selected source would actually do with its keywords.

    This is the answer to "show me the different searches". It deliberately
    reports the *strength* of each one — native / category / local — because
    the platforms are not equivalent and a UI that implied they were would be
    lying. See the `keywords` module docstring for the measurements.
    """
    sources = _web_sources()
    if profile:
        picked = profile.get("web_sources") or []
        if picked and picked != ["__none__"]:
            wanted = {str(s).lower() for s in picked}
            sources = [s for s in sources if str(s.get("name", "")).lower() in wanted]
        elif picked == ["__none__"]:
            sources = []

    plans = []
    for src in sources:
        names = src.get("keywords") or (profile or {}).get("keywords") or []
        plans.append(plan_for_source(src, terms_for(names)).to_dict())
    return plans


# ── Route handlers ───────────────────────────────────────────────────


def build_routes():
    """Return the Starlette routes for /admin, imported lazily by site.py."""
    from starlette.responses import HTMLResponse, JSONResponse
    from starlette.routing import Route

    async def admin_page(request):
        return HTMLResponse(render_page(), headers={"Cache-Control": "no-store"})

    async def api_state(request):
        return JSONResponse({
            "caps": current_caps(),
            "cap_sources": list(CAP_SOURCES),
            "cap_max": CAP_MAX,
            "keywords": [kl.to_dict() for kl in load_keyword_lists()],
            "profiles": _profiles(),
            "sources": [
                {"name": s.get("name"), "mode": s.get("mode", "auto"),
                 "keywords": s.get("keywords") or []}
                for s in _web_sources()
            ],
            "plan": search_plan(),
        })

    async def api_caps(request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "expected an object"},
                                status_code=400)
        try:
            stored = save_caps_overlay(body.get("caps") or {})
        except (TypeError, ValueError) as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        logger.info("admin: caps set to %s", stored)
        return JSONResponse({"ok": True, "caps": current_caps()})

    async def api_keywords(request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        entries = body.get("keywords") if isinstance(body, dict) else None
        if not isinstance(entries, list):
            return JSONResponse({"ok": False, "error": "expected {keywords: [...]}"},
                                status_code=400)
        lists: list[KeywordList] = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            try:
                name = validate_name(e.get("name", ""))
            except KeywordError as err:
                return JSONResponse({"ok": False, "error": str(err)}, status_code=400)
            lists.append(KeywordList(
                name=name,
                terms=normalise_terms(e.get("terms")),
                description=str(e.get("description", ""))[:200],
            ))
        save_keyword_lists(lists)
        logger.info("admin: saved %d keyword list(s)", len(lists))
        return JSONResponse({"ok": True,
                             "keywords": [kl.to_dict() for kl in lists]})

    async def api_profiles(request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        entries = body.get("profiles") if isinstance(body, dict) else None
        if not isinstance(entries, list):
            return JSONResponse({"ok": False, "error": "expected {profiles: [...]}"},
                                status_code=400)
        clean = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            try:
                name = validate_name(e.get("name", ""))
            except KeywordError as err:
                return JSONResponse({"ok": False, "error": str(err)}, status_code=400)
            entry = {
                "name": name,
                "description": str(e.get("description", ""))[:200],
                "web_sources": [str(x)[:60] for x in (e.get("web_sources") or [])],
                "keywords": [str(x)[:40] for x in (e.get("keywords") or [])],
            }
            caps = e.get("caps")
            if isinstance(caps, dict):
                # `_clamp_cap`, not a local `max(0, ...)`. Clamping to zero
                # would turn "unlimited" into "drop this source entirely" —
                # the same misreading `config._clamp_cap` exists to prevent,
                # and now reachable by round-tripping a profile through this
                # page rather than only by hand-editing the file.
                entry["caps"] = {
                    str(k)[:20]: _clamp_cap(v)
                    for k, v in caps.items()
                    if isinstance(v, (int, float, str)) and str(v).lstrip("-").isdigit()
                }
            clean.append(entry)
        _save_profiles(clean)
        logger.info("admin: saved %d profile(s)", len(clean))
        return JSONResponse({"ok": True, "profiles": clean})

    async def api_plan(request):
        name = request.query_params.get("profile")
        prof = next((p for p in _profiles() if p.get("name") == name), None) if name else None
        return JSONResponse({"ok": True, "plan": search_plan(prof)})

    return [
        Route("/admin", admin_page),
        Route("/admin/api/state", api_state),
        Route("/admin/api/caps", api_caps, methods=["POST"]),
        Route("/admin/api/keywords", api_keywords, methods=["POST"]),
        Route("/admin/api/profiles", api_profiles, methods=["POST"]),
        Route("/admin/api/plan", api_plan),
    ]


def render_page() -> str:
    """The admin page. Served from here rather than a template file so the
    service has no runtime asset dependency — same choice `html_report.py`
    made, for the same reason."""
    from event_harvester.outputs.nav import NAV_STYLE, nav_html

    # This page only ever exists on the origin, so its nav is unconditional —
    # unlike the two generated pages, which can be forwarded away from it.
    return _PAGE.replace("<!--NAV-->", nav_html("/admin")).replace(
        "/*NAV-STYLE*/", NAV_STYLE)


_PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Event Harvester — Admin</title>
<style>
  :root{--bg:#0f1115;--card:#171a21;--line:#262b36;--fg:#e6e9ef;--dim:#9aa4b6;
        --accent:#6ea8fe;--good:#57c98b;--warn:#e6b455;--flat:#7f8798;
        /* aliases so the shared nav styles resolve here too */
        --muted:#9aa4b6;--chip:#212530}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:15px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  header{padding:20px 24px;border-bottom:1px solid var(--line);display:flex;
         align-items:baseline;gap:16px;flex-wrap:wrap}
  h1{font-size:19px;margin:0}
  a{color:var(--accent)}
  main{max-width:1080px;margin:0 auto;padding:24px;display:grid;gap:24px}
  section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px}
  h2{font-size:15px;margin:0 0 4px;letter-spacing:.02em}
  .hint{color:var(--dim);font-size:13px;margin:0 0 16px}
  .eq{display:grid;gap:14px}
  .row{display:grid;grid-template-columns:110px 1fr 84px 44px;gap:14px;align-items:center}
  .inf{background:transparent;border:1px solid var(--line);color:var(--dim);
       border-radius:7px;padding:7px 0;font-size:16px;line-height:1;cursor:pointer}
  .inf[aria-pressed=true]{background:var(--good);border-color:var(--good);color:#08101f}
  .row.off input[type=range],.row.off input[type=number]{opacity:.35}
  .row label{font-weight:600;text-transform:capitalize}
  input[type=range]{width:100%;accent-color:var(--accent)}
  input[type=number],input[type=text]{background:#0c0e13;color:var(--fg);
       border:1px solid var(--line);border-radius:6px;padding:7px 9px;width:100%;font:inherit}
  .total{border-top:1px solid var(--line);margin-top:6px;padding-top:16px}
  button{background:var(--accent);color:#08101f;border:0;border-radius:7px;
         padding:9px 15px;font:inherit;font-weight:650;cursor:pointer}
  button.ghost{background:transparent;color:var(--fg);border:1px solid var(--line)}
  .bar{display:flex;gap:10px;align-items:center;margin-top:16px;flex-wrap:wrap}
  .ok{color:var(--good)} .err{color:var(--warn)}
  table{width:100%;border-collapse:collapse;font-size:14px}
  th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);
        vertical-align:top}
  th{color:var(--dim);font-weight:600;font-size:12px;text-transform:uppercase;
     letter-spacing:.04em}
  .tag{display:inline-block;padding:1px 8px;border-radius:99px;font-size:11px;
       font-weight:700;letter-spacing:.03em}
  .native{background:rgba(87,201,139,.16);color:var(--good)}
  .category{background:rgba(230,180,85,.16);color:var(--warn)}
  .local{background:rgba(127,135,152,.18);color:var(--flat)}
  .kw{display:grid;gap:10px;margin-bottom:12px;padding:12px;
      border:1px solid var(--line);border-radius:8px}
  .kw .top{display:grid;grid-template-columns:180px 1fr auto;gap:10px}
  .picks{display:grid;grid-template-columns:1fr 1fr;gap:18px}
  @media(max-width:640px){.picks{grid-template-columns:1fr}
    .kw .top{grid-template-columns:1fr}}
  .pick{display:inline-flex;align-items:center;gap:6px;margin:0 12px 4px 0;
        font-size:13.5px;white-space:nowrap}
  .pick input{accent-color:var(--accent)}
  code{background:#0c0e13;padding:1px 5px;border-radius:4px;font-size:13px}
  .note{color:var(--dim);font-size:12.5px}
  header{flex-direction:column;align-items:stretch;gap:0}
  .headline{display:flex;align-items:baseline;gap:16px;flex-wrap:wrap}
/*NAV-STYLE*/
</style></head><body>
<header>
  <!--NAV-->
  <div class="headline">
    <h1>Event Harvester — Admin</h1>
    <a href="/healthz">health</a>
    <span id="status" class="note"></span>
  </div>
</header>
<main>

<section>
  <h2>Caps</h2>
  <p class="hint">How many messages each source may send to the LLM, after scoring.
     <strong>Unlimited (&#8734;) by default since 2026-08-11</strong> — capping here
     was losing whole sources silently: every web sub-source shares one
     <code>web</code> bucket, so Luma's 60 messages took all 30 slots and erobay
     fetched 121 every run for weeks without producing a single event. Filtering
     belongs on the events page, which can only filter what was extracted.
     Set a number to bound the LLM bill. Saved to
     <code>data/settings.json</code>; delete it to fall back to <code>.env</code>.
     The <code>event-harvester</code> CLI keeps the old ceilings regardless.</p>
  <div class="eq" id="eq"></div>
  <div class="row total">
    <label for="cap-total">Total</label>
    <input type="range" id="cap-total-range" min="0" max="2000" step="25">
    <input type="number" id="cap-total" min="0">
    <button class="inf" id="cap-total-inf" type="button"
            aria-pressed="false" title="No limit">&#8734;</button>
  </div>
  <p class="note">Total is applied <em>after</em> the per-source caps, so it can
     override them. If the sliders add up to more than the total, the total wins.
     <span id="sum"></span></p>
  <div class="bar"><button onclick="saveCaps()">Save caps</button>
    <button class="ghost" onclick="load()">Reset</button></div>
</section>

<section>
  <h2>Keywords</h2>
  <p class="hint">Your own words. A source references a list by name; the same
     list can feed several sources.</p>
  <div id="kws"></div>
  <div class="bar"><button class="ghost" onclick="addList()">Add list</button>
    <button onclick="saveKeywords()">Save keywords</button></div>
</section>

<section>
  <h2>Profiles</h2>
  <p class="hint">A saved selection: which web sources to run, which keyword
     lists to carry, and optional caps that override the sliders above for that
     run only. <code>__none__</code> in sources means "skip web entirely" — an
     empty list means "all of them", so the two need different spellings.
     Saved to <code>data/profiles.json</code>.</p>
  <div id="profs"></div>
  <div class="bar"><button class="ghost" onclick="addProfile()">Add profile</button>
    <button onclick="saveProfiles()">Save profiles</button></div>
</section>

<section>
  <h2>Searches</h2>
  <p class="hint">What each source will actually do with its keywords. The
     platforms are not equivalent, so this shows the strength of each:</p>
  <p class="note">
    <span class="tag native">native</span> typed into the site's own search &nbsp;
    <span class="tag category">category</span> mapped to the platform's taxonomy &nbsp;
    <span class="tag local">local</span> cannot be queried — matched after fetching,
    and scored higher rather than filtered
  </p>
  <div class="bar" style="margin:0 0 12px">
    <label for="prof" class="note">Profile</label>
    <select id="prof" onchange="loadPlan()" style="background:#0c0e13;color:var(--fg);
      border:1px solid var(--line);border-radius:6px;padding:7px"></select>
  </div>
  <table><thead><tr><th>Source</th><th>Mode</th><th>Strength</th>
    <th>Searches</th><th>Why</th></tr></thead><tbody id="plan"></tbody></table>
</section>

</main>
<script>
let S = {caps:{}, keywords:[], profiles:[], cap_sources:[], cap_max:5000};
const $ = id => document.getElementById(id);
const esc = s => String(s??"").replace(/[&<>"]/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function say(msg, bad){ const el=$("status");
  el.textContent=msg; el.className = bad ? "err" : "ok";
  setTimeout(()=>{el.textContent="";}, 4000); }

async function load(){
  const r = await fetch("/admin/api/state");
  S = await r.json();
  renderEq(); renderKeywords(); renderProfiles(); renderPlan(S.plan);
}

function renderEq(){
  $("eq").innerHTML = S.cap_sources.map(name => `
    <div class="row">
      <label for="cap-${name}">${esc(name)}</label>
      <input type="range" id="cap-${name}-range" min="0" max="500" step="5">
      <input type="number" id="cap-${name}" min="0" max="${S.cap_max}">
      <button class="inf" id="cap-${name}-inf" type="button"
              aria-pressed="false" title="No limit">&#8734;</button>
    </div>`).join("");
  for (const name of S.cap_sources) bindPair(name, S.caps[name]);
  bindPair("total", S.caps.total);
}

// -1 is "unlimited", and it is the default. It cannot be a slider position —
// the slider is a quantity — so it is a separate toggle, and turning it on
// dims the quantity controls rather than hiding them, so the number you would
// return to stays visible.
function bindPair(name, value){
  const num = $(`cap-${name}`), rng = $(`cap-${name}-range`), inf = $(`cap-${name}-inf`);
  const row = inf.closest(".row");
  const unlimited = (value === undefined || value === null || value < 0);
  const shown = unlimited ? Number(rng.max) : value;

  const paint = () => {
    const off = inf.getAttribute("aria-pressed") === "true";
    row.classList.toggle("off", off);
    num.disabled = rng.disabled = off;
    sum();
  };
  inf.setAttribute("aria-pressed", String(unlimited));
  num.value = shown; rng.value = Math.min(shown, Number(rng.max));
  rng.oninput = () => { num.value = rng.value; sum(); };
  num.oninput = () => { rng.value = Math.min(Number(num.value)||0, Number(rng.max)); sum(); };
  inf.onclick = () => {
    inf.setAttribute("aria-pressed",
      String(inf.getAttribute("aria-pressed") !== "true"));
    paint();
  };
  paint();
}

function capOf(name){
  return $(`cap-${name}-inf`).getAttribute("aria-pressed") === "true"
    ? -1 : (Number($(`cap-${name}`).value) || 0);
}

function sum(){
  if(!S.cap_sources.length) return;
  const total = capOf("total");
  const vals = S.cap_sources.map(capOf);
  if (vals.some(v => v < 0) || total < 0){
    $("sum").textContent =
      "Unlimited buckets send everything the classifier passes to the LLM.";
    return;
  }
  const per = vals.reduce((a,b)=>a+b, 0);
  $("sum").textContent = `Sliders sum to ${per}; total is ${total}.` +
    (per > total ? " Total is the binding one." : "");
}

function capsPayload(){
  const caps = {total: capOf("total")};
  for (const n of S.cap_sources) caps[n] = capOf(n);
  return caps;
}

async function saveCaps(){
  const r = await fetch("/admin/api/caps", {method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({caps: capsPayload()})});
  const j = await r.json();
  if(j.ok){ S.caps=j.caps; say("Caps saved. Applies to the next harvest."); }
  else say(j.error||"save failed", true);
}

function renderKeywords(){
  $("kws").innerHTML = S.keywords.map((k,i) => `
    <div class="kw">
      <div class="top">
        <input type="text" value="${esc(k.name)}" data-i="${i}" data-f="name"
               placeholder="name" aria-label="list name">
        <input type="text" value="${esc(k.description)}" data-i="${i}" data-f="description"
               placeholder="what this list is for" aria-label="description">
        <button class="ghost" onclick="delList(${i})">Remove</button>
      </div>
      <input type="text" value="${esc((k.terms||[]).join(', '))}" data-i="${i}"
             data-f="terms" placeholder="comma-separated terms" aria-label="terms">
    </div>`).join("") || '<p class="note">No lists yet.</p>';
  for (const el of $("kws").querySelectorAll("input")){
    el.oninput = () => {
      const k = S.keywords[+el.dataset.i], f = el.dataset.f;
      k[f] = f === "terms" ? el.value.split(",").map(s=>s.trim()).filter(Boolean)
                           : el.value;
    };
  }
}

function addList(){ S.keywords.push({name:"", description:"", terms:[]}); renderKeywords(); }
function delList(i){ S.keywords.splice(i,1); renderKeywords(); }

async function saveKeywords(){
  const r = await fetch("/admin/api/keywords", {method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({keywords: S.keywords})});
  const j = await r.json();
  if(j.ok){ S.keywords=j.keywords; renderKeywords(); loadPlan();
            say("Keywords saved."); }
  else say(j.error||"save failed", true);
}

// Sources and keyword lists are checkboxes rather than free text because both
// vocabularies are closed and already known here. A typed source name that
// matches nothing fails silently at harvest time, hours later — the classic
// shape of a config bug that costs an evening.
function renderProfiles(){
  $("profs").innerHTML = S.profiles.map((p,i) => `
    <div class="kw">
      <div class="top">
        <input type="text" value="${esc(p.name)}" data-i="${i}" data-f="name"
               placeholder="name" aria-label="profile name">
        <input type="text" value="${esc(p.description||'')}" data-i="${i}"
               data-f="description" placeholder="what this profile is for"
               aria-label="description">
        <button class="ghost" onclick="delProfile(${i})">Remove</button>
      </div>
      <div class="picks">
        <div>
          <div class="note">Web sources</div>
          ${pickList(i, "web_sources", S.sources.map(s=>s.name), p.web_sources||[])}
          <label class="pick"><input type="checkbox" data-i="${i}" data-f="none"
            ${(p.web_sources||[]).includes("__none__") ? "checked" : ""}>
            <span>skip web entirely</span></label>
        </div>
        <div>
          <div class="note">Keyword lists</div>
          ${pickList(i, "keywords", S.keywords.map(k=>k.name), p.keywords||[])}
        </div>
      </div>
      <div class="note">${capSummary(p)}</div>
    </div>`).join("") || '<p class="note">No profiles yet.</p>';

  for (const el of $("profs").querySelectorAll('input[type=text]')){
    el.oninput = () => { S.profiles[+el.dataset.i][el.dataset.f] = el.value; };
  }
  for (const el of $("profs").querySelectorAll('input[type=checkbox]')){
    el.onchange = () => {
      const p = S.profiles[+el.dataset.i], f = el.dataset.f;
      if (f === "none"){
        // Mutually exclusive with picking sources: "none of them" and "these
        // three" cannot both be the answer.
        p.web_sources = el.checked ? ["__none__"] : [];
        renderProfiles();
        return;
      }
      const set = new Set((p[f]||[]).filter(v => v !== "__none__"));
      el.checked ? set.add(el.value) : set.delete(el.value);
      p[f] = [...set];
      if (f === "web_sources") renderProfiles();
    };
  }
  renderProfileOptions();
}

function pickList(i, field, options, chosen){
  const picked = new Set(chosen||[]);
  return options.map(o => `
    <label class="pick"><input type="checkbox" value="${esc(o)}" data-i="${i}"
      data-f="${field}" ${picked.has(o) ? "checked" : ""}>
      <span>${esc(o)}</span></label>`).join("") ||
    '<span class="note">none defined</span>';
}

// Caps are editable above, per-profile caps are not — they are shown so the
// override is visible rather than a surprise at harvest time.
function capSummary(p){
  const caps = p.caps || {};
  const keys = Object.keys(caps);
  if (!keys.length) return "No cap overrides — the sliders above apply.";
  return "Cap overrides: " +
    keys.map(k => `${k}=${caps[k]}`).join(", ") +
    " (edit in data/profiles.json)";
}

function addProfile(){
  S.profiles.push({name:"", description:"", web_sources:[], keywords:[]});
  renderProfiles();
}
function delProfile(i){ S.profiles.splice(i,1); renderProfiles(); }

async function saveProfiles(){
  const r = await fetch("/admin/api/profiles", {method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({profiles: S.profiles})});
  const j = await r.json();
  if(j.ok){ S.profiles=j.profiles; renderProfiles(); loadPlan();
            say("Profiles saved. Applies to the next harvest."); }
  else say(j.error||"save failed", true);
}

function renderProfileOptions(){
  const keep = $("prof").value;
  $("prof").innerHTML = '<option value="">All sources</option>' +
    S.profiles.filter(p=>p.name)
      .map(p=>`<option value="${esc(p.name)}">${esc(p.name)}</option>`).join("");
  $("prof").value = keep;
}

async function loadPlan(){
  const p = $("prof").value;
  const r = await fetch("/admin/api/plan" + (p ? `?profile=${encodeURIComponent(p)}` : ""));
  const j = await r.json();
  renderPlan(j.plan || []);
}

function renderPlan(plan){
  $("plan").innerHTML = plan.map(p => `
    <tr>
      <td>${esc(p.source)}</td>
      <td><code>${esc(p.mode)}</code></td>
      <td><span class="tag ${esc(p.strength)}">${esc(p.strength)}</span></td>
      <td>${(p.terms||[]).map(t=>`<code>${esc(t)}</code>`).join(" ")
            || '<span class="note">&mdash;</span>'}
          ${(p.categories||[]).map(c=>`<code>${esc(c)}</code>`).join(" ")}</td>
      <td class="note">${esc(p.note)}</td>
    </tr>`).join("") || '<tr><td colspan="5" class="note">No sources.</td></tr>';
}

load();
</script></body></html>
"""
