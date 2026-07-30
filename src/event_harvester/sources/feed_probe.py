"""Feed discovery — find a machine-readable calendar feed without a browser.

Most small venues do not hand-roll their events page. They run a platform
(WordPress + The Events Calendar, Tockify, Squarespace, an embedded Google
Calendar), and every one of those platforms already publishes the same data
as iCalendar at a *predictable* URL. Two sources in ``data/web_sources.json``
prove the point::

    powerhouse   ics   https://powerhousebar.com/events/?ical=1
    dnalounge    ics   webcal://cdn.dnalounge.com/calendar/dnalounge.ics

``?ical=1`` is not a Powerhouse fact — it is The Events Calendar plugin's
standard export. Recognising the *platform* is worth far more than
recognising the site: one pattern converts every venue running that plugin.

So this module runs **before** the browser. It fetches one page of HTML and
looks for a feed four ways (standards-first, guesswork last), then *validates*
every candidate by actually parsing it. Nothing is trusted because it matched
a pattern — a candidate counts only if ``icalendar`` parses it and it contains
at least one VEVENT. That is what makes the speculative probes safe: a wrong
guess costs one request and is discarded, so the pattern table can be
optimistic without lying to the caller.

A hit turns a browser-driven ``raw``/``calendar`` source into an ``ics`` one,
which is faster, more reliable, immune to Cloudflare, and already a supported
mode (:mod:`event_harvester.sources.web_modes.ics`).

`known rough edge`: HTML scanning is regex-based. The project has no HTML
parser dependency (no bs4/lxml), and pulling one in for four attribute
lookups was not worth it. Regex over HTML is famously fragile, which is
tolerable *here* only because every candidate is validated by fetching it —
a missed or malformed match degrades to "no feed found", never to a bad
source entry.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse, urlunparse

from event_harvester.observability.tracing import get_tracer

logger = logging.getLogger("event_harvester.feed_probe")
tracer = get_tracer("event_harvester.sources.feed_probe")

USER_AGENT = "event-harvester/1.0 (+feed-probe)"

# One page fetch + at most this many candidate validations. A venue homepage
# that matches every pattern in the table would otherwise fire ~15 requests at
# a small business's server for one `web add`.
MAX_VALIDATIONS = 12

# Body cap for the initial HTML fetch. Venue homepages are ~50-500KB; anything
# far past that is a video-heavy page whose <head> we already have.
MAX_HTML_BYTES = 2_000_000

# Candidate origins, best first. Ties in validated event count break this way,
# so a declared feed always beats a lucky guess at the same event count.
ORIGIN_RANK = {
    "link-rel": 0,      # <link rel="alternate" type="text/calendar"> — the standard
    "embed": 1,         # an iframe pointing at a calendar platform
    "anchor": 2,        # a visible link ending .ics / carrying ical=1
    "platform": 3,      # platform fingerprint -> known export URL
    "blind": 4,         # untargeted probe of a conventional path
}


@dataclass
class FeedCandidate:
    """A URL that might be a calendar feed, plus what validation found.

    ``events``/``future_events`` are ``0`` until :func:`_validate` runs.
    ``valid`` is the only field callers should gate on.
    """

    url: str
    origin: str
    platform: str | None = None
    valid: bool = False
    events: int = 0
    future_events: int = 0
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def rank_key(self) -> tuple:
        """Sort key — more future events first, then origin quality."""
        return (-self.future_events, -self.events, ORIGIN_RANK.get(self.origin, 9))

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "origin": self.origin,
            "platform": self.platform,
            "valid": self.valid,
            "events": self.events,
            "future_events": self.future_events,
            "error": self.error,
        }


# ── Platform fingerprints ─────────────────────────────────────────────
#
# (marker regex, platform name, [URL templates]). ``{base}`` is the site root
# (scheme://netloc), ``{url}`` the page as given, minus query/fragment.
#
# Templates are deliberately speculative. Validation is the filter, so a
# wrong guess costs one request. `unverified`: only the WordPress/Tribe and
# Google Calendar templates below were confirmed against a live site in this
# repo (powerhousebar.com and the embed format respectively); the rest are
# written from the platforms' documented conventions and have not been
# exercised end-to-end here.

_PLATFORM_FINGERPRINTS: list[tuple[str, str, list[str]]] = [
    (
        r"tribe-events|tribe_events|the-events-calendar",
        "wordpress-the-events-calendar",
        [
            "{base}/events/?ical=1",
            "{url}?ical=1",
            "{base}/?post_type=tribe_events&ical=1",
            "{base}/events/list/?ical=1",
        ],
    ),
    (
        r"events-manager|em-calendar|/plugins/events-manager",
        "wordpress-events-manager",
        ["{base}/events/feed/", "{base}/?feed=eventsical"],
    ),
    (
        r"tockify",
        "tockify",
        # Tockify calendars are addressed by slug; pull it from any tockify URL
        # on the page. Handled specially in _candidates_from_platform.
        [],
    ),
    (
        r"Static\.SQUARESPACE_CONTEXT|squarespace\.com",
        "squarespace",
        ["{url}?format=ical", "{base}/events?format=ical"],
    ),
    (
        r"wp-content|wp-includes",
        "wordpress-generic",
        ["{base}/events/feed/", "{base}/calendar/feed/"],
    ),
]

# Tried only when no fingerprint matched and nothing was declared. Ordered by
# how often they pay off, and kept short on purpose — this is guessing at
# someone else's server.
_BLIND_PATHS = [
    "/events/?ical=1",
    "/events.ics",
    "/calendar.ics",
    "/events/feed/",
]


# ── HTML scanning ─────────────────────────────────────────────────────

_LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.I)
_IFRAME_SRC_RE = re.compile(r"<iframe\b[^>]*\bsrc=[\"']([^\"']+)[\"']", re.I)
_HREF_RE = re.compile(r"\bhref=[\"']([^\"']+)[\"']", re.I)
_ATTR_RE = re.compile(r"\b([a-zA-Z-]+)\s*=\s*[\"']([^\"']*)[\"']")
_GCAL_SRC_RE = re.compile(r"[?&]src=([^&\"'\s]+)", re.I)
_TOCKIFY_SLUG_RE = re.compile(r"tockify\.com/(?:api/)?([A-Za-z0-9_.-]+)", re.I)

# A dated permalink segment: /2026/7/30/ or /2026/07/30/. Squarespace (and
# several other CMSes) put a per-event "Add to calendar" export at
# ``<event-permalink>?format=ical``, which validates perfectly as iCalendar
# and contains exactly one event -- the single event you were looking at.
#
# Found the hard way: probing studsf.com/calendar returned
#   /calendar/2026/7/30/140-olivia-lauren-amp-friends?format=ical  (1 event)
# and nothing else. Adopting that would have replaced a working calendar
# source with a feed permanently pinned to one night in July. The collection
# URL (``/calendar?format=ical``) serves nothing, so there is no feed here at
# all -- "none" is the correct answer, and these links must not disguise it.
_DATED_PERMALINK_RE = re.compile(r"/(?:19|20)\d{2}/\d{1,2}/\d{1,2}(?:/|$|\?)")


def _site_base(url: str) -> str:
    """Scheme://netloc for ``url``, no trailing slash."""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _strip_query(url: str) -> str:
    """``url`` with query and fragment removed, no trailing slash."""
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path.rstrip("/"), "", "", ""))


