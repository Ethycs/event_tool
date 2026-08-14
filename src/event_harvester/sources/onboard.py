"""Someone hands you a URL. This decides what to actually configure.

The manual version is `10 - Blueprints/01 - Adding a Web Source.md`, and its own
"known rough edges" section admits the important step is not automated:

    **Step 3 is manual.** Finding the better URL is exactly the work
    [the control UI] proposes automating. Until then it is `curl` and judgement.

This is that step, plus the judgement around it. **No browser.** Everything here
is `requests` and parsing, which is what makes it cheap enough to run on every
candidate rather than only on the one a human already guessed.

## The one thing it exists to know

**The URL you are given is usually not the page that has the events.** Two for
two on 2026-08-04, and both would have "worked" if configured as handed over:

| Given | Right answer | Why the given one fails |
|---|---|---|
| `thebitterendsf.com` | `…/calendar` | 4,619 chars of nav and gallery, **0 event
  links**; the calendar section is 821 chars of pure schedule |
| `ma.to` | `…/events/san-francisco` | Homepage defaults to **New York** |

Neither would have raised an error. One would have fed a menu to the LLM every
night; the other would have filled an SF calendar with New York.

## Ranking: the two cases disagree, and that is the design

`ma.to` was decided by **dated event links** — 18 against 0. The Bitter End had
zero links on both candidates and was decided by **concentration**: 821 chars of
schedule beat 4,619 chars of nav, so *fewer characters won*. A single "more
content is better" score gets the second case exactly backwards.

So: rank by dated event links; when every candidate has none, break the tie on
date-token density rather than size. Both numbers are reported either way, so a
human can overrule a ranking they can see.

## What it will not do

Decide. :func:`explore` returns evidence and a recommendation; it does not write
`web_sources.json`. The blueprint's step 7 — prove it end to end before
believing it — is not something a scoring function can do for you.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from event_harvester.observability.tracing import get_tracer
from event_harvester.sources.web_utils import (
    _DECIMATE_DATE_RE,
    _DECIMATE_TIME_RE,
    _extract_text,
)

logger = logging.getLogger("event_harvester.sources.onboard")
tracer = get_tracer("event_harvester.sources.onboard")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

#: Cap on bytes pulled per page. This runs against arbitrary URLs and one
#: video-heavy homepage should not be able to pull hundreds of MB into memory.
MAX_HTML_BYTES = 2_000_000

#: Nav hrefs worth following. Ordered by how often they were the answer.
_NAV_HINTS = ("calendar", "events", "event", "schedule", "whats-on", "what's-on",
              "whatson", "lineup", "shows", "agenda", "programme", "program")

#: Path *tokens* that disqualify a candidate. Matched against the path split on
#: `/`, `-` and `_` rather than as substrings, because the real-world shape is
#: `/event-party-booking` — a room-hire form that matched on "event", outranked
#: the venue's actual calendar, and would have been configured (2026-08-14).
#: A substring test with a leading slash misses it; one without would reject
#: `facebook` and `bookings-are-open` alike.
_NAV_ANTI_TOKENS = frozenset({
    "new", "submit", "add", "create", "past", "archive", "privacy", "terms",
    "login", "signin", "signup", "register", "account", "cart", "checkout",
    "booking", "bookings", "book", "rental", "rentals", "hire", "private",
    "merch", "merchandise", "menu", "menus", "faq", "contact", "about",
    "gallery", "team", "jobs", "press",
})

#: A **single event's** page, which is the most seductive wrong answer: it is
#: full of dates, so it outranks the listing that links to it on every signal
#: this module measures. Configuring one pins the source to one night forever.
#: Measured 2026-08-14 — `ma.to/event/bagels-with-buddies-canyon-coffee-14-aug-2026`
#: beat `ma.to/events/san-francisco` before this existed.
_DETAIL_PATH_RE = re.compile(
    r"^/(?:event|e|show|gig|party|listing)/[^/]+", re.I,
)

#: A page recommending `raw` while carrying at least this many date strings is
#: the Bottom of the Hill trap: `raw` sends **one** message however many shows
#: the page lists, so a 338 KB calendar of ~40 shows yielded exactly one event.
#: The same venue's RSS yielded 65.
RAW_DENSE_DATES = 8

#: Hosts that must never be scraped — they have their own mode, and configuring
#: them as `calendar` was measured returning 1 event from a listing of 80.
_PLATFORM_MODES = {
    "lu.ma": "luma",
    "partiful.com": "partiful",
}

_FEED_LINK_RE = re.compile(
    r"""<link[^>]+(?:type=["']application/(rss|atom)\+xml["'][^>]*href=["']([^"']+)["']"""
    r"""|href=["']([^"']+)["'][^>]*type=["']application/(rss|atom)\+xml["'])""",
    re.I,
)
_FEED_HREF_RE = re.compile(
    r"""href=["']([^"']*(?:/feed/?|rss(?:\.xml)?|atom\.xml|feed\.xml)(?:\?[^"']*)?)["']""",
    re.I,
)
_ANCHOR_RE = re.compile(r"""<a\b[^>]*href=["']([^"']+)["']""", re.I)
_SQSP_SECTION_RE = re.compile(r"""data-url-id=["']([a-z0-9\-]+)["']""", re.I)
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)


# ── evidence ───────────────────────────────────────────────────────────────


@dataclass
class PageEvidence:
    """Everything one fetch of one URL can tell you, without a browser.

    The blueprint spends four separate `curl` invocations deriving these —
    headers, sitemap, nav links, feed grep. They all come out of the same bytes.
    """

    url: str
    ok: bool = False
    status: int | None = None
    error: str | None = None
    origin: str = "given"

    chars: int = 0
    date_tokens: int = 0
    time_tokens: int = 0
    dated_links: int = 0
    total_links: int = 0

    structured_events: int = 0
    rss_feeds: tuple[str, ...] = ()
    has_ld_json: bool = False
    has_next_data: bool = False
    blocked: bool = False

    #: Where this URL sat in the discovery ordering — lower is a stronger
    #: listing-shaped path. Used only to break ties that carry no evidence.
    hint_rank: int = 99

    @property
    def has_signal(self) -> bool:
        """Did anything here actually look like an event, without a browser?

        The honest gate. A JS-rendered venue page returns a nav bar and a
        cookie banner to `requests`, and every number below reads zero — which
        is not evidence that the page is empty, only that this module cannot
        see it. Ranking on zeroes is how a 406-char room-booking form beat the
        calendar it sits next to.

        A declared feed counts. A page publishing RSS has *told* you where its
        events are, and that answer does not depend on whether its HTML renders
        server-side — so a feed settles the question a browser would otherwise
        have to.
        """
        return bool(self.dated_links or self.structured_events
                    or self.date_tokens or self.rss_feeds)

    @property
    def date_density(self) -> float:
        """Date tokens per 1,000 characters of readable text.

        The Bitter End discriminator. The homepage and the calendar section had
        the same number of event links (zero); what separated them was how much
        of the page was schedule rather than furniture.
        """
        return (self.date_tokens / self.chars * 1000) if self.chars else 0.0

    @property
    def rank_key(self) -> tuple:
        """Dated links, then structured events, then density, then path shape.

        Density is a tie-break rather than a co-equal term on purpose. When a
        candidate has real dated links the link count is the better signal, and
        letting density outvote it would rank a wordy homepage above a sparse
        listing page that actually enumerates events.

        **Size is not in this key at all, and that is deliberate.** An earlier
        version ended `…, self.chars)` to encode "the Bitter End's 821-char
        calendar beat its 4,619-char homepage". That reading was wrong: fewer
        characters did not win, *higher concentration* won, and they only
        coincided. On a site where every candidate scores zero — which is every
        JS-rendered site, seen from `requests` — a size term silently becomes
        the whole ranking and picks the emptiest page on the domain.
        """
        return (
            -self.dated_links,
            -self.structured_events,
            -round(self.date_density, 3),
            self.hint_rank,
        )

    def to_dict(self) -> dict:
        return {
            "url": self.url, "origin": self.origin, "ok": self.ok,
            "status": self.status, "error": self.error, "chars": self.chars,
            "date_tokens": self.date_tokens, "time_tokens": self.time_tokens,
            "dated_links": self.dated_links, "total_links": self.total_links,
            "date_density": round(self.date_density, 2),
            "structured_events": self.structured_events,
            "has_signal": self.has_signal,
            "rss_feeds": list(self.rss_feeds), "has_ld_json": self.has_ld_json,
            "has_next_data": self.has_next_data, "blocked": self.blocked,
        }


@dataclass
class OnboardReport:
    """What to configure, why, and what the runner-up looked like."""

    given_url: str
    final_url: str
    redirect_chain: tuple[str, ...] = ()
    already_configured: str | None = None

    candidates: tuple[PageEvidence, ...] = ()
    best: PageEvidence | None = None
    mode: str | None = None
    #: One of :data:`SOURCE_KINDS` — direct / search / special.
    kind: str | None = None
    ics_feed: str | None = None

    #: No candidate showed an event signal, so the browser-free pass cannot
    #: rank them and has not tried. Candidates are still listed — this is a
    #: handoff to blueprint step 4, not a failure.
    needs_browser: bool = False

    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def changed_url(self) -> bool:
        """Did the reasoning move off the URL it was handed? The headline."""
        return bool(self.best and _norm(self.best.url) != _norm(self.given_url))

    def to_dict(self) -> dict:
        return {
            "given_url": self.given_url, "final_url": self.final_url,
            "redirect_chain": list(self.redirect_chain),
            "already_configured": self.already_configured,
            "changed_url": self.changed_url, "needs_browser": self.needs_browser,
            "mode": self.mode, "kind": self.kind, "ics_feed": self.ics_feed,
            "best": self.best.to_dict() if self.best else None,
            "candidates": [c.to_dict() for c in self.candidates],
            "warnings": list(self.warnings), "notes": list(self.notes),
        }


# ── the cheap probes, in blueprint order ───────────────────────────────────


def _norm(url: str) -> str:
    """Compare URLs without tripping over a trailing slash or a scheme."""
    p = urlparse(url if "://" in url else "https://" + url)
    host = (p.netloc or "").lower().removeprefix("www.")
    return f"{host}{(p.path or '').rstrip('/')}"


def _path_tokens(path: str) -> set[str]:
    """Split a URL path into words, so `/event-party-booking` yields
    ``{"event", "party", "booking"}`` and can be judged on any of them."""
    return {t for t in re.split(r"[/\-_.]+", (path or "").lower()) if t}


def _registrable(host: str) -> str:
    """Last two labels. Crude, and enough to notice a domain broker."""
    parts = (host or "").lower().removeprefix("www.").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def fetch(url: str, timeout_s: int = 15) -> tuple[str, list[str], PageEvidence]:
    """GET a URL, following redirects. Returns ``(html, chain, evidence)``.

    The redirect chain is returned rather than discarded because it is the
    whole of step 2: on 2026-08-04 `mato.com` resolved through `www.mato.com`
    to `brandforce.com/domain/Mato.com/`, a parked domain for sale. Reading
    only the final status would have called that a healthy 200 and handed a
    broker's sale page to the LLM every night.
    """
    import requests

    ev = PageEvidence(url=url)
    chain: list[str] = []
    try:
        r = requests.get(
            url, timeout=timeout_s, allow_redirects=True, stream=True,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
        )
        chain = [h.url for h in r.history] + [r.url]
        ev.status = r.status_code
        ev.url = r.url
        r.raise_for_status()

        chunks: list[bytes] = []
        total = 0
        for chunk in r.iter_content(8192):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total >= MAX_HTML_BYTES:
                break
        html = b"".join(chunks).decode(r.encoding or "utf-8", errors="replace")
        ev.ok = True
        return html, chain, ev
    except Exception as e:  # noqa: BLE001 - any failure is "no evidence"
        ev.error = f"{type(e).__name__}: {e}"
        logger.debug("onboard: fetch failed for %s: %s", url, e)
        return "", chain or [url], ev


def measure(url: str, html: str, ev: PageEvidence | None = None) -> PageEvidence:
    """Derive every cheap signal from HTML already in hand.

    One fetch answers what the blueprint spends four `curl`s on.
    """
    ev = ev or PageEvidence(url=url, ok=bool(html))
    text = _extract_text(html)
    ev.chars = len(text)
    ev.date_tokens = len(_DECIMATE_DATE_RE.findall(text))
    ev.time_tokens = len(_DECIMATE_TIME_RE.findall(text))
    ev.blocked = "just a moment" in html[:4000].lower()

    hrefs = _ANCHOR_RE.findall(html)
    ev.total_links = len(hrefs)
    ev.dated_links = _count_dated_links(html, url)

    ev.rss_feeds = tuple(_rss_feeds(html, url))
    ev.has_ld_json = 'application/ld+json' in html
    ev.has_next_data = "__NEXT_DATA__" in html or "__NUXT__" in html
    ev.structured_events = count_structured_events(html)
    return ev


def count_structured_events(html: str) -> int:
    """How many schema.org ``Event`` objects the HTML already carries.

    The blueprint is emphatic about this and it is the single cheapest win
    available: *"Do not assume a SPA renders client-side — measure it."* Luma is
    React and still ships its events as JSON-LD, which is how Gmail link
    expansion pulls exact date, time and venue off a Luma page with no browser.

    Sibling to ``web_utils._extract_structured_data``, which answers a different
    question — it returns the *first* event as a normalised dict for the
    pipeline. This counts them, because a count is what discriminates a listing
    page from a detail page.
    """
    import json as _json

    total = 0
    for m in re.finditer(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        try:
            data = _json.loads(m.group(1))
        except Exception:  # noqa: BLE001 - malformed JSON-LD is common and not our problem
            continue
        items = [data] if isinstance(data, dict) else data if isinstance(data, list) else []
        if isinstance(data, dict) and isinstance(data.get("@graph"), list):
            items = data["@graph"]
        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type", "")
            if isinstance(item_type, list):
                item_type = " ".join(str(t) for t in item_type)
            if "Event" in str(item_type):
                total += 1
    return total


#: An href whose own text or URL carries a date is an event link. This is the
#: browser-free approximation of `web_utils._extract_event_links`, which needs a
#: live page; it is deliberately conservative, since over-counting here would
#: promote a homepage over the listing page it links to.
_DATE_IN_URL_RE = re.compile(
    r"/(?:20\d{2})[-/](?:0?[1-9]|1[0-2])(?:[-/](?:0?[1-9]|[12]\d|3[01]))?"
    r"|/(?:20\d{2})\d{4}"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[-_]\d{1,2}",
    re.I,
)
_ANCHOR_WITH_TEXT_RE = re.compile(
    r"""<a\b[^>]*href=["']([^"']+)["'][^>]*>(.{0,160}?)</a>""", re.I | re.S,
)


def _count_dated_links(html: str, base: str) -> int:
    """Links that either sit at a dated URL or are labelled with a date."""
    seen: set[str] = set()
    for href, label in _ANCHOR_WITH_TEXT_RE.findall(html):
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full = urljoin(base, href)
        if full in seen:
            continue
        text = _extract_text(label)
        if _DATE_IN_URL_RE.search(full) or _DECIMATE_DATE_RE.search(text):
            seen.add(full)
    return len(seen)


def _rss_feeds(html: str, base: str) -> list[str]:
    """RSS/Atom feeds. `feed_probe` finds iCalendar only, and says so.

    The blueprint names this gap explicitly and tells the reader to grep for it
    by hand — including the warning that a naive `ical` grep matches the CSS
    property `vertical-align`. Anchoring on the declared MIME type and on whole
    filename shapes avoids that class of false positive entirely.
    """
    out: list[str] = []
    for m in _FEED_LINK_RE.finditer(html):
        href = m.group(2) or m.group(3)
        if href:
            out.append(urljoin(base, href))
    for href in _FEED_HREF_RE.findall(html):
        out.append(urljoin(base, href))
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq[:5]


def nav_candidates(html: str, base: str, limit: int = 8) -> list[str]:
    """Same-host links that look like a listing page.

    The highest-yield probe for both 2026-08-04 failures: `ma.to` exposed
    `/events/{city}` this way, and the Bitter End's `#calendar-section` anchor
    had its own URL.
    """
    host = urlparse(base).netloc.lower()
    scored: list[tuple[int, str]] = []
    seen: set[str] = set()
    for href in _ANCHOR_RE.findall(html):
        if href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        full = urljoin(base, href.split("#")[0]) if not href.startswith("#") else None
        if href.startswith("#"):
            continue
        if not full or urlparse(full).netloc.lower() != host:
            continue
        path = (urlparse(full).path or "/").lower()
        if path in ("", "/") or _norm(full) in seen:
            continue
        if _path_tokens(path) & _NAV_ANTI_TOKENS:
            continue
        if _DETAIL_PATH_RE.match(path):
            continue                      # one event's page, not a listing
        hit = next((i for i, h in enumerate(_NAV_HINTS) if h in path), None)
        if hit is None:
            continue
        seen.add(_norm(full))
        # Prefer the hint that ranked earlier, then the shallower path — a
        # `/events/san-francisco` beats `/events/san-francisco/2026/08/14`.
        scored.append((hit * 100 + path.count("/"), full))
    scored.sort()
    return [u for _, u in scored[:limit]]


def squarespace_sections(html: str, base: str, limit: int = 6) -> list[str]:
    """Squarespace one-pagers hide sections behind anchors.

    ``<div id="x-section" data-url-id="x">`` means ``/x`` exists as a real page
    with its own collection — which is how the Bitter End's calendar turned out
    to be addressable at all.
    """
    out: list[str] = []
    for slug in _SQSP_SECTION_RE.findall(html):
        if any(h in slug.lower() for h in _NAV_HINTS):
            out.append(urljoin(base, "/" + slug.strip("/")))
    seen, uniq = set(), []
    for u in out:
        if _norm(u) not in seen:
            seen.add(_norm(u))
            uniq.append(u)
    return uniq[:limit]


def sitemap_candidates(base: str, timeout_s: int = 10, limit: int = 8) -> list[str]:
    """Listing-shaped URLs from ``/sitemap.xml``. Reveals city routes."""
    import requests

    root = f"{urlparse(base).scheme or 'https'}://{urlparse(base).netloc}"
    try:
        r = requests.get(
            f"{root}/sitemap.xml", timeout=timeout_s,
            headers={"User-Agent": USER_AGENT, "Accept": "application/xml,*/*"},
        )
        if r.status_code >= 400:
            return []
        locs = _LOC_RE.findall(r.text[:MAX_HTML_BYTES])
    except Exception as e:  # noqa: BLE001
        logger.debug("onboard: sitemap fetch failed for %s: %s", root, e)
        return []

    out, seen = [], set()
    for loc in locs:
        path = (urlparse(loc).path or "").lower()
        if _path_tokens(path) & _NAV_ANTI_TOKENS:
            continue
        if any(h in path for h in _NAV_HINTS) and _norm(loc) not in seen:
            seen.add(_norm(loc))
            out.append(loc)
    return out[:limit]


# ── the judgement ──────────────────────────────────────────────────────────


def recommend_mode(ev: PageEvidence, ics_feed: str | None) -> tuple[str, list[str]]:
    """Pick a mode, and say out loud when the pick is a known trap.

    Order is the blueprint's §5 table: a real feed always beats scraping,
    because it is one item per event rather than one blob per page.
    """
    warnings: list[str] = []
    host = urlparse(ev.url).netloc.lower().removeprefix("www.")

    for platform, mode in _PLATFORM_MODES.items():
        if host == platform or host.endswith("." + platform):
            return mode, warnings

    if ics_feed:
        return "ics", warnings
    if ev.rss_feeds:
        return "rss", warnings
    if ev.blocked:
        warnings.append(
            "Cloudflare challenge unresolved — needs native_chrome: true plus a "
            "cooldown_hours. Cloudflare merely fronting a site is common and "
            "usually harmless; this is the case where it is not."
        )
    if ev.dated_links >= 3:
        return "calendar", warnings
    if ev.has_next_data and ev.dated_links == 0:
        warnings.append(
            "Server-rendered app data (__NEXT_DATA__/__NUXT__) with no dated "
            "links in the HTML. There is probably an API behind this — check "
            "for one before accepting `feed`, which needs an api_pattern."
        )
        return "feed", warnings

    # Everything else falls to `raw`, and `raw` is the trap.
    if ev.date_tokens >= RAW_DENSE_DATES:
        warnings.append(
            f"`raw` on a dense page: {ev.date_tokens} date strings found. "
            f"`raw` sends ONE message however many events the page lists, and "
            f"the model returns roughly one event from one message — measured "
            f"2026-08-07, a 338 KB listing of ~40 shows yielded 1 event while "
            f"the same venue's RSS yielded 65. Hunt harder for a feed."
        )
    return "raw", warnings


#: The three shapes a source comes in. This is the taxonomy the control UI
#: groups by, and it is about **how you get the events**, not about the venue.
SOURCE_KINDS = {
    "direct": (
        "Publishes its calendar. A feed, or a page whose HTML already lists "
        "dated events. Cheapest to run and cheapest to trust — nothing has to "
        "be driven, and a feed gives one item per event instead of one blob "
        "per page."
    ),
    "search": (
        "Has the events but will not show them until asked. Needs a query, a "
        "form fill, or a city/date route chosen before anything appears."
    ),
    "special": (
        "Needs machinery of its own. A bot challenge that only a real browser "
        "clears (erobay), a platform with a dedicated mode (Luma, Partiful, "
        "Instagram), or a listing that arrives from an XHR rather than the "
        "HTML (Meetup). These are the ones a generic scraper gets wrong "
        "quietly rather than loudly."
    ),
}

_SPECIAL_MODES = {"luma", "partiful", "instagram", "feed", "chat", "steps"}
_SEARCH_MODES = {"search"}
_DIRECT_MODES = {"ics", "rss", "calendar", "raw"}


def classify_kind(
    *,
    mode: str | None = None,
    evidence: PageEvidence | None = None,
    ics_feed: str | None = None,
    native_chrome: bool = False,
    has_query: bool = False,
) -> str:
    """Which of :data:`SOURCE_KINDS` this source is.

    Ordered most-specific first: a Cloudflare-fronted site that also publishes
    a feed is still `special`, because the thing that decides how it must be
    *run* outranks the thing that decides how it is parsed. Erobay is the case
    — it answers 403 to a plain request and 43 of the last harvest's 244 events
    came from it, so misfiling it as `direct` would be an expensive tidy.
    """
    if native_chrome or (evidence is not None and evidence.blocked):
        return "special"
    if mode in _SPECIAL_MODES:
        return "special"
    if mode in _SEARCH_MODES or has_query:
        return "search"
    if ics_feed or mode in _DIRECT_MODES:
        return "direct"
    return "special"


# ── feeds handed to us directly ────────────────────────────────────────────

_FEED_URL_RE = re.compile(
    r"\.(?:ics|rss|atom|xml)(?:$|\?)|/feed/?$|[?&]ical=1|^webcal://", re.I,
)


def looks_like_feed_url(url: str) -> str | None:
    """Is the URL *itself* a feed? Returns the kind, or ``None``.

    Configured sources are frequently already pointed at a feed —
    `bottom-of-the-hill` at an RSS URL, `dnalounge` at `webcal://…/dnalounge.ics`.
    Fetching those as HTML finds no links and no dates, so the reasoning below
    would confidently recommend `raw` for the one source the blueprint holds up
    as the RSS success story (65 events against `raw`'s 1). Detect and stop.
    """
    if not _FEED_URL_RE.search(url or ""):
        return None
    low = url.lower()
    if low.startswith("webcal://") or ".ics" in low or "ical=1" in low:
        return "ics"
    return "rss"


def validate_rss(url: str, timeout_s: int = 12) -> tuple[int, int]:
    """Fetch an RSS/Atom feed and return ``(items, items_with_dates)``.

    **A site-wide `/feed/` is usually the blog, not the calendar.** Six sources
    in the 2026-08-14 sweep were about to be "upgraded" from a working scrape
    to a WordPress blog feed on nothing more than the feed's existence — one of
    them literally `sfoasis.com/blog?format=rss`. Existence is not evidence;
    dated items are. This is the RSS analogue of what `feed_probe._validate`
    already does for iCalendar, and it exists for the same reason.
    """
    import requests

    try:
        r = requests.get(
            url, timeout=timeout_s,
            headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml,*/*"},
        )
        if r.status_code >= 400:
            return 0, 0
        body = r.text[:MAX_HTML_BYTES]
    except Exception as e:  # noqa: BLE001
        logger.debug("onboard: rss validate failed for %s: %s", url, e)
        return 0, 0

    entries = re.split(r"<(?:item|entry)\b", body, flags=re.I)[1:]
    dated = 0
    for entry in entries:
        # An events feed dates its *contents*; a blog feed dates its posts, so
        # pubDate alone proves nothing. Look for a date in the title or the
        # body text, which is where an event feed puts when the thing happens.
        head = entry[:1500]
        text = _extract_text(head)
        if _DECIMATE_DATE_RE.search(text):
            dated += 1
    return len(entries), dated


#: Below this many dated items, a feed is treated as a blog rather than a
#: calendar. Two is deliberate: one dated post is a coincidence in any blog.
RSS_DATED_MIN = 2


def _configured_match(url: str) -> str | None:
    """Is this host already configured? Step 1, and free."""
    try:
        from event_harvester.sources.web_utils import _load_web_sources
        sources = _load_web_sources() or []
    except Exception as e:  # noqa: BLE001 - a missing config is not an error here
        logger.debug("onboard: could not read configured sources: %s", e)
        return None

    host = urlparse(url if "://" in url else "https://" + url).netloc.lower()
    host = host.removeprefix("www.")
    for src in sources:
        src_url = str(src.get("url") or "")
        src_host = urlparse(src_url).netloc.lower().removeprefix("www.")
        if src_host and (src_host == host or _norm(src_url) == _norm(url)):
            return str(src.get("name") or src_url)
    return None


def explore(
    url: str,
    *,
    timeout_s: int = 15,
    max_candidates: int = 6,
    probe_ics: bool = True,
    check_configured: bool = True,
    validate_feeds: bool = True,
) -> OnboardReport:
    """Run the whole browser-free workflow and return what it found.

    Cheapest first, and it stops spending as soon as a step settles the
    question: a URL that is already configured never gets fetched, and a
    validated iCalendar feed short-circuits the candidate sweep because a real
    feed beats any scraping candidate the sweep could produce.
    """
    with tracer.start_as_current_span("onboard.explore") as span:
        span.set_attribute("onboard.url", url)
        if "://" not in url:
            url = "https://" + url
        report = OnboardReport(given_url=url, final_url=url)

        if check_configured:
            if (existing := _configured_match(url)):
                report.already_configured = existing
                report.notes.append(
                    f"Already configured as {existing!r}. Nothing fetched."
                )
                span.set_attribute("onboard.already_configured", True)
                return report

        # A URL that is already a feed is the answer, not a page to reason
        # about. Fetching it as HTML finds nothing and recommends `raw`.
        if (feed_kind := looks_like_feed_url(url)):
            report.mode = feed_kind
            report.kind = "direct"
            if feed_kind == "ics":
                report.ics_feed = url
            report.notes.append(
                f"The URL supplied is already {feed_kind.upper()}. Nothing to "
                f"choose — a feed gives one item per event, which is the "
                f"outcome the rest of this reasoning is trying to reach."
            )
            return report

        html, chain, ev = fetch(url, timeout_s=timeout_s)
        report.final_url = ev.url
        report.redirect_chain = tuple(chain)

        if len(chain) > 1 and _registrable(urlparse(chain[0]).netloc) != _registrable(
            urlparse(chain[-1]).netloc
        ):
            report.warnings.append(
                f"Redirects off-domain: {chain[0]} → {chain[-1]}. A parked or "
                f"for-sale domain looks exactly like this and answers 200 — "
                f"read the chain, not the status."
            )

        if not ev.ok:
            report.warnings.append(f"Could not fetch {url}: {ev.error}")
            report.candidates = (ev,)
            return report

        measure(ev.url, html, ev)
        found = [ev]

        if probe_ics:
            try:
                from event_harvester.sources.feed_probe import best_feed
                if (cand := best_feed(ev.url)):
                    report.ics_feed = cand.url
                    report.notes.append(
                        f"iCalendar feed with {cand.future_events} future "
                        f"event(s): {cand.url} — prefer this over scraping."
                    )
            except Exception as e:  # noqa: BLE001
                logger.debug("onboard: ics probe failed: %s", e)

        # A validated feed settles the mode; candidate sweeping cannot beat it,
        # so do not spend the requests.
        if not report.ics_feed:
            seen = {_norm(ev.url)}
            todo: list[tuple[str, str]] = []
            for u in squarespace_sections(html, ev.url):
                todo.append((u, "squarespace"))
            for u in nav_candidates(html, ev.url):
                todo.append((u, "nav"))
            for u in sitemap_candidates(ev.url, timeout_s=timeout_s):
                todo.append((u, "sitemap"))

            for rank, (cand_url, origin) in enumerate(todo):
                if len(found) - 1 >= max_candidates:
                    report.notes.append(
                        f"Candidate sweep capped at {max_candidates}; "
                        f"{len(todo)} were discovered."
                    )
                    break
                if _norm(cand_url) in seen:
                    continue
                seen.add(_norm(cand_url))
                c_html, _, c_ev = fetch(cand_url, timeout_s=timeout_s)
                c_ev.origin = origin
                c_ev.hint_rank = rank
                if c_ev.ok:
                    measure(c_ev.url, c_html, c_ev)
                found.append(c_ev)

        usable = [c for c in found if c.ok]
        report.candidates = tuple(sorted(usable, key=lambda c: c.rank_key)) or (ev,)
        report.best = report.candidates[0] if usable else None

        # Refuse to move off the given URL on no evidence. Every number this
        # module produces reads zero against a JS-rendered site, and a ranking
        # built from zeroes is noise wearing a recommendation's clothes — it
        # picked a 406-char room-booking form over the venue's calendar before
        # this check existed. Discovery is still reported; only the *claim* is
        # withheld, which is what step 4 of the blueprint (`web test`, with a
        # browser) exists to settle.
        if report.best is not None and not any(c.has_signal for c in usable):
            report.needs_browser = True
            report.best = ev if ev.ok else report.best
            report.warnings.append(
                f"No candidate showed an event signal without a browser — "
                f"{len(usable)} page(s) fetched, all with 0 dated links, 0 "
                f"JSON-LD events and 0 date strings. That is not evidence the "
                f"pages are empty, only that this reasoning cannot see them. "
                f"Run `web test` on the candidates below before configuring "
                f"anything; the recommendation here is withheld, not zero."
            )

        # A feed has to earn the recommendation. Existence is not evidence —
        # `/feed/` on a WordPress venue site is the blog, and swapping a
        # working scrape for it would be a downgrade wearing an upgrade's name.
        if validate_feeds and report.best and report.best.rss_feeds:
            kept: list[str] = []
            for feed_url in report.best.rss_feeds:
                items, dated = validate_rss(feed_url, timeout_s=timeout_s)
                if dated >= RSS_DATED_MIN:
                    kept.append(feed_url)
                    report.notes.append(
                        f"RSS with {dated} dated item(s) of {items}: {feed_url}"
                    )
                else:
                    report.notes.append(
                        f"Ignored {feed_url} — {items} item(s), only {dated} "
                        f"dated. Reads as a blog feed, not a calendar."
                    )
            report.best.rss_feeds = tuple(kept)

        if report.best:
            report.mode, mode_warnings = recommend_mode(report.best, report.ics_feed)
            report.warnings.extend(mode_warnings)
            report.kind = classify_kind(
                mode=report.mode, evidence=report.best, ics_feed=report.ics_feed,
            )
            if report.changed_url:
                report.notes.append(
                    f"Configure {report.best.url} — not the URL supplied. "
                    f"{report.best.dated_links} dated link(s), {report.best.chars} "
                    f"chars, density {report.best.date_density:.1f}/1k against "
                    f"{ev.dated_links} / {ev.chars} / {ev.date_density:.1f} for "
                    f"the page you gave."
                )
            span.set_attribute("onboard.mode", report.mode or "")
            span.set_attribute("onboard.changed_url", report.changed_url)
            span.set_attribute("onboard.candidates", len(report.candidates))

        return report
