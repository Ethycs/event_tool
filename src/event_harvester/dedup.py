"""Event deduplication using structured field matching.

Matches new events against existing TickTick tasks to avoid duplicates.
Uses event_match for structured comparison by title, date, location, and link.
"""

import logging

from event_harvester.event_match import events_match, find_fingerprint

logger = logging.getLogger("event_harvester.dedup")


def _merge_action(new_event: dict, existing_task: dict) -> str:
    """Determine if there's new info to merge, or if it's a skip.

    Returns "update" if the new event has info not in the existing task,
    "skip" if the existing task already has everything.
    """
    existing_content = (existing_task.get("content") or "") + (existing_task.get("title") or "")
    existing_lower = existing_content.lower()

    for field in ("link", "location", "time"):
        val = new_event.get(field)
        if val and val.lower() not in existing_lower:
            return "update"

    notes = new_event.get("notes", "")
    if notes and notes.lower() not in existing_lower:
        return "update"

    return "skip"


def dedup_events_against_ticktick(events, existing_tasks, reranker_model=None):
    """Classify events as create, update, or skip against TickTick tasks."""
    to_create, to_update, to_skip = [], [], []

    for event in events:
        # Check fingerprint first (fast)
        fp = find_fingerprint(event)
        if fp:
            to_skip.append(event)
            continue

        # Check against TickTick tasks
        matched = None
        for task in existing_tasks:
            task_as_event = {
                "title": task.get("title", ""),
                "date": task.get("dueDate", "")[:10] if task.get("dueDate") else None,
                "location": None,
                "link": None,
            }
            is_match, score = events_match(event, task_as_event)
            if is_match:
                matched = task
                break

        if matched:
            action = _merge_action(event, matched)
            if action == "update":
                to_update.append((event, matched))
            else:
                to_skip.append(event)
        else:
            to_create.append(event)

    logger.info(
        "Dedup: %d events -> %d create, %d update, %d skip.",
        len(events), len(to_create), len(to_update), len(to_skip),
    )
    return to_create, to_update, to_skip