def _looks_like_ics_url(u: str) -> bool:
    low = u.lower()
    return (
        low.startswith(("webcal://", "webcals://"))
        or low.rstrip("/").endswith(".ics")
        or "ical=1" in low
        or "format=ical" in low
        or "format=ics" in low
    )


def _candidates_from_link_rel(html: str, page_url: str) -> list[FeedCandidate]:
    """`<link rel="alternate" type="text/calendar" href="...">` — the standard.

    Also accepts a ``.ics`` href on any ``<link>`` regardless of ``type``,
    since plenty of sites declare the link but get the MIME type wrong.
    """
    out: list[FeedCandidate] = []
    with tracer.start_as_current_span("feedprobe.discover.link_rel") as span:
        for tag in _LINK_TAG_RE.findall(html):
            attrs = {k.lower(): v for k, v in _ATTR_RE.findall(tag)}
            href = attrs.get("href")
            if not href:
                continue
            typ = (attrs.get("type") or "").lower()
            rel = (attrs.get("rel") or "").lower()
            declared = "text/calendar" in typ or "calendar" in rel
            if declared or _looks_like_ics_url(href):
                out.append(
                    FeedCandidate(
                        url=urljoin(page_url, href),
                        origin="link-rel",
                        meta={"declared_type": typ or None, "rel": rel or None},
                    )
                )
        span.set_attribute("candidate.count", len(out))
    return out


def _is_single_event_export(url: str) -> bool:
    """True for a *dated* per-event "add to calendar" link.

    See :data:`_DATED_PERMALINK_RE`. Checked at discovery time so these never
    cost a validation request, and never outrank a real feed on a page that
    has both. Undated permalinks need the event count to distinguish them —
    see :func:`_is_nested_single_event`.
    """
    return bool(_DATED_PERMALINK_RE.search(urlparse(url).path or url))


