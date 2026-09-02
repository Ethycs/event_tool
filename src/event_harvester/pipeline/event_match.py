"""Event fingerprinting and structured matching.

Compares events by normalized structured fields (title, date, location, link)
and maintains a fingerprint store for cross-run dedup.
"""

import logging
import string
from datetime import date
from urllib.parse import urlparse

from event_harvester.observability.tracing import set_span_attrs, traced
from event_harvester.utils import data_path, load_json, save_json

logger = logging.getLogger("event_harvester.pipeline.event_match")
_TRACER = "event_harvester.pipeline.event_match"


# Repo-anchored, not CWD-relative. As a bare `data/...` path this resolved
# against whatever directory the process started in — so the stdio MCP
# server (launched by its client, from anywhere) read and wrote a different
# fingerprint store than the CLI, and each was invisible to the other.
FINGERPRINT_FILE = data_path(".event_fingerprints.json")

_STOP_WORDS = frozenset({"the", "a", "an", "at", "in", "on", "for", "and", "of", "to"})

# A link is only a dedup key when it identifies THE EVENT. When a message
# carries no event URL (calendar rows don't), the LLM fills the field with
# either a placeholder ("TBD") or the bare source domain ("https://erobay.com")
# — both shared across every event from that source. A link match scores +2 (an
# instant match), so either one collapsed a whole day of distinct events into
# one. Measured on erobay 2026-09-02: 6 distinct same-day events → 1, every row
# carrying `link=https://erobay.com`. Both are normalized to None in
# `event_signature` so link only ever matches a specific event URL (one with a
# path), never a placeholder or a homepage.
_PLACEHOLDER_LINKS = frozenset({"tbd", "n/a", "na", "none", "null", "-", "--",
                                "unknown", "todo", "no link", "not available"})


def _link_key(link) -> str | None:
    """A link usable as a dedup key, or None. None for placeholders and for
    bare domains/homepages (no path), which identify a venue, not an event."""
    if not link or not isinstance(link, str):
        return None
    s = link.strip().lower()
    if not s or s in _PLACEHOLDER_LINKS:
        return None
    try:
        p = urlparse(s)
    except ValueError:
        return s  # unparseable but non-empty: keep as-is, it's at least specific
    if p.scheme and p.netloc and p.path.strip("/") == "" and not p.query:
        return None  # scheme://host with no path/query — a homepage, shared by many
    return s


def _normalize_date(date_val) -> str | None:
    """Normalize a date value to ISO YYYY-MM-DD or None.

    Accepts ISO strings, free-text dates (parsed via dateutil), or None.
    Returns None if the date cannot be parsed — never returns junk.

    Free-text guard: dateutil's fuzzy parser silently fabricates dates
    from strings with stray digits ("Event 2" → today-month-02). We
    parse twice with widely-spaced default years and require month and
    day to agree between the two parses — disagreement means the value
    came from the default rather than the input. Year may default
    (so "April 27" still resolves to the current year).
    """
    if not date_val:
        return None
    if not isinstance(date_val, str):
        return None
    s = date_val.strip()
    if not s:
        return None

    try:
        return date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        pass

    try:
        from datetime import datetime

        from dateutil import parser as dateutil_parser

        d_low = dateutil_parser.parse(s, fuzzy=True, default=datetime(1900, 1, 1))
        d_high = dateutil_parser.parse(s, fuzzy=True, default=datetime(2099, 12, 31))
    except (ValueError, OverflowError, TypeError):
        return None

    if d_low.month != d_high.month or d_low.day != d_high.day:
        return None

    if d_low.year == d_high.year:
        year = d_low.year
    elif d_low.year == 1900 and d_high.year == 2099:
        year = date.today().year
    else:
        return None

    if year < 2000 or year > 2100:
        return None
    try:
        return date(year, d_low.month, d_low.day).isoformat()
    except ValueError:
        return None


def _normalize_title(title: str) -> set[str]:
    """Lowercase, strip punctuation, remove stop words, return word set."""
    title = title.lower()
    title = title.translate(str.maketrans("", "", string.punctuation))
    words = set(title.split())
    return words - _STOP_WORDS


def _normalize_location(loc) -> set[str] | None:
    """Normalize a location string to a word set, or None if empty."""
    if not loc:
        return None
    if not isinstance(loc, str):
        return None
    loc = loc.lower().strip()
    if not loc:
        return None
    loc = loc.translate(str.maketrans("", "", string.punctuation))
    return set(loc.split()) - _STOP_WORDS


