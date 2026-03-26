"""LLM-based event validation using litellm."""

import json
import logging
from typing import Optional

from litellm import completion

from event_harvester.config import LLMConfig

logger = logging.getLogger("event_harvester.llm_filter")

_SYSTEM_PROMPT = """\
You are an event verification assistant. You receive candidates that have \
already passed a structural pre-filter (they contain dates, times, scheduling \
keywords, or event platform links). Your job is to evaluate each one:

1. Is this an ACTUAL EVENT that someone could attend or needs to act on? \
(meetups, parties, workshops, hackathons, deadlines, meetings, conventions, \
RSVPs, concerts, calls, webinars)
2. DISCARD: casual time mentions ("I'll fix it tonight"), weather ("102F next \
week"), jokes, news articles with dates, past/resolved events, status updates.
3. For each REAL event, return a clean JSON object:
   - "title": Short, clear event title (max 80 chars). Not the raw message.
   - "date": Resolved date (YYYY-MM-DD or YYYY-MM-DDTHH:MM), or null.
   - "all_day": true if no specific time, false if time is known.
   - "source": Original channel name (pass through).
   - "author": Who posted it (pass through).
   - "details": 1-2 sentence summary with location/link if available.
   - "score": Original score (pass through unchanged).
   - "pinned": Whether pinned (pass through unchanged).
   - "original": Original message text (pass through, truncated).

Today's date is provided in the user message for resolving relative dates.

Return JSON: {"events": [...]}
Only return the JSON, no other text."""


def validate_events(
    candidates: list[dict],
    cfg: Optional[LLMConfig] = None,
    max_candidates: int = 40,
) -> list[dict]:
    """Send candidate events to LLM for validation and cleanup.

    Returns cleaned list of real events with proper titles and dates.
    """
    if not candidates:
        return []

    if cfg is None or not cfg.is_configured:
        logger.warning("LLM not configured - skipping event validation.")
        return candidates

    model = cfg.litellm_model

    # Build the candidate list for the LLM
    items = []
    for c in candidates[:max_candidates]:
        item = {
            "content": c["content"][:300],
            "author": c["author"],
            "channel": c["channel"],
            "timestamp": c["timestamp"],
            "dates": c.get("dates", []),
            "times": c.get("times", []),
            "score": c.get("score", 0),
            "pinned": c.get("pinned", False),
            "scheduling": c.get("scheduling", False),
        }
        if c.get("best_date"):
            item["resolved_date"] = c["best_date"]
        items.append(item)

    from datetime import date

    today = date.today().isoformat()
    user_msg = (
        f"Today's date: {today}\n\n"
        f"These {len(items)} candidates passed structural pre-filtering "
        f"(they have dates, times, scheduling keywords, or event links). "
        f"Evaluate each: is it a real event someone could attend or act on?\n\n"
        f"```json\n{json.dumps(items, indent=2, ensure_ascii=False)}\n```"
    )

    logger.info(
        "Validating %d candidate events with %s ...",
        len(items), model,
    )

    try:
        resp = completion(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=4096,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content
        data = json.loads(raw)
        events = data.get("events", [])
        logger.info(
            "LLM kept %d / %d candidates as real events.",
            len(events), len(items),
        )
        return events

    except json.JSONDecodeError as e:
        logger.error("LLM returned invalid JSON: %s", e)
        return candidates
    except Exception as e:
        logger.error("LLM validation failed: %s", e)
        return candidates