def _is_nested_single_event(cand: FeedCandidate, page_url: str) -> bool:
    """True for a one-event export living *underneath* the page we probed.

    The dated-permalink check above misses Squarespace's opaque slugs::

        page:      https://studsf.com/calendar
        candidate: https://studsf.com/calendar/h4wr7zbsakm3fye-...?format=ical

    Same trap, no date to match on. What still distinguishes it is structure
    plus size: a real collection feed is not usually a child of the
    collection page *and* down to a single event. Requiring both keeps a
    genuine ``/calendar/all.ics`` (which will have several) while dropping
    the per-event exports.

    Only applies when the probed URL has a path — probing a bare domain makes
    every candidate a "child" and the rule meaningless.
    """
    if cand.events > 1:
        return False
    page_path = urlparse(page_url).path.rstrip("/")
    if not page_path:
        return False
    cand_path = urlparse(cand.url).path
    return cand_path.startswith(page_path + "/")


def _candidates_from_anchors(html: str, page_url: str) -> list[FeedCandidate]:
    """Visible links that look like calendar exports (`.ics`, `?ical=1`).

    Per-event exports are dropped here — an events page often carries one per
    listing, and each validates as a real (one-event) calendar.
    """
    out: list[FeedCandidate] = []
    with tracer.start_as_current_span("feedprobe.discover.anchors") as span:
        seen: set[str] = set()
        skipped_single = 0
        for href in _HREF_RE.findall(html):
            if not _looks_like_ics_url(href):
                continue
            full = urljoin(page_url, href)
            if full in seen:
                continue
            seen.add(full)
            if _is_single_event_export(full):
                skipped_single += 1
                continue
            out.append(FeedCandidate(url=full, origin="anchor"))
        span.set_attribute("candidate.count", len(out))
        span.set_attribute("candidate.skipped_single_event", skipped_single)
        if skipped_single:
            logger.debug(
                "feed_probe: skipped %d per-event export link(s) on %s",
                skipped_single, page_url,
            )
    return out


def _candidates_from_embeds(html: str, page_url: str) -> list[FeedCandidate]:
    """Calendar platforms embedded in an iframe.

    A Google Calendar embed carries the calendar id in ``?src=``, and every
    public Google calendar exposes the same ICS URL derived from that id —
    so an embed is a *declaration* of the feed, not a guess.
    """
    out: list[FeedCandidate] = []
    with tracer.start_as_current_span("feedprobe.discover.embeds") as span:
        for src in _IFRAME_SRC_RE.findall(html):
            full = urljoin(page_url, src)
            low = full.lower()

            if "calendar.google.com" in low:
                m = _GCAL_SRC_RE.search(full)
                if m:
                    cal_id = m.group(1).replace("%40", "@")
                    out.append(
                        FeedCandidate(
                            url=(
                                "https://calendar.google.com/calendar/ical/"
                                f"{cal_id}/public/basic.ics"
                            ),
                            origin="embed",
                            platform="google-calendar",
                            meta={"calendar_id": cal_id},
                        )
                    )
            elif "tockify.com" in low:
                m = _TOCKIFY_SLUG_RE.search(full)
                if m:
                    out.extend(_tockify_candidates(m.group(1)))

        span.set_attribute("candidate.count", len(out))
    return out


def _tockify_candidates(slug: str) -> list[FeedCandidate]:
    """Tockify ICS export URLs for a calendar slug.

    `unverified` — Tockify's public export path has moved before; both known
    shapes are emitted and validation picks whichever answers.
    """
    skip = {"api", "www", "public", "embed", "js", "css"}
    if slug.lower() in skip:
        return []
    return [
        FeedCandidate(
            url=f"https://tockify.com/api/feeds/ics/{slug}",
            origin="platform",
            platform="tockify",
        ),
        FeedCandidate(
            url=f"https://tockify.com/{slug}/ics",
            origin="platform",
            platform="tockify",
        ),
    ]


