"""LLM-based task extraction from messages."""

import logging
from dataclasses import dataclass, field

from event_harvester.config import LLMConfig
from event_harvester.llm import chat_completion
from event_harvester.observability.tracing import get_tracer

logger = logging.getLogger("event_harvester.pipeline.analysis")
tracer = get_tracer("event_harvester.pipeline.analysis")

# The five reject buckets every caller expects, in pipeline order.
REJECT_BUCKETS = ("classifier", "caps", "reranker", "llm_past", "llm_no_events",
                  "llm_errors")


def empty_rejects() -> dict[str, list[dict]]:
    """A full reject dict with every bucket present and empty."""
    return {bucket: [] for bucket in REJECT_BUCKETS}


@dataclass
class ExtractionResult:
    """Everything :func:`extract_events_llm` produces.

    Replaces three side-channel function attributes that used to carry the
    reject buckets out of this module — ``extract_events_llm._last_rejects``,
    ``_extract_events_cloud._last_llm_dropped/._last_llm_no_events``, and
    ``_parse_event_ini._last_dropped``. Those were broken in two distinct ways:

    1. ``_extract_events_cloud``'s attributes were read by the orchestrator
       *unconditionally*, including on runs where ``backend="local"`` meant
       that function never executed — so a local run reported whatever the
       previous cloud run had left behind, or nothing at all.
    2. ``_parse_event_ini._last_dropped`` was written and then read as two
       separate statements while up to 5 ``ThreadPoolExecutor`` workers called
       it concurrently, so one batch could read another batch's dropped list.

    Returning the data makes both impossible, and is what allows the extract
    stage to be moved off the event loop with ``asyncio.to_thread`` — a
    function attribute shared across threads would have made that worse.
    """

    summary: str
    events: list[dict]
    rejects: dict[str, list[dict]] = field(default_factory=empty_rejects)


from event_harvester.utils import parse_llm_ini

# Backward-compat alias — existing internal callers use this name.
_parse_llm_ini = parse_llm_ini


PRIORITY_LABEL = {0: "none", 1: "low", 3: "medium", 5: "high"}


_TRACKING_PARAMS = ("aff", "recId", "recSource", "searchQuery", "utm_source",
                    "utm_medium", "utm_campaign", "DoneURL")


def chat_label(platform: str, channel: str) -> str:
    """A short, stable name for the group header a batch is organised under.

    **A web source's `channel` is the event's full URL**, tracking parameters
    and all — up to 120 characters, 39-64 tokens each. Measured 2026-08-13,
    those headers were **26% of the worst-case prompt** (486 of 1,881 tokens),
    with every URL repeated a second time inside the message body as
    `link: https://…`. They also fragmented the batch into ten one-message
    groups, so ten headers were paid for ten messages.

    Compacting to host plus first path segment keeps what the header is *for*
    — telling the model which listing this came from — and lets several events
    from the same site share one group. The `source` the model emits is
    `@author in #channel`, so the author handle survives untouched, which
    matters: `outputs/regions.py` reads venue names out of it to place events
    that carry no location.
    """
    name = str(channel or "")
    if name.startswith(("http://", "https://")):
        from urllib.parse import urlsplit

        parts = urlsplit(name)
        host = parts.netloc.removeprefix("www.")
        segment = next((s for s in parts.path.split("/") if s), "")
        # A slug is the event's own title repeated; the first segment is the
        # section ('e', 'events', 'calendar') or the group name, which is the
        # part that identifies the source.
        name = f"{host}/{segment}" if segment else host
    return f"{str(platform).capitalize()} / {name}"


