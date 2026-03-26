"""Local analysis - extract and weight links and date-referenced events."""

import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from dateutil import parser as dateutil_parser

URL_RE = re.compile(r"https?://[^\s<>\"'\]]+")
DATE_RE = re.compile(
    r"(?i)(?:"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}"
    r"|\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
    r"|\d{1,2}/\d{1,2}(?:/\d{2,4})?"
    r"|(?:today|tonight|tomorrow|"
    r"this\s+(?:week|sunday|monday|tuesday|wednesday|thursday|friday|saturday)|"
    r"next\s+(?:week|sunday|monday|tuesday|wednesday|thursday|friday|saturday))"
    r"|(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r")"
)
TIME_RE = re.compile(
    r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm|AM|PM)\b"
    r"|\b\d{1,2}(?::\d{2})\s*(?:ET|PT|PST|EST|UTC)\b"
)

_SCHEDULING_KEYWORDS = [
    "rsvp", "meeting", "event", "join", "attend", "host",
    "starts", "session", "see you", "cancelled", "canceled",
    "deadline", "due", "tonight", "tomorrow", "next week",
]

_LINK_EVENT_DOMAINS = [
    "luma.com", "eventbrite", "meetup.com", "rsvp", "calendar",
]
_LINK_TECH_DOMAINS = [
    "openai.com", "anthropic", "claude.ai", "arxiv", "github.com",
]
_LINK_ARTICLE_DOMAINS = [
    "medium.com", "substack", "blog", "article",
]
_LINK_SOCIAL_DOMAINS = [
    "instagram.com", "twitter.com", "x.com", "youtube.com",
]
_LINK_SKIP_DOMAINS = [
    "tenor.com", "giphy.com",
]


_RELATIVE_DAY_MAP = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _resolve_date(
    date_str: str,
    reference: date | None = None,
) -> date | None:
    """Try to resolve a date string to an absolute date.

    Handles: 'March 25', '3/25', 'next Friday', 'tomorrow', 'today', etc.
    Returns None if unparseable.
    """
    ref = reference or date.today()
    s = date_str.strip().lower()

    # Relative dates
    if s in ("today", "tonight"):
        return ref
    if s == "tomorrow":
        return ref + timedelta(days=1)

    # "this/next <weekday>"
    for prefix in ("this ", "next "):
        if s.startswith(prefix):
            day_name = s[len(prefix):].strip()
            if day_name == "week":
                return ref + timedelta(days=7)
            target_wd = _RELATIVE_DAY_MAP.get(day_name)
            if target_wd is not None:
                current_wd = ref.weekday()
                days_ahead = (target_wd - current_wd) % 7
                if prefix == "next " and days_ahead == 0:
                    days_ahead = 7
                if prefix == "next ":
                    days_ahead += 7 if days_ahead <= 0 else 0
                return ref + timedelta(days=days_ahead)
            return None

    # Bare weekday name
    if s in _RELATIVE_DAY_MAP:
        target_wd = _RELATIVE_DAY_MAP[s]
        current_wd = ref.weekday()
        days_ahead = (target_wd - current_wd) % 7
        if days_ahead == 0:
            days_ahead = 7  # assume next occurrence
        return ref + timedelta(days=days_ahead)

    # Absolute dates via dateutil
    try:
        dt = dateutil_parser.parse(date_str, default=datetime(ref.year, 1, 1))
        resolved = dt.date()
        # If resolved date is in the past, try next year
        if resolved < ref - timedelta(days=30):
            resolved = resolved.replace(year=ref.year + 1)
        return resolved
    except (ValueError, OverflowError):
        return None


def _event_proximity_score(
    resolved_date: date | None,
    now: date | None = None,
) -> int:
    """Score 0-10 based on how soon the event is. Future events score high."""
    if resolved_date is None:
        return 0
    ref = now or date.today()
    days_until = (resolved_date - ref).days
    if days_until < -1:
        return 0  # already passed
    if days_until <= 0:
        return 10  # today or yesterday
    if days_until <= 3:
        return 9
    if days_until <= 7:
        return 7
    if days_until <= 14:
        return 5
    if days_until <= 30:
        return 3
    return 1


def _recency_score(ts_str: str, now: Optional[datetime] = None) -> int:
    """0-10 score, higher = more recent."""
    if now is None:
        now = datetime.now(timezone.utc)
    ts = datetime.fromisoformat(ts_str)
    # Ensure both are tz-aware for subtraction
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    days_ago = (now - ts).days
    if days_ago <= 7:
        return 10
    if days_ago <= 14:
        return 8
    if days_ago <= 30:
        return 6
    if days_ago <= 90:
        return 4
    if days_ago <= 180:
        return 2
    return 1


def _link_type_score(url: str) -> int:
    """0-8 score by link category."""
    u = url.lower()
    if any(k in u for k in _LINK_SKIP_DOMAINS):
        return 0
    if any(k in u for k in _LINK_EVENT_DOMAINS):
        return 8
    if any(k in u for k in _LINK_TECH_DOMAINS):
        return 6
    if any(k in u for k in _LINK_ARTICLE_DOMAINS):
        return 5
    if any(k in u for k in _LINK_SOCIAL_DOMAINS):
        return 3
    if "discord.com/channels" in u:
        return 1
    return 4


