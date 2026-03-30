"""Event fingerprinting and structured matching.

Compares events by normalized structured fields (title, date, location, link)
and maintains a fingerprint store for cross-run dedup.
"""

import json
import logging
import re
import string
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger("event_harvester.event_match")

FINGERPRINT_FILE = Path("data/.event_fingerprints.json")

_STOP_WORDS = frozenset({"the", "a", "an", "at", "in", "on", "for", "and", "of", "to"})


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
    """Check if word set overlap is >= 50% of the smaller set."""
    if not a or not b:
        return False
    overlap = len(a & b)
    smaller = min(len(a), len(b))
    return overlap / smaller >= 0.5


def event_signature(event: dict) -> dict:
    """Extract normalized comparison fields from an event."""
    title = event.get("title", "") or ""
    date_val = event.get("date") or None
    location = event.get("location") or None
    link = event.get("link") or None

    # Normalize date to YYYY-MM-DD string
    if date_val and isinstance(date_val, str):
        date_val = date_val[:10]  # take just the date portion

    return {
        "title_words": _normalize_title(title),
        "date": date_val,
        "location_words": _normalize_location(location),
        "link": link.strip().lower() if link else None,
    }


def events_match(a: dict, b: dict) -> tuple[bool, int]:
    """Compare two events by structured fields.

    Returns (is_match, score) where score = number of matching fields.
    Link match counts as 2. Match requires score >= 2.
    """
    sig_a = event_signature(a)
    sig_b = event_signature(b)
    score = 0

    # Title match (word overlap >= 50%)
    if _titles_overlap(sig_a["title_words"], sig_b["title_words"]):
        score += 1

    # Date match
    if sig_a["date"] and sig_b["date"] and sig_a["date"] == sig_b["date"]:
        score += 1

    # Location match
    loc_a = sig_a["location_words"]
    loc_b = sig_b["location_words"]
    if loc_a and loc_b and _titles_overlap(loc_a, loc_b):
        score += 1

    # Link match (counts as 2)
    if sig_a["link"] and sig_b["link"] and sig_a["link"] == sig_b["link"]:
        score += 2

    return (score >= 2, score)


# --------------- Fingerprint store ---------------


def load_fingerprints() -> list[dict]:
    """Load fingerprints from JSON, auto-prune expired entries."""
    if not FINGERPRINT_FILE.exists():
        return []
    try:
        data = json.loads(FINGERPRINT_FILE.read_text())
    except Exception:
        return []

    today = date.today()
    pruned = []
    for fp in data:
        fp_date = fp.get("date")
        created_at = fp.get("created_at")

        if fp_date:
            try:
                if date.fromisoformat(fp_date) < today:
                    continue  # expired
            except ValueError:
                pass
        elif created_at:
            try:
                created = date.fromisoformat(created_at)
                if (today - created).days > 30:
                    continue  # older than 30 days with no date
            except ValueError:
                pass

        pruned.append(fp)

    return pruned


def save_fingerprint(event: dict, ticktick_id: str | None = None) -> None:
    """Add a fingerprint for an event to the store."""
    fps = load_fingerprints()

    entry = {
        "title": event.get("title", ""),
        "date": event.get("date"),
        "time": event.get("time"),
        "location": event.get("location"),
        "link": event.get("link"),
        "ticktick_id": ticktick_id,
        "created_at": date.today().isoformat(),
    }

    fps.append(entry)
    FINGERPRINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    FINGERPRINT_FILE.write_text(json.dumps(fps, indent=2))


def find_fingerprint(event: dict) -> dict | None:
    """Find a matching fingerprint for the given event."""
    fps = load_fingerprints()
    for fp in fps:
        is_match, score = events_match(event, fp)
        if is_match:
            return fp
    return None


def dedup_events(events: list[dict]) -> list[dict]:
    """Within-run dedup using events_match. Returns unique events."""
    unique: list[dict] = []
    for event in events:
        matched = False
        for existing in unique:
            is_match, _ = events_match(event, existing)
            if is_match:
                matched = True
                break
        if not matched:
            unique.append(event)
    return unique