def build_prompt(messages: list[dict], days_back: int) -> str:
    """Build the event extraction prompt from collected messages.

    Includes today's date and pre-resolved dates/times per message
    so the LLM doesn't have to re-parse relative dates.
    """
    from datetime import date, datetime

    from event_harvester.pipeline.weights import DATE_RE, TIME_RE, _resolve_date

    today = date.today()

    grouped: dict[str, list[dict]] = {}
    for msg in messages:
        key = chat_label(msg["platform"], msg["channel"])
        grouped.setdefault(key, []).append(msg)

    lines = [
        f"Today's date: {today.isoformat()}\n",
        f"Review {len(messages)} messages from the last {days_back} day(s) "
        f"across {len(grouped)} chat(s).\n",
        "Find real-world events someone could attend. Each message below "
        "has pre-extracted date/time hints in [brackets] to help you.\n",
        "--- Messages ---\n",
    ]
    for chat, msgs in sorted(grouped.items()):
        # The count only earns its tokens when it is not one.
        suffix = f"  ({len(msgs)} messages)" if len(msgs) > 1 else ""
        lines.append(f"### {chat}{suffix}")
        for m in msgs[-60:]:
            ts = m["timestamp"][:16].replace("T", " ")
            content = m["content"][:400].replace("\n", " ")
            author = m["author"]

            # Pre-extract date/time hints
            hints = []
            dates = DATE_RE.findall(content)
            times = TIME_RE.findall(content)

            if dates:
                # Resolve relative dates using message timestamp
                msg_date = today
                try:
                    msg_date = datetime.fromisoformat(m["timestamp"]).date()
                except (ValueError, KeyError):
                    pass

                resolved = []
                for d in dates:
                    r = _resolve_date(d, msg_date)
                    if r and r >= today:
                        resolved.append(r.isoformat())
                if resolved:
                    hints.append(f"dates={','.join(resolved)}")

            if times:
                hints.append(f"times={','.join(times)}")

            hint_str = f" ({'; '.join(hints)})" if hints else ""
            lines.append(f"  [{ts}] {author}:{hint_str} {content}")
        lines.append("")
    # Escape every literal '%' so a downstream printf-style formatter
    # (litellm / httpx logging path) doesn't choke on values like
    # '% OFF', URL-encoded fragments ('%3A'), etc. Doubled '%%' collapses
    # back to '%' on printf and the LLM tolerates the doubling otherwise.
    return "\n".join(lines).replace("%", "%%")


_EVENT_EXTRACTION_PROMPT = """\
You are an event extractor. Read the messages and find real-world events \
someone could attend or join. Extract the event details.

Ignore: casual chat, newsletters, marketing, opinions, jokes, news, \
someone else's personal plans, past events.

Output events in INI format with one section per event:

[Event.1]
title = Hackathon Weekend
date = 2026-04-02
time = 09:00
location = San Francisco
link = https://lu.ma/hack
source = @author in #channel
details = $45k in prizes, Google DeepMind sponsoring

[Event.2]
title = Happy Hour
date = 2026-04-02
time = 18:00
location = Google Cloud, SF
link = TBD
source = @alice in #INBOX
details = AI infra talk with demo

If date, time, location, or link is unknown, write TBD.
Include any registration links, RSVP links, or event page URLs.
Only output the INI sections, nothing else."""


def score_message(m: dict) -> int:
    """Score a message by event signal strength.

    Higher = more likely to contain an event mention. Used by prioritize()
    and prioritize_with_caps() to sort before capping.
    """
    from event_harvester.pipeline.weights import URL_RE, has_date_or_event_signal

    content = m.get("content", "")
    s = 0
    if has_date_or_event_signal(content):
        s += 3
    if URL_RE.search(content):
        s += 2
    if m.get("pinned"):
        s += 2
    if any(kw in content.lower() for kw in _get_actionable_signals()):
        s += 1
    # Your own keywords outrank every built-in signal. This is what makes
    # keywords do something on sources that cannot be queried: matching is
    # local and never drops a message, so the only way a match matters is by
    # surviving `prioritize_with_caps`, and the caps are where ~98% is lost.
    if m.get("_keywords"):
        s += 4
    return s


def prioritize(messages: list[dict], max_messages: int = 150) -> list[dict]:
    """Sort messages by event signal strength, cap to fit time budget.

    150 messages = 15 batches of 10 ≈ 2 minutes on local LLM.
    """
    scored = sorted(messages, key=score_message, reverse=True)
    if len(scored) > max_messages:
        logger.info("Priority cap: %d -> %d messages (top by score).",
                     len(scored), max_messages)
    return scored[:max_messages]