def extract_links(
    messages: list[dict],
    now: Optional[datetime] = None,
) -> list[dict]:
    """Extract, dedupe, and score all links from messages."""
    raw: list[dict] = []
    for m in messages:
        for url in URL_RE.findall(m["content"]):
            url = url.rstrip(".,;:!?)")
            ts = _link_type_score(url)
            if ts == 0:
                continue
            rec = _recency_score(m["timestamp"], now)
            pin_boost = 2.0 if m.get("pinned") else 0.0
            raw.append({
                "url": url,
                "author": m["author"],
                "timestamp": m["timestamp"][:16],
                "channel": m["channel"],
                "context": m["content"][:120].replace("\n", " "),
                "recency": rec,
                "type_score": ts,
                "pinned": m.get("pinned", False),
                "score": round(rec * 0.6 + ts * 0.4 + pin_boost, 1),
            })

    # Dedupe by URL, keep highest scored
    best: dict[str, dict] = {}
    for link in raw:
        key = link["url"][:150]
        if key not in best or link["score"] > best[key]["score"]:
            best[key] = link

    return sorted(best.values(), key=lambda x: -x["score"])


def extract_events(
    messages: list[dict],
    now: Optional[datetime] = None,
) -> list[dict]:
    """Extract messages that reference dates/times/scheduling.

    Resolves date strings to absolute dates and scores events by
    proximity (how soon they happen), not just message recency.
    """
    ref_dt = now or datetime.now(timezone.utc)
    ref_date = ref_dt.date() if isinstance(ref_dt, datetime) else ref_dt
    results: list[dict] = []

    for m in messages:
        content = m["content"]
        dates = DATE_RE.findall(content)
        times = TIME_RE.findall(content)
        if not dates and not times:
            continue
        if len(content) < 20 and not times:
            continue

        rec = _recency_score(m["timestamp"], ref_dt)
        has_sched = any(k in content.lower() for k in _SCHEDULING_KEYWORDS)
        is_pinned = m.get("pinned", False)

        # Resolve dates: relative dates ("tonight", "next Friday") use the
        # message's own timestamp as reference so old pinned messages don't
        # resolve to today. Absolute dates ("March 25") use today.
        msg_date = ref_date
        try:
            msg_dt = datetime.fromisoformat(m["timestamp"])
            msg_date = msg_dt.date()
        except (ValueError, KeyError):
            pass

        resolved_dates = []
        best_resolved = None
        for d in dates:
            ds = d.strip().lower()
            is_relative = ds in (
                "today", "tonight", "tomorrow",
            ) or ds.startswith(("this ", "next ")) or ds in _RELATIVE_DAY_MAP
            resolve_ref = msg_date if is_relative else ref_date
            rd = _resolve_date(d, resolve_ref)
            if rd is not None:
                resolved_dates.append(rd.isoformat())
                # Only consider future or very recent dates
                if rd >= ref_date - timedelta(days=1):
                    if best_resolved is None or rd < best_resolved:
                        best_resolved = rd

        proximity = _event_proximity_score(best_resolved, ref_date)

        # Score: proximity (how soon) weighted most, plus scheduling + pinned
        # Old formula: rec + sched + pinned (max ~16)
        # New formula: max(rec, proximity) + sched + pinned (max ~16)
        # This way a 2-month-old pinned msg about an event tomorrow scores high
        base = max(rec, proximity)
        score = base + (3 if has_sched else 0) + (3 if is_pinned else 0)

        results.append({
            "content": content[:200].replace("\n", " "),
            "author": m["author"],
            "timestamp": m["timestamp"][:16],
            "channel": m["channel"],
            "dates": dates,
            "times": times,
            "resolved_dates": resolved_dates,
            "best_date": best_resolved.isoformat() if best_resolved else None,
            "recency": rec,
            "proximity": proximity,
            "scheduling": has_sched,
            "pinned": is_pinned,
            "score": score,
        })

    return sorted(results, key=lambda x: -x["score"])


_EVENT_LINK_DOMAINS = [
    "luma.com", "lu.ma", "eventbrite", "meetup.com",
    "zoom.us", "meet.google.com", "webex.com", "hopin.com",
    "fetlife.com/events", "plra.io", "partiful.com",
]


def prefilter_events(events: list[dict]) -> list[dict]:
    """Layer 1: Structural pre-filter for event candidates.

    Keeps events that have strong structural signals:
    - Has a resolved future date (best_date is set), OR
    - Has a scheduling keyword + date/time mention, OR
    - Contains an event platform link (eventbrite, luma, meetup, zoom, etc.)

    Drops events that only have weak signals (bare weekday mention in
    casual chat, old pinned messages with no future date).
    """
    passed: list[dict] = []
    for ev in events:
        has_future_date = ev.get("best_date") is not None
        has_sched = ev.get("scheduling", False)
        has_time = bool(ev.get("times"))
        has_date = bool(ev.get("dates"))
        content_lower = ev.get("content", "").lower()
        has_event_link = any(d in content_lower for d in _EVENT_LINK_DOMAINS)

        # Pass if: future date, or scheduling+date/time, or event link
        if has_future_date:
            passed.append(ev)
        elif has_sched and (has_date or has_time):
            passed.append(ev)
        elif has_event_link:
            passed.append(ev)
        # else: drop — weak signal (bare "Monday" in casual chat, etc.)

    return passed