def _titles_overlap(a: set[str], b: set[str]) -> bool:
    """Check if word set overlap is >= 50% of the smaller set.

    Special case: identical non-empty sets always match (so "AI Meetup"
    matches itself even though it has only 2 words after stop-word removal).

    For non-identical sets, requires at least 2 overlapping words —
    otherwise titles like "Event A" and "Event B" (which differ only in
    a stop-word-stripped letter) would match each other.
    """
    if not a or not b:
        return False
    if a == b:
        return True
    overlap = a & b
    if len(overlap) < 2:
        return False
    smaller = min(len(a), len(b))
    return len(overlap) / smaller >= 0.5


def event_signature(event: dict) -> dict:
    """Extract normalized comparison fields from an event."""
    title = event.get("title", "") or ""
    location = event.get("location") or None
    link = event.get("link") or None

    return {
        "title_words": _normalize_title(title),
        "date": _normalize_date(event.get("date")),
        "location_words": _normalize_location(location),
        "link": _link_key(link),
    }


def events_match(a: dict, b: dict) -> tuple[bool, int]:
    """Compare two events by structured fields.

    Returns (is_match, score) where score = number of matching fields; a link
    match counts as 2. A match requires score >= 2 **and** agreement on the
    identity of the event — a title or a link.

    The identity guard matters because date and location are shared, not
    identifying: a venue hosts many distinct events on the same day, so
    date + location alone (score 2) would merge them. On erobay 2026-09-02
    'Go Topless! Subs Meetup' and "Miss Vicki's Munch" both carried the
    category "Munch" as their location on the same date and collapsed into one.
    Two events are the same event only when they share a title or a specific
    URL — never merely a day and a room.
    """
    score = _sig_match_score(event_signature(a), event_signature(b))
    return (score >= 2, score)


# --------------- Fingerprint store ---------------


@traced("fingerprint.load", tracer_name=_TRACER)
def load_fingerprints() -> list[dict]:
    """Load fingerprints from JSON, auto-prune expired entries.

    Pruning rules (in order):
      1. If `date` is a parseable ISO date in the past → drop.
      2. If `date` is unparseable AND created_at > 30 days ago → drop.
      3. If `created_at` > 30 days ago (no date) → drop.
      4. Otherwise → keep.
    """
    data = load_json(FINGERPRINT_FILE, default=[])
    if not data:
        return []

    today = date.today()
    pruned = []
    for fp in data:
        fp_date = fp.get("date")
        created_at = fp.get("created_at")

        # Try to parse the event date
        date_is_past = False
        date_parseable = False
        if fp_date and isinstance(fp_date, str):
            try:
                if date.fromisoformat(fp_date[:10]) < today:
                    date_is_past = True
                date_parseable = True
            except ValueError:
                pass  # falls through to created_at check

        if date_is_past:
            continue  # expired by event date

        # Fall back to created_at staleness for entries without parseable dates
        if not date_parseable and created_at:
            try:
                created = date.fromisoformat(created_at)
                if (today - created).days > 30:
                    continue  # too old to keep without a usable date
            except ValueError:
                pass

        pruned.append(fp)

    # Pruning is silent and destructive-looking from the outside: an event the
    # user declined last month stops being suppressed once its fingerprint
    # ages out. The counts make that legible.
    set_span_attrs(**{
        "fingerprint.loaded": len(data),
        "fingerprint.kept": len(pruned),
        "fingerprint.pruned": len(data) - len(pruned),
    })
    return pruned


@traced("fingerprint.save", tracer_name=_TRACER)
def save_fingerprint(
    event: dict,
    ticktick_id: str | None = None,
    *,
    status: str = "auto",
) -> None:
    """Add a fingerprint for an event to the store.

    Dates are normalized to ISO YYYY-MM-DD on write so the pruning logic
    in load_fingerprints() can correctly drop expired entries.

    `status` records *why* the entry was saved:
      - "auto"      — saved during a fetch/extract pass (e.g. web link
                      dedup); should NOT cause future events to be hidden
                      from the user.
      - "approved"  — user clicked Approve in the UI.
      - "declined"  — user clicked Decline in the UI.

    Only "approved" and "declined" entries cause `find_acted_fingerprint`
    to filter — auto entries serve as cheap pre-fetch dedup hints only.
    """
    fps = load_fingerprints()

    entry = {
        "title": event.get("title", ""),
        "date": _normalize_date(event.get("date")),
        "time": event.get("time"),
        "location": event.get("location"),
        "link": event.get("link"),
        "ticktick_id": ticktick_id,
        "status": status,
        "created_at": date.today().isoformat(),
    }

    fps.append(entry)
    save_json(FINGERPRINT_FILE, fps)