def prioritize_with_caps(messages: list[dict], caps) -> list[dict]:
    """Score messages, then apply per-source caps before merging.

    Each source's messages are scored independently, sorted, and capped
    to its quota. The resulting per-source winners are merged and
    optionally trimmed to caps.total.

    This prevents noisy sources (e.g., 500 Telegram messages) from
    crowding out high-signal events from quieter sources (e.g., 6 web
    events).

    If `caps.no_cap` is set, all per-source and total trimming is
    bypassed — messages are still scored and sorted (so the LLM gets
    the highest-signal items first) but every message survives.
    """
    if getattr(caps, "no_cap", False):
        logger.info(
            "no_cap=True: skipping per-source and global caps (%d messages).",
            len(messages),
        )
        return sorted(messages, key=score_message, reverse=True)

    from collections import defaultdict

    # Bucket by platform
    buckets: dict[str, list[dict]] = defaultdict(list)
    for m in messages:
        platform = (m.get("platform") or "unknown").lower()
        buckets[platform].append(m)

    # Score-then-cap per bucket. A negative cap (e.g. -1) means
    # "unlimited for this source" — keep all messages in the bucket.
    capped_buckets: dict[str, list[dict]] = {}
    for platform, msgs in buckets.items():
        scored = sorted(msgs, key=score_message, reverse=True)
        cap = caps.get(platform)
        if cap < 0:
            kept = scored
        else:
            kept = scored[:cap]
        capped_buckets[platform] = kept
        if cap >= 0 and len(scored) > cap:
            logger.info(
                "  %s: %d -> %d (per-source cap)", platform, len(scored), cap,
            )
        elif cap < 0:
            logger.info(
                "  %s: %d (no cap)", platform, len(scored),
            )

    # Merge and re-sort by score, then apply global ceiling.
    # A negative caps.total means "no global ceiling".
    merged: list[dict] = []
    for msgs in capped_buckets.values():
        merged.extend(msgs)
    merged.sort(key=score_message, reverse=True)

    if caps.total >= 0 and len(merged) > caps.total:
        logger.info(
            "Global cap: %d -> %d messages (after per-source caps).",
            len(merged), caps.total,
        )
        merged = merged[: caps.total]
    elif caps.total < 0:
        # Caps default to unlimited since 2026-08-11, which moves the whole
        # cost of a run onto the LLM stage. Say the number out loud: an
        # expensive harvest should be visible in the log that caused it, not
        # discovered on a bill. `_LOUD_MESSAGE_COUNT` is a "you probably meant
        # to cap this" line, not a limit.
        level = logger.warning if len(merged) >= _LOUD_MESSAGE_COUNT else logger.info
        level(
            "No global cap: %d message(s) going to extraction%s",
            len(merged),
            " — set a cap in /admin or CAP_TOTAL if that is more than intended."
            if len(merged) >= _LOUD_MESSAGE_COUNT else ".",
        )

    return merged


# Roughly 100 LLM batches at the default batch size of 10.
_LOUD_MESSAGE_COUNT = 1000


def prioritize_grouped(messages: list[dict], caps) -> dict[str, list[dict]]:
    """Like prioritize_with_caps but returns per-source buckets.

    Each bucket is independently scored and capped. No global merge or
    total ceiling — useful when downstream wants to process each source
    separately (e.g., one LLM call per source, or grouped display).
    """
    from collections import defaultdict

    buckets: dict[str, list[dict]] = defaultdict(list)
    for m in messages:
        platform = (m.get("platform") or "unknown").lower()
        buckets[platform].append(m)

    grouped: dict[str, list[dict]] = {}
    for platform, msgs in buckets.items():
        scored = sorted(msgs, key=score_message, reverse=True)
        cap = caps.get(platform)
        if cap < 0:
            grouped[platform] = scored
        else:
            grouped[platform] = scored[:cap]
        if cap >= 0 and len(scored) > cap:
            logger.info(
                "  %s: %d -> %d (per-source cap)", platform, len(scored), cap,
            )

    return grouped


_ACTIONABLE_SIGNALS = [
    "action required", "action needed", "action item",
    "please", "could you", "can you", "let me know",
    "respond", "reply", "reminder", "follow up",
    "?",  # questions
]

