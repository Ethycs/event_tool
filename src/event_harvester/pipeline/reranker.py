"""Rerank messages by combining semantic relevance + date/event signals.

Uses a cross-encoder for semantic scoring and date resolution from weights.py
for temporal scoring. The combined score prioritizes messages that are both
semantically event-like AND have upcoming dates.
"""

import logging
import os
from pathlib import Path

from event_harvester.observability.tracing import set_span_attrs, traced

logger = logging.getLogger("event_harvester.pipeline.reranker")
_TRACER = "event_harvester.pipeline.reranker"

_EVENT_QUERY = (
    "upcoming event, meetup, hackathon, workshop, conference, party, "
    "happy hour, webinar, interview, scheduled meeting, bar night, "
    "furmeet, convention with a specific date, time, and location"
)

# HuggingFace id, used only as a last resort.  Preferred source is a local
# copy from the model store (lakeFS/MinIO via DVC), which is versioned,
# survives HF being unreachable, and does not fetch weights at runtime.
_MODEL_ID = "cross-encoder/ms-marco-MiniLM-L-6-v2"

# Where `just model-deploy` puts models, per pipelines/models/manifest.toml.
# System-level paths on purpose: this service runs as LocalSystem, so a copy
# under a user profile would be scoped to an account that isn't the consumer.
_LOCAL_MODEL_DIRS = (
    r"C:\models\ms-marco-MiniLM-L-6-v2",
    "/data/models/ms-marco-MiniLM-L-6-v2",
)

_model = None


def _resolve_model_source():
    """Return (source, is_local) for the cross-encoder.

    Prefers a local checkout from the model store so startup does no network
    I/O.  ``EVENT_HARVESTER_RERANKER_PATH`` overrides, for a host that keeps
    models somewhere else.

    A directory only counts if it actually contains ``config.json``.  An empty
    or half-pulled directory would otherwise be picked and then fail deep
    inside transformers with a much less obvious error.
    """
    override = os.environ.get("EVENT_HARVESTER_RERANKER_PATH")
    candidates = ([override] if override else []) + list(_LOCAL_MODEL_DIRS)

    for candidate in candidates:
        if candidate and (Path(candidate) / "config.json").is_file():
            return candidate, True

    if override:
        # Explicitly configured and not usable -- say so rather than silently
        # falling back to the network, which is what the operator asked us
        # not to do by setting it.
        logger.warning(
            "EVENT_HARVESTER_RERANKER_PATH=%s has no config.json; "
            "falling back to HuggingFace (%s)", override, _MODEL_ID,
        )
    return _MODEL_ID, False


@traced("rerank.model.get", tracer_name=_TRACER)
def _get_model():
    """Lazy-load the cross-encoder.

    Local copy first; HuggingFace only if no local copy exists.
    """
    global _model
    # Cold start is seconds and can silently become a HuggingFace download;
    # the span distinguishes "cached" from "loaded from disk" from "went to
    # the network", which the log lines only tell you if you are watching.
    if _model is not None:
        set_span_attrs(**{"rerank.model.cached": True})
        return _model

    set_span_attrs(**{"rerank.model.cached": False})
    if _model is None:
        source, is_local = _resolve_model_source()
        set_span_attrs(**{
            "rerank.model.source": str(source),
            "rerank.model.is_local": is_local,
        })

        from sentence_transformers import CrossEncoder

        if is_local:
            # Nothing to fetch -- pin offline so a stray lookup cannot turn a
            # local load into a silent network call.
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
            logger.info("Loading reranker from local model store: %s", source)
            _model = CrossEncoder(source, max_length=512)
            logger.info("Reranker loaded (local).")
            set_span_attrs(**{"rerank.model.loaded_from": "local"})
            return _model

        # No local copy.  Try the HF cache offline first, then the network.
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        logger.info("No local model store copy; trying HF cache for %s", source)
        try:
            _model = CrossEncoder(source, max_length=512)
            logger.info("Reranker loaded (HF cache).")
            set_span_attrs(**{"rerank.model.loaded_from": "hf_cache"})
        except Exception:
            # First run -- need to download, go online.
            os.environ.pop("HF_HUB_OFFLINE", None)
            os.environ.pop("TRANSFORMERS_OFFLINE", None)
            logger.warning(
                "Downloading reranker from HuggingFace. Deploy it to the "
                "model store instead (just model-deploy <host>) so startup "
                "does no network I/O: %s", source,
            )
            _model = CrossEncoder(source, max_length=512)
            logger.info("Reranker loaded (downloaded).")
            set_span_attrs(**{"rerank.model.loaded_from": "huggingface_download"})
    return _model


def _resolve_message_dates(content, timestamp, today):
    """Resolve all dates in a message, return (best_proximity, has_future, has_event_link).

    Parses date mentions from the message content, resolves relative dates
    using the message timestamp as reference, and checks for event platform links.
    """
    from datetime import datetime

    from event_harvester.pipeline.weights import (
        _EVENT_LINK_DOMAINS,
        GATE_DATE_RE,
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


@traced("rerank.cross_encoder.predict", tracer_name=_TRACER)
def _score_pairs(model, pairs):
    """Run the cross-encoder. Split out so the GPU work gets its own span.

    This is the single most expensive step in the pipeline that isn't an API
    call, and it was previously indistinguishable from the filtering around
    it -- if `rerank.messages` is slow, this span says whether that is model
    inference or the date-resolution loop.
    """
    set_span_attrs(**{"rerank.pair_count": len(pairs)})
    return model.predict(pairs, batch_size=64, show_progress_bar=True)


@traced("rerank.messages", tracer_name=_TRACER)
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
    set_span_attrs(**{"rerank.message.in": len(messages), "rerank.top_k": top_k})
    if not messages:
        return []

    import re
    from datetime import date

    from event_harvester.pipeline.weights import TIME_RE

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
    # This pre-filter drops more messages than the cross-encoder does, and it
    # is invisible above the reranker -- a run where everything lands in
    # `no_signal` looks identical to a quiet week from the outside.
    set_span_attrs(**{
        "rerank.candidates": len(candidates),
        "rerank.dropped_past": n_past,
        "rerank.dropped_no_signal": n_no_signal,
    })

    if not candidates:
        return []

    # Step 2: Score with cross-encoder
    model = _get_model()
    pairs = [[query, m.get("content", "")[:512]] for m in candidates]
    logger.info("Reranker: scoring %d candidates...", len(candidates))
    semantic_scores = _score_pairs(model, pairs)

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
        cutoff = combined[min(top_k - 1, len(combined) - 1)][1]
        logger.info(
            "Reranker: scored %d -> top %d (top: %.1f, cutoff: %.1f)",
            len(combined), min(top_k, len(combined)),
            combined[0][1], cutoff,
        )
        set_span_attrs(**{
            "rerank.score.top": round(float(combined[0][1]), 2),
            "rerank.score.cutoff": round(float(cutoff), 2),
            "rerank.truncated": len(combined) > top_k,
        })

    set_span_attrs(**{"rerank.message.out": len(result)})
    return result
