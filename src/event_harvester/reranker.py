"""Rerank messages by combining semantic relevance + date/event signals.

Uses a cross-encoder for semantic scoring and date resolution from weights.py
for temporal scoring. The combined score prioritizes messages that are both
semantically event-like AND have upcoming dates.
"""

import logging

logger = logging.getLogger("event_harvester.reranker")

_EVENT_QUERY = (
    "upcoming event, meetup, hackathon, workshop, conference, party, "
    "happy hour, webinar, interview, scheduled meeting, bar night, "
    "furmeet, convention with a specific date, time, and location"
)

_model = None


def _get_model():
    """Lazy-load the cross-encoder (first call downloads ~23MB)."""
    global _model
    if _model is None:
        import os
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

        from sentence_transformers import CrossEncoder
        logger.info("Loading reranker model...")
        try:
            _model = CrossEncoder(
                "cross-encoder/ms-marco-MiniLM-L-6-v2",
                max_length=512,
            )
        except Exception:
            # First run — need to download, go online
            os.environ.pop("HF_HUB_OFFLINE", None)
            os.environ.pop("TRANSFORMERS_OFFLINE", None)
            _model = CrossEncoder(
                "cross-encoder/ms-marco-MiniLM-L-6-v2",
                max_length=512,
            )
        logger.info("Reranker loaded.")
    return _model


def _resolve_message_dates(content, timestamp, today):
    """Resolve all dates in a message, return (best_proximity, has_future, has_event_link).

    Parses date mentions from the message content, resolves relative dates
    using the message timestamp as reference, and checks for event platform links.
    """
    from datetime import datetime

    from event_harvester.weights import (
        GATE_DATE_RE,
        _EVENT_LINK_DOMAINS,
        _event_proximity_score,
        _resolve_date,
    )

    content_lower = content.lower()
    has_event_link = any(d in content_lower for d in _EVENT_LINK_DOMAINS)

    dates = GATE_DATE_RE.findall(content)
    if not dates and not has_event_link:
        return 0, False, has_event_link

    # Determine message date for relative date resolution
    msg_date = today
    try:
        msg_date = datetime.fromisoformat(timestamp).date()
    except (ValueError, KeyError):
        pass

    has_future = False
    best_proximity = 0
    for d in dates:
        ds = d.strip().lower()
        is_relative = ds in ("today", "tonight", "tomorrow") or \
            ds.startswith(("this ", "next ")) or \
            ds in {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}
        resolved = _resolve_date(d, msg_date if is_relative else today)
        if resolved and resolved >= today:
            has_future = True
            best_proximity = max(best_proximity, _event_proximity_score(resolved, today))

    return best_proximity, has_future, has_event_link


def rerank_messages(
    messages: list[dict],
    query: str = _EVENT_QUERY,
    top_k: int = 150,
) -> list[dict]:
    """Rerank messages by combined semantic + temporal score.

    Only scores messages that have a date/time mention. Messages without
    any date signal are dropped before the cross-encoder runs.

    Combined score = semantic (0-10) + date proximity (0-10) + bonuses.
    """
    if not messages:
        return []

    from datetime import date
    import re

    from event_harvester.weights import TIME_RE

    today = date.today()
    url_re = re.compile(r"https?://\S+")

    # Step 1: Resolve dates per message, filter to those with future dates or event links
    candidates = []
    date_info: list[tuple[float, bool]] = []  # (best_proximity, has_event_link)
    n_past = 0
    n_no_signal = 0

    for m in messages:
        content = m.get("content", "")
        timestamp = m.get("timestamp", "")

        best_proximity, has_future, has_event_link = _resolve_message_dates(
            content, timestamp, today,
        )

        if has_future or has_event_link:
            candidates.append(m)
            date_info.append((best_proximity, has_event_link))
        elif best_proximity == 0 and not has_event_link:
            n_no_signal += 1
        else:
            n_past += 1

    logger.info(
        "Reranker filter: %d -> %d (dropped %d past, %d no date/link).",
        len(messages), len(candidates), n_past, n_no_signal,
    )

    if not candidates:
        return []

    # Step 2: Score with cross-encoder
    model = _get_model()
    pairs = [[query, m.get("content", "")[:512]] for m in candidates]
    logger.info("Reranker: scoring %d candidates...", len(candidates))
    semantic_scores = model.predict(pairs, batch_size=64, show_progress_bar=True)

    # Step 3: Combine scores
    combined = []
    for m, sem_score, (best_proximity, has_event_link) in zip(
        candidates, semantic_scores, date_info,
    ):
        content = m.get("content", "")

        score = (
            float(sem_score) * 10
            + best_proximity
            + (2 if TIME_RE.search(content) else 0)
            + (2 if url_re.search(content) else 0)
            + (3 if has_event_link else 0)
            + (2 if m.get("pinned") else 0)
        )

        combined.append((m, score))

    combined.sort(key=lambda x: x[1], reverse=True)
    result = [m for m, _ in combined[:top_k]]

    if combined:
        logger.info(
            "Reranker: scored %d -> top %d (top: %.1f, cutoff: %.1f)",
            len(combined), min(top_k, len(combined)),
            combined[0][1], combined[min(top_k - 1, len(combined) - 1)][1],
        )

    return result