# Merge in shared scheduling keywords to avoid maintaining two lists.
# Import is deferred to avoid circular import at module load.
def _get_actionable_signals() -> list[str]:
    from event_harvester.pipeline.weights import SCHEDULING_KEYWORDS
    return _ACTIONABLE_SIGNALS + SCHEDULING_KEYWORDS


def extract_events_llm(
    messages: list[dict],
    days_back: int,
    cfg: LLMConfig,
    caps=None,
) -> ExtractionResult:
    """Send messages to the LLM and return an :class:`ExtractionResult`.

    If `caps` (CapConfig) is provided, applies per-source quotas before
    LLM extraction. Messages are scored first, then capped per source,
    so noisy sources can't crowd out high-signal ones.

    Pipeline order: classifier -> per-source caps -> reranker -> LLM.
    Caps run before the reranker so each source gets its quota of
    candidates *before* global ranking trims to top_k. Reranking first
    would let a noisy source monopolize the top-k and starve other
    sources of any survivors regardless of their per-source cap.
    """
    if not cfg.is_configured:
        logger.warning("LLM not configured - skipping analysis.")
        return ExtractionResult("", [])

    from event_harvester.pipeline.classifier import filter_actionable as classifier_filter
    from event_harvester.pipeline.classifier import has_trained_models

    total = len(messages)
    if caps is None or getattr(caps, "no_cap", False) or caps.total < 0:
        # Three ways a caller says "don't cap this run", all equivalent here:
        #   caps=None      -- the orchestrator's unlimited_caps path, which is
        #                     what CLI `--only` takes. This used to fall through
        #                     to a literal 150, so `--only` silently trimmed to
        #                     the default total after advertising no caps.
        #   caps.no_cap    -- the UI checkbox / MCP no_cap param / profile flag.
        #   caps.total < 0 -- the -1 "unlimited" sentinel (e.g. CAP_TOTAL=-1).
        # Use the message count itself as the rerank top_k so nothing is dropped.
        cap_total = max(total, 1)
    else:
        cap_total = caps.total

    rejects: dict[str, list[dict]] = empty_rejects()

    if has_trained_models():
        after_clf = classifier_filter(messages)
        n_after_clf = len(after_clf)
        clf_ids = {id(m) for m in after_clf}
        rejects["classifier"] = [m for m in messages if id(m) not in clf_ids]

        if caps:
            after_caps = prioritize_with_caps(after_clf, caps)
        else:
            after_caps = sorted(after_clf, key=score_message, reverse=True)[:cap_total]

        caps_ids = {id(m) for m in after_caps}
        rejects["caps"] = [m for m in after_clf if id(m) not in caps_ids]

        try:
            from event_harvester.pipeline.reranker import rerank_messages
            filtered = rerank_messages(after_caps, top_k=cap_total)
        except ImportError:
            logger.warning("sentence-transformers not installed, falling back to regex priority.")
            filtered = sorted(after_caps, key=score_message, reverse=True)

        filtered_ids = {id(m) for m in filtered}
        rejects["reranker"] = [m for m in after_caps if id(m) not in filtered_ids]

        logger.info(
            "Event extraction: %d -> %d (classifier) -> %d (caps) -> %d (rerank/score) -> LLM (%s)",
            total, n_after_clf, len(after_caps), len(filtered), cfg.display_name,
        )
    else:
        # No classifier — score, then per-source cap (or global cap)
        if caps:
            filtered = prioritize_with_caps(messages, caps)
        else:
            filtered = prioritize(messages, max_messages=cap_total)

        filtered_ids = {id(m) for m in filtered}
        rejects["caps"] = [m for m in messages if id(m) not in filtered_ids]

        logger.info(
            "Event extraction: %d -> %d messages (priority) -> LLM (%s)",
            total, len(filtered), cfg.display_name,
        )

    # Only the backend that actually runs contributes LLM-stage rejects. The
    # orchestrator used to read them off `_extract_events_cloud` regardless of
    # which backend ran, so local runs reported stale or empty buckets.
    if cfg.backend == "local":
        summary, events, llm_rejects = _extract_events_local(filtered, days_back, cfg)
    else:
        logger.info(
            "Sending %d messages to %s (backend=%s) ...",
            len(filtered), cfg.display_name, cfg.backend,
        )
        summary, events, llm_rejects = _extract_events_cloud(filtered, days_back, cfg)

    rejects.update(llm_rejects)
    return ExtractionResult(summary, events, rejects)