def _candidates_from_platform(html: str, page_url: str) -> list[FeedCandidate]:
    """Fingerprint the hosting platform, then emit its known export URLs."""
    out: list[FeedCandidate] = []
    base = _site_base(page_url)
    stripped = _strip_query(page_url)

    with tracer.start_as_current_span("feedprobe.discover.platform") as span:
        matched: list[str] = []
        for marker, platform, templates in _PLATFORM_FINGERPRINTS:
            if not re.search(marker, html, re.I):
                continue
            matched.append(platform)

            if platform == "tockify":
                m = _TOCKIFY_SLUG_RE.search(html)
                if m:
                    out.extend(_tockify_candidates(m.group(1)))
                continue

            for tpl in templates:
                out.append(
                    FeedCandidate(
                        url=tpl.format(base=base, url=stripped),
                        origin="platform",
                        platform=platform,
                    )
                )

        span.set_attribute("platform.matched", ",".join(matched) or "none")
        span.set_attribute("candidate.count", len(out))
        if matched:
            logger.debug("feed_probe: platform fingerprint %s for %s", matched, page_url)
    return out


def _candidates_blind(page_url: str) -> list[FeedCandidate]:
    """Conventional paths, tried only as a last resort."""
    base = _site_base(page_url)
    with tracer.start_as_current_span("feedprobe.discover.blind") as span:
        out = [FeedCandidate(url=base + p, origin="blind") for p in _BLIND_PATHS]
        span.set_attribute("candidate.count", len(out))
    return out


# ── Fetch + validate ──────────────────────────────────────────────────


def _fetch_page(url: str, timeout_s: int) -> str | None:
    """GET ``url`` and return HTML text, or ``None`` on any failure."""
    import requests

    with tracer.start_as_current_span("feedprobe.fetch_page") as span:
        span.set_attribute("http.url", url)
        try:
            r = requests.get(
                url,
                timeout=timeout_s,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
                stream=True,
            )
            span.set_attribute("http.status_code", r.status_code)
            r.raise_for_status()

            # Streamed + capped: a venue homepage is small, but this runs
            # against arbitrary URLs and one video-heavy page should not be
            # able to pull hundreds of MB into memory.
            chunks: list[bytes] = []
            total = 0
            for chunk in r.iter_content(8192):
                if not chunk:
                    continue
                chunks.append(chunk)
                total += len(chunk)
                if total >= MAX_HTML_BYTES:
                    span.set_attribute("http.response_truncated", True)
                    break
            content = b"".join(chunks)
            html = content.decode(r.encoding or "utf-8", errors="replace")
            span.set_attribute("http.response_bytes", len(content))
            return html
        except Exception as e:
            span.set_attribute("error.message", str(e))
            span.record_exception(e)
            logger.debug("feed_probe: page fetch failed for %s: %s", url, e)
            return None


def _count_events(ics_bytes: bytes) -> tuple[int, int]:
    """Return ``(total_vevents, future_vevents)``; raises if it isn't iCalendar."""
    from icalendar import Calendar

    cal = Calendar.from_ical(ics_bytes)
    now = datetime.now(timezone.utc)
    today = now.date()

    total = 0
    future = 0
    for component in cal.walk("VEVENT"):
        total += 1
        dtstart = component.get("dtstart")
        if dtstart is None:
            continue
        dt = getattr(dtstart, "dt", None)
        try:
            if isinstance(dt, datetime):
                # Naive datetimes are assumed UTC; the alternative is guessing
                # the venue's tz, and an hours-scale error does not change
                # whether a feed is worth using.
                cmp_dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                if cmp_dt >= now:
                    future += 1
            elif isinstance(dt, date):
                if dt >= today:
                    future += 1
        except Exception:  # pragma: no cover - exotic dtstart types
            continue
    return total, future


def _validate(cand: FeedCandidate, timeout_s: int) -> FeedCandidate:
    """Fetch a candidate and confirm it really is a calendar feed.

    Mutates and returns ``cand``. Never raises — a candidate that fails
    validation is simply ``valid=False`` with ``error`` set.
    """
    import requests

    from event_harvester.sources.web_modes.ics import _normalize_url

    with tracer.start_as_current_span("feedprobe.validate") as span:
        url = _normalize_url(cand.url)
        span.set_attribute("http.url", url)
        span.set_attribute("candidate.origin", cand.origin)
        if cand.platform:
            span.set_attribute("candidate.platform", cand.platform)

        try:
            r = requests.get(
                url,
                timeout=timeout_s,
                headers={"User-Agent": USER_AGENT, "Accept": "text/calendar,*/*"},
            )
            span.set_attribute("http.status_code", r.status_code)
            r.raise_for_status()

            body = r.content
            # Cheap reject before handing anything to the parser: a WordPress
            # 404 page is a 200 with HTML, and icalendar will happily spend
            # time on it before failing.
            if b"BEGIN:VCALENDAR" not in body[:4096].upper():
                cand.error = "not iCalendar (no BEGIN:VCALENDAR)"
                span.set_attribute("candidate.valid", False)
                span.set_attribute("candidate.reject_reason", cand.error)
                return cand

            total, future = _count_events(body)
            cand.events = total
            cand.future_events = future
            cand.valid = total > 0
            if not cand.valid:
                cand.error = "parsed, but contains no VEVENT"
            span.set_attribute("candidate.valid", cand.valid)
            span.set_attribute("event.count", total)
            span.set_attribute("event.future_count", future)
        except Exception as e:
            cand.error = str(e)
            span.set_attribute("candidate.valid", False)
            span.set_attribute("error.message", str(e))
            span.record_exception(e)
        return cand