_ACTED_STATUSES = frozenset({"approved", "declined"})


def find_fingerprint(event: dict) -> dict | None:
    """Find a matching fingerprint for the given event.

    Matches against ALL stored fingerprints regardless of status — used
    for pre-fetch link dedup in web sources, where any prior sighting is
    a useful skip hint.
    """
    fps = load_fingerprints()
    for fp in fps:
        is_match, score = events_match(event, fp)
        if is_match:
            return fp
    return None


def find_acted_fingerprint(event: dict) -> dict | None:
    """Find a matching fingerprint, but only if the user has acted on it.

    Returns the fingerprint only when its status is "approved" or
    "declined" — i.e. the user has explicitly confirmed they have seen
    this event. Auto-saved entries (web link dedup, etc.) are ignored.

    Use this for the user-visible "hide previously seen events" filter.
    """
    fps = load_fingerprints()
    for fp in fps:
        if fp.get("status") not in _ACTED_STATUSES:
            continue
        is_match, score = events_match(event, fp)
        if is_match:
            return fp
    return None


@traced("dedup.within_run", tracer_name=_TRACER)
def dedup_events(events: list[dict]) -> list[dict]:
    """Within-run dedup using events_match. Returns unique events.

    Uses a link index for O(1) exact-link dedup and caches signatures
    to avoid recomputing them in the fuzzy-match inner loop.
    """
    set_span_attrs(**{"dedup.event.in": len(events)})
    unique: list[dict] = []
    unique_sigs: list[dict] = []
    seen_links: dict[str, dict] = {}

    def _merge_provenance(kept: dict, dropped: dict) -> None:
        """Carry the discarded twin's source messages onto the survivor.

        A duplicate is otherwise thrown away whole. That is right for the
        event — it *is* the same event — but wrong for its provenance: two
        emails announcing one meetup are two emails, and the Gmail labeller
        should mark both. Added 2026-08-10 with ``_source_ids``.
        """
        extra = dropped.get("_source_ids") or []
        if not extra:
            return
        ids = kept.setdefault("_source_ids", [])
        ids.extend(mid for mid in extra if mid not in ids)

    for event in events:
        sig = event_signature(event)

        # Fast path: exact link match
        link = sig["link"]
        if link and link in seen_links:
            _merge_provenance(seen_links[link], event)
            continue

        # Slow path: fuzzy match against existing unique events
        matched = False
        for i, existing_sig in enumerate(unique_sigs):
            score = _sig_match_score(sig, existing_sig)
            if score >= 2:
                matched = True
                _merge_provenance(unique[i], event)
                break

        if not matched:
            unique.append(event)
            unique_sigs.append(sig)
            if link:
                seen_links[link] = event

    set_span_attrs(**{
        "dedup.event.out": len(unique),
        "dedup.collapsed": len(events) - len(unique),
    })
    return unique


def _sig_match_score(sig_a: dict, sig_b: dict) -> int:
    """Compare two pre-computed signatures. Returns match score.

    The score is what the caller thresholds at >= 2. It carries the identity
    guard: without agreement on title or link, date and location alone (both
    shared, not identifying) are capped below the threshold, so two distinct
    events in the same room on the same day never read as one. This is the sole
    scorer for both `events_match` and `dedup_events` — the two must not drift.
    """
    score = 0
    title_hit = _titles_overlap(sig_a["title_words"], sig_b["title_words"])
    if title_hit:
        score += 1
    if sig_a["date"] and sig_b["date"] and sig_a["date"] == sig_b["date"]:
        score += 1
    loc_a, loc_b = sig_a["location_words"], sig_b["location_words"]
    if loc_a and loc_b and _titles_overlap(loc_a, loc_b):
        score += 1
    link_hit = bool(sig_a["link"] and sig_b["link"] and sig_a["link"] == sig_b["link"])
    if link_hit:
        score += 2
    if not (title_hit or link_hit):
        return min(score, 1)  # date+location is not identity
    return score