def _extract_events_cloud(
    messages: list[dict], days_back: int, cfg: LLMConfig,
    batch_size: int = 10,
) -> tuple[str, list[dict], dict[str, list[dict]]]:
    """Extract events using parallel batched requests (cloud APIs handle concurrency).

    Returns ``(summary, events, llm_rejects)``. ``llm_rejects`` carries the
    ``llm_past``/``llm_no_events`` buckets back to the caller instead of being
    stashed on this function object — see :class:`ExtractionResult`.
    """
    import concurrent.futures

    batches = [messages[i:i + batch_size] for i in range(0, len(messages), batch_size)]
    logger.info("Cloud extraction: %d messages in %d parallel batches", len(messages), len(batches))

    all_tasks: list[dict] = []
    raw_outputs: list[str] = []
    llm_dropped: list[dict] = []
    llm_no_events: list[dict] = []  # messages in batches that yielded 0 events
    llm_errors: list[dict] = []     # messages in batches that never got an answer

    def _process_batch_with_raw(batch, i):
        with tracer.start_as_current_span(f"extract.batch.{i}") as span:
            span.set_attribute("batch.size", len(batch))
            try:
                raw = chat_completion(
                    messages=[
                        {"role": "system", "content": _EVENT_EXTRACTION_PROMPT},
                        {"role": "user", "content": build_prompt(batch, days_back)},
                    ],
                    cfg=cfg,
                    max_tokens=4096,
                )
                # `dropped` comes back from the call itself. It used to be read
                # from `_parse_event_ini._last_dropped` on the next line, while
                # up to 5 pool workers called the same function -- so a batch
                # could read a sibling batch's dropped list.
                _, tasks, dropped = _parse_event_ini(raw)
                # Resolve the back-reference here: this is the only scope that
                # holds both the events and the messages they came from.
                attach_source_ids(tasks, batch)
                span.set_attribute("events.extracted", len(tasks))
                span.set_attribute("events.dropped_past", len(dropped))
                span.set_attribute(
                    "events.source_linked", sum(1 for t in tasks if t.get("_source_ids")),
                )
                return tasks, raw, dropped, batch if not tasks else [], []
            except Exception as e:
                logger.exception("Batch failed (%s)", type(e).__name__)
                span.set_attribute("error.message", str(e))
                span.record_exception(e)
                # The batch goes to `llm_errors`, NOT `llm_no_events`. They are
                # different facts — "the model read these and found nothing"
                # versus "the model never saw these" — and conflating them cost
                # 251 events on 2026-08-12: the API returned 429 for every
                # batch, all 20,716 messages were filed as "no events", the run
                # reported ok with 0 events, and that empty result overwrote a
                # full cache. Axiom 3 says every drop is attributable; a drop
                # attributed to the wrong stage is worse than an unattributed
                # one, because it reads as a finding.
                return [], "", [], [], batch

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(_process_batch_with_raw, b, i) for i, b in enumerate(batches)]
        for f in concurrent.futures.as_completed(futures):
            tasks, raw, dropped, no_event_msgs, error_msgs = f.result()
            all_tasks.extend(tasks)
            llm_dropped.extend(dropped)
            llm_no_events.extend(no_event_msgs)
            llm_errors.extend(error_msgs)
            if raw:
                raw_outputs.append(raw)

    if llm_errors:
        # Loud, and phrased as the operational fact it is. The 429 storm was
        # visible only as a stack trace per batch in a log nobody reads during
        # a run; the summary said "0 events" and looked like a quiet night.
        logger.error(
            "Extraction incomplete: %d of %d message(s) across %d batch(es) never "
            "reached the model. This is an outage, not an empty result.",
            len(llm_errors), len(messages), len(batches),
        )

    if not all_tasks and raw_outputs:
        logger.warning(
            "No events parsed. First batch raw output:\n%s",
            raw_outputs[0][:1000],
        )

    # Deduplicate (fingerprint match pass)
    from event_harvester.pipeline.event_match import dedup_events
    with tracer.start_as_current_span("match.fingerprints") as span:
        unique = dedup_events(all_tasks)
        # match.count = how many duplicates were collapsed
        span.set_attribute("match.count", max(0, len(all_tasks) - len(unique)))
        span.set_attribute("events.in", len(all_tasks))
        span.set_attribute("events.out", len(unique))

    return "", unique, {"llm_past": llm_dropped, "llm_no_events": llm_no_events,
                        "llm_errors": llm_errors}


