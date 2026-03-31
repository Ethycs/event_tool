"""LLM-based task extraction from messages."""

import json
import logging
import re

from event_harvester.config import LLMConfig
from event_harvester.llm import chat_completion

logger = logging.getLogger("event_harvester.analysis")


from event_harvester.utils import parse_llm_ini

# Backward-compat alias — existing internal callers use this name.
_parse_llm_ini = parse_llm_ini


PRIORITY_LABEL = {0: "none", 1: "low", 3: "medium", 5: "high"}


def build_prompt(messages: list[dict], days_back: int) -> str:
    """Build the event extraction prompt from collected messages.

    Includes today's date and pre-resolved dates/times per message
    so the LLM doesn't have to re-parse relative dates.
    """
    from datetime import date, datetime
    from event_harvester.weights import DATE_RE, TIME_RE, _resolve_date

    today = date.today()

    grouped: dict[str, list[dict]] = {}
    for msg in messages:
        key = f"{msg['platform'].capitalize()} / {msg['channel']}"
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
        lines.append(f"### {chat}  ({len(msgs)} messages)")
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
    return "\n".join(lines)


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


def prioritize(messages: list[dict], max_messages: int = 150) -> list[dict]:
    """Sort messages by event signal strength, cap to fit time budget.

    150 messages = 15 batches of 10 ≈ 2 minutes on local LLM.
    """
    from event_harvester.weights import has_date_or_event_signal, URL_RE

    def _score(m: dict) -> int:
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
        return s

    scored = sorted(messages, key=_score, reverse=True)
    if len(scored) > max_messages:
        logger.info("Priority cap: %d -> %d messages (top by date/link/pinned score).",
                     len(scored), max_messages)
    return scored[:max_messages]


_ACTIONABLE_SIGNALS = [
    "action required", "action needed", "action item",
    "please", "could you", "can you", "let me know",
    "respond", "reply", "reminder", "follow up",
    "?",  # questions
]

# Merge in shared scheduling keywords to avoid maintaining two lists.
# Import is deferred to avoid circular import at module load.
def _get_actionable_signals() -> list[str]:
    from event_harvester.weights import SCHEDULING_KEYWORDS
    return _ACTIONABLE_SIGNALS + SCHEDULING_KEYWORDS


def extract_events_llm(
    messages: list[dict],
    days_back: int,
    cfg: LLMConfig,
) -> tuple[str, list[dict]]:
    """Send messages to LLM and return (summary, tasks)."""
    if not cfg.is_configured:
        logger.warning("LLM not configured - skipping analysis.")
        return "", []

    # Always run classifier + reranker to cut messages before hitting any LLM
    from event_harvester.classifier import filter_actionable as classifier_filter, has_trained_models

    total = len(messages)

    if has_trained_models():
        filtered = classifier_filter(messages)
        n_after_clf = len(filtered)

        try:
            from event_harvester.reranker import rerank_messages
            filtered = rerank_messages(filtered, top_k=150)
        except ImportError:
            logger.warning("sentence-transformers not installed, falling back to regex priority.")
            filtered = prioritize(filtered)

        logger.info(
            "Event extraction: %d -> %d (classifier) -> %d (reranker) -> LLM (%s)",
            total, n_after_clf, len(filtered), cfg.display_name,
        )
    else:
        filtered = prioritize(messages)
        logger.info(
            "Event extraction: %d -> %d messages (priority) -> LLM (%s)",
            total, len(filtered), cfg.display_name,
        )

    if cfg.backend == "local":
        return _extract_events_local(filtered, days_back, cfg)

    logger.info("Sending %d messages to %s (backend=%s) ...", len(filtered), cfg.display_name, cfg.backend)
    return _extract_events_cloud(filtered, days_back, cfg)


def _extract_events_cloud(
    messages: list[dict], days_back: int, cfg: LLMConfig,
    batch_size: int = 10,
) -> tuple[str, list[dict]]:
    """Extract events using parallel batched requests (cloud APIs handle concurrency)."""
    import concurrent.futures

    batches = [messages[i:i + batch_size] for i in range(0, len(messages), batch_size)]
    logger.info("Cloud extraction: %d messages in %d parallel batches", len(messages), len(batches))

    def _process_batch(batch):
        try:
            raw = chat_completion(
                messages=[
                    {"role": "system", "content": _EVENT_EXTRACTION_PROMPT},
                    {"role": "user", "content": build_prompt(batch, days_back)},
                ],
                cfg=cfg,
                max_tokens=4096,
            )
            _, tasks = _parse_event_ini(raw)
            if not tasks:
                logger.debug("Batch raw output:\n%s", raw[:500])
            return tasks
        except Exception as e:
            logger.error("Batch failed: %s", e)
            return []

    all_tasks: list[dict] = []
    raw_outputs: list[str] = []

    def _process_batch_with_raw(batch):
        try:
            raw = chat_completion(
                messages=[
                    {"role": "system", "content": _EVENT_EXTRACTION_PROMPT},
                    {"role": "user", "content": build_prompt(batch, days_back)},
                ],
                cfg=cfg,
                max_tokens=4096,
            )
            _, tasks = _parse_event_ini(raw)
            return tasks, raw
        except Exception as e:
            logger.error("Batch failed: %s", e)
            return [], ""

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(_process_batch_with_raw, b) for b in batches]
        for f in concurrent.futures.as_completed(futures):
            tasks, raw = f.result()
            all_tasks.extend(tasks)
            if raw:
                raw_outputs.append(raw)

    if not all_tasks and raw_outputs:
        logger.warning("No events parsed. First batch raw output:\n%s", raw_outputs[0][:1000])

    # Deduplicate
    from event_harvester.event_match import dedup_events
    unique = dedup_events(all_tasks)

    return "", unique


_PRIO_MAP = {"HIGH": 5, "MED": 3, "MEDIUM": 3, "LOW": 1}


def _extract_events_local(
    messages: list[dict], days_back: int, cfg: LLMConfig,
    batch_size: int = 10,
) -> tuple[str, list[dict]]:
    """Extract tasks using simple numbered list format (local models).

    Processes messages in batches to stay within the model's context window.
    """
    all_tasks: list[dict] = []
    summary = ""

    for i in range(0, len(messages), batch_size):
        batch = messages[i : i + batch_size]
        batch_num = i // batch_size + 1
        total_batches = (len(messages) + batch_size - 1) // batch_size
        logger.info("Task extraction batch %d/%d (%d messages)", batch_num, total_batches, len(batch))

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
            logger.error("LLM call failed on batch %d: %s", batch_num, e)
            continue

        batch_summary, batch_tasks = _parse_event_ini(raw)
        if not batch_tasks:
            logger.debug("Batch %d raw LLM output:\n%s", batch_num, raw[:500])
        if not summary and batch_summary:
            summary = batch_summary
        all_tasks.extend(batch_tasks)

    # Deduplicate
    from event_harvester.event_match import dedup_events
    unique = dedup_events(all_tasks)

    return summary, unique


def _parse_event_ini(raw: str) -> tuple[str, list[dict]]:
    """Parse INI-formatted LLM output into event dicts.

    Drops events with dates that resolve to the past.
    """
    import configparser
    from datetime import date as date_type
    from dateutil import parser as dateutil_parser

    today = date_type.today()
    sections = _parse_llm_ini(raw)
    events = []

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

    return "", events