def _dedupe(cands: Iterable[FeedCandidate]) -> list[FeedCandidate]:
    """Drop duplicate URLs, keeping the best-ranked origin for each."""
    best: dict[str, FeedCandidate] = {}
    for c in cands:
        key = c.url.rstrip("/")
        prior = best.get(key)
        if prior is None or ORIGIN_RANK.get(c.origin, 9) < ORIGIN_RANK.get(prior.origin, 9):
            best[key] = c
    return list(best.values())


# ── Public API ────────────────────────────────────────────────────────


def probe_feeds(
    url: str,
    *,
    timeout_s: int = 10,
    allow_blind: bool = True,
    max_validations: int = MAX_VALIDATIONS,
) -> list[FeedCandidate]:
    """Find calendar feeds for ``url``. Returns validated candidates, best first.

    Runs four discovery lanes in decreasing order of trustworthiness, then
    validates each candidate by fetching and parsing it. Blind probes are
    only attempted when the first three lanes found nothing, so a site that
    declares its feed never gets speculatively poked.

    Returns ``[]`` when nothing valid was found — including when the page
    itself could not be fetched.
    """
    with tracer.start_as_current_span("feedprobe") as span:
        span.set_attribute("probe.url", url)
        span.set_attribute("probe.allow_blind", allow_blind)

        # An ICS URL needs no discovery — validate it directly.
        if _looks_like_ics_url(url):
            span.set_attribute("probe.shortcut", "already_ics")
            cand = _validate(FeedCandidate(url=url, origin="link-rel"), timeout_s)
            found = [cand] if cand.valid else []
            span.set_attribute("probe.valid_count", len(found))
            return found

        html = _fetch_page(url, timeout_s)
        if html is None:
            span.set_attribute("probe.page_fetched", False)
            span.set_attribute("probe.valid_count", 0)
            return []
        span.set_attribute("probe.page_fetched", True)

        declared = _dedupe([
            *_candidates_from_link_rel(html, url),
            *_candidates_from_embeds(html, url),
            *_candidates_from_anchors(html, url),
            *_candidates_from_platform(html, url),
        ])

        candidates = declared
        if not declared and allow_blind:
            candidates = _dedupe(_candidates_blind(url))
            span.set_attribute("probe.used_blind", True)
        else:
            span.set_attribute("probe.used_blind", False)

        span.set_attribute("probe.candidate_count", len(candidates))

        # Validate in origin order so the cap, when it bites, drops guesses
        # rather than declarations.
        candidates.sort(key=lambda c: ORIGIN_RANK.get(c.origin, 9))
        if len(candidates) > max_validations:
            logger.debug(
                "feed_probe: capping %d candidates at %d for %s",
                len(candidates), max_validations, url,
            )
            span.set_attribute("probe.candidates_dropped", len(candidates) - max_validations)
            candidates = candidates[:max_validations]

        validated = [_validate(c, timeout_s) for c in candidates]

        valid: list[FeedCandidate] = []
        nested_single = 0
        for c in validated:
            if not c.valid:
                continue
            if _is_nested_single_event(c, url):
                nested_single += 1
                logger.debug("feed_probe: dropping per-event export %s", c.url)
                continue
            valid.append(c)
        span.set_attribute("probe.dropped_single_event", nested_single)
        valid.sort(key=lambda c: c.rank_key)

        span.set_attribute("probe.valid_count", len(valid))
        if valid:
            span.set_attribute("probe.best_url", valid[0].url)
            span.set_attribute("probe.best_origin", valid[0].origin)
            logger.info(
                "feed_probe: %s -> %s (%d events, %d upcoming, via %s)",
                url, valid[0].url, valid[0].events, valid[0].future_events, valid[0].origin,
            )
        return valid


def best_feed(url: str, **kw) -> FeedCandidate | None:
    """Highest-ranked validated feed for ``url``, or ``None``."""
    found = probe_feeds(url, **kw)
    return found[0] if found else None