def attach_source_ids(events: list[dict], batch: list[dict]) -> None:
    """Link each event back to the message in ``batch`` that produced it.

    **`EH-STD-EVENT-v0` §3.1 is explicit that `source` is free text and "NOT a
    structured back-reference".** That was fine while every sink only
    displayed it, and stopped being fine when Gmail labelling needed to know
    which mail to label. Resolution is confined to the batch the event came
    out of (≤10 messages), which is what makes it tractable at all.

    **Confident matches only, and the bar is deliberately high**: the consumer
    writes to a real mailbox, and a mislabelled message is worse than an
    unlabelled one. Two signals qualify --

    - the event's ``link`` appears verbatim in the message content, or
    - the event's ``source`` names the message's author.

    A title-word overlap heuristic was considered and rejected: LLM titles are
    rewritten, not quoted, so it produced matches that looked plausible and
    were not checkable. Ambiguity (two messages matching equally) resolves to
    *no* id rather than a guess. Events keep ``_source_ids: []`` when nothing
    resolves, so a caller can tell "unlinked" from "never attempted".
    """
    for ev in events:
        ev.setdefault("_source_ids", [])
        link = (ev.get("link") or "").strip()
        source = (ev.get("source") or "").strip().lower()

        hits: list[str] = []
        for m in batch:
            content = m.get("content") or ""
            author = (m.get("author") or "").strip().lower()
            matched = bool(link) and len(link) > 12 and link in content
            if not matched and author and source:
                # `source` is prompt-documented as "@author in #channel"; for
                # Gmail the author is a From header, so compare the address.
                addr = author.partition("<")[2].partition(">")[0] or author
                matched = bool(addr) and addr in source
            if matched and m.get("id"):
                hits.append(m["id"])

        # Exactly one message must claim it. Two equally-good matches means
        # the signal did not discriminate, and guessing is not allowed here.
        if len(hits) == 1:
            ev["_source_ids"] = hits

        # Platform is safe to take from the whole batch even when the message
        # is ambiguous: batches are built from the scored message list, which
        # routinely mixes platforms, but an event can only have come from one
        # of the platforms present. Where the batch is single-platform this is
        # exact; where it is not, a wrong-but-plausible platform is still
        # better than the current fallback, which labels the event with the
        # *author's email address* and produces one filter chip per person.
        ev.setdefault("_platforms", sorted({
            str(m.get("platform")) for m in batch if m.get("platform")
        }))
        if hits:
            exact = {str(m.get("platform")) for m in batch
                     if m.get("id") in hits and m.get("platform")}
            if exact:
                ev["_platforms"] = sorted(exact)


_PRIO_MAP = {"HIGH": 5, "MED": 3, "MEDIUM": 3, "LOW": 1}


def _extract_events_local(
    messages: list[dict], days_back: int, cfg: LLMConfig,
    batch_size: int = 10,
) -> tuple[str, list[dict], dict[str, list[dict]]]:
    """Extract tasks using simple numbered list format (local models).

    Processes messages in batches to stay within the model's context window.

    Returns ``(summary, events, llm_rejects)`` with the same bucket shape as
    :func:`_extract_events_cloud`. This backend previously produced no reject
    buckets at all, so a local run's `--show-rejects` output silently showed
    the *cloud* function's leftovers.
    """
    all_tasks: list[dict] = []
    summary = ""
    llm_dropped: list[dict] = []
    llm_no_events: list[dict] = []
    llm_errors: list[dict] = []

    for i in range(0, len(messages), batch_size):
        batch = messages[i : i + batch_size]
        batch_num = i // batch_size + 1
        total_batches = (len(messages) + batch_size - 1) // batch_size
        logger.info("Task extraction batch %d/%d (%d messages)", batch_num, total_batches, len(batch))

        with tracer.start_as_current_span(f"extract.batch.{batch_num - 1}") as span:
            span.set_attribute("batch.size", len(batch))
            try:
                raw = chat_completion(
                    messages=[
                        {"role": "system", "content": _EVENT_EXTRACTION_PROMPT},
                        {"role": "user", "content": build_prompt(batch, days_back)},
                    ],
                    cfg=cfg,
                    max_tokens=4096,
                )
            except Exception as e:
                logger.exception("LLM call failed on batch %d", batch_num)
                span.set_attribute("error.message", str(e))
                span.record_exception(e)
                llm_dropped.append({"title": "(batch error)", "reason": str(e)})
                # `llm_errors`, not `llm_no_events` — see the same branch in
                # `_extract_events_cloud` for what conflating them cost.
                llm_errors.extend(batch)
                continue

            batch_summary, batch_tasks, batch_dropped = _parse_event_ini(raw)
            span.set_attribute("events.extracted", len(batch_tasks))
            span.set_attribute("events.dropped_past", len(batch_dropped))
            llm_dropped.extend(batch_dropped)
            if not batch_tasks:
                logger.debug("Batch %d raw LLM output:\n%s", batch_num, raw[:500])
                llm_no_events.extend(batch)
            if not summary and batch_summary:
                summary = batch_summary
            all_tasks.extend(batch_tasks)

    # Deduplicate (fingerprint match pass)
    from event_harvester.pipeline.event_match import dedup_events
    with tracer.start_as_current_span("match.fingerprints") as span:
        unique = dedup_events(all_tasks)
        span.set_attribute("match.count", max(0, len(all_tasks) - len(unique)))
        span.set_attribute("events.in", len(all_tasks))
        span.set_attribute("events.out", len(unique))

    if llm_errors:
        logger.error(
            "Extraction incomplete: %d of %d message(s) never reached the model. "
            "This is an outage, not an empty result.",
            len(llm_errors), len(messages),
        )

    return summary, unique, {"llm_past": llm_dropped, "llm_no_events": llm_no_events,
                             "llm_errors": llm_errors}


def _parse_event_ini(raw: str) -> tuple[str, list[dict], list[dict]]:
    """Parse INI-formatted LLM output into event dicts.

    Returns ``(summary, events, dropped)``. ``dropped`` holds events whose
    date resolved to the past, for ``--show-rejects``. It is returned rather
    than stashed on the function object because this runs on up to 5 pool
    threads at once — see :class:`ExtractionResult`.
    """
    from datetime import date as date_type

    from dateutil import parser as dateutil_parser

    today = date_type.today()
    sections = _parse_llm_ini(raw)
    events = []
    dropped: list[dict] = []

    for section_name, fields in sections.items():
        if not section_name.lower().startswith("event"):
            continue

        title = fields.get("title", "").strip()
        if not title:
            continue

        date_str = fields.get("date", "TBD").strip()
        time_str = fields.get("time", "TBD").strip()
        location = fields.get("location", "TBD").strip()
        link = fields.get("link", "TBD").strip()
        source = fields.get("source", "").strip()
        desc = fields.get("details", "").strip()

        # Drop past events
        if date_str and date_str != "TBD":
            try:
                first_date = date_str.split(" to ")[0]
                parsed = dateutil_parser.parse(first_date, fuzzy=True).date()
                if parsed < today:
                    dropped.append({
                        "title": title, "date": date_str, "source": source,
                        "reason": f"past date ({parsed.isoformat()})",
                    })
                    continue
            except (ValueError, OverflowError):
                pass

        # Convert TBD to None
        date_val = date_str if date_str != "TBD" else None
        time_val = time_str if time_str != "TBD" else None
        loc_val = location if location != "TBD" else None
        link_val = link if link != "TBD" else None

        notes_parts = []
        if desc:
            notes_parts.append(desc)
        if loc_val:
            notes_parts.append(loc_val)
        if link_val:
            notes_parts.append(link_val)

        events.append({
            "title": title,
            "date": date_val,
            "time": time_val,
            "location": loc_val,
            "link": link_val,
            "notes": " | ".join(notes_parts) if notes_parts else "",
            "source": source,
            "priority": 3,
            "due_in_days": None,
        })

    return "", events, dropped
