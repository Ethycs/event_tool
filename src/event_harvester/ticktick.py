"""TickTick OAuth2 authentication and task creation."""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from event_harvester.analysis import PRIORITY_LABEL
from event_harvester.config import TickTickConfig
from event_harvester.display import BOLD, DIM, GREEN, RED, RESET
from event_harvester.event_match import save_fingerprint

logger = logging.getLogger("event_harvester.ticktick")


def get_ticktick_client(cfg: TickTickConfig):
    """Build and return an authenticated TickTickClient."""
    from ticktick.api import TickTickClient
    from ticktick.oauth2 import OAuth2

    if not cfg.is_configured:
        logger.warning(
            "TickTick: missing credentials - set TICKTICK_CLIENT_ID, "
            "TICKTICK_CLIENT_SECRET, TICKTICK_USERNAME, TICKTICK_PASSWORD"
        )
        return None

    try:
        auth = OAuth2(
            client_id=cfg.client_id,
            client_secret=cfg.client_secret,
            redirect_uri=cfg.redirect_uri,
            check_cache=True,
        )
        tt = TickTickClient(cfg.username, cfg.password, auth)
        return tt
    except Exception as e:
        logger.error("TickTick auth failed: %s", e)
        return None


def _find_project_id(tt, name: str) -> Optional[str]:
    """Look up a TickTick project ID by name (case-insensitive)."""
    for project in tt.state.get("projects", []):
        if project.get("name", "").lower() == name.lower():
            return project.get("id")
    return None


def sync_tasks(tt, project_name: str = "") -> list[dict]:
    """Sync TickTick state and return existing tasks from the target project."""
    try:
        tt.sync()
    except Exception as e:
        logger.warning("TickTick sync failed: %s", e)
        return []

    project_id = None
    if project_name:
        project_id = _find_project_id(tt, project_name)

    tasks = tt.state.get("tasks", [])
    if project_id:
        tasks = [t for t in tasks if t.get("projectId") == project_id]

    logger.info("TickTick: synced %d existing tasks.", len(tasks))
    return tasks


def update_task_details(tt, task: dict, new_event: dict) -> dict | None:
    """Merge new event details into an existing TickTick task."""
    existing_content = task.get("content") or ""

    # Build new info to append
    new_parts = []
    for field in ("notes", "link", "location", "time"):
        val = new_event.get(field)
        if val and val.lower() not in existing_content.lower():
            new_parts.append(val)

    source = new_event.get("source", "")
    if source:
        new_parts.append(f"(via {source})")

    if not new_parts:
        return None

    # Append with separator
    separator = "\n---\n" if existing_content else ""
    task["content"] = existing_content + separator + " | ".join(new_parts)

    # Update due date if existing has none
    if not task.get("dueDate") and new_event.get("date"):
        try:
            from dateutil import parser as dateutil_parser
            dt = dateutil_parser.parse(new_event["date"])
            task["dueDate"] = dt.strftime("%Y-%m-%dT%H:%M:%S+0000")
        except (ValueError, OverflowError):
            pass

    # Keep higher priority
    new_prio = new_event.get("priority", 0)
    if new_prio > (task.get("priority") or 0):
        task["priority"] = new_prio

    try:
        result = tt.task.update(task)
        return result
    except Exception as e:
        logger.error("Failed to update task '%s': %s", task.get("title"), e)
        return None


def create_ticktick_tasks(
    tt,
    events: list[dict],
    project_name: str = "",
    dry_run: bool = False,
) -> dict[str, list]:
    """Create/update/skip events in TickTick with dedup.

    Returns {"created": [...], "updated": [...], "skipped": [...]}.
    """
    if not events:
        return {"created": [], "updated": [], "skipped": []}

    # Sync existing tasks for dedup
    existing_tasks = sync_tasks(tt, project_name)

    # Run dedup if we have existing tasks
    if existing_tasks:
        from event_harvester.dedup import dedup_events_against_ticktick
        to_create, to_update, to_skip = dedup_events_against_ticktick(events, existing_tasks)
    else:
        to_create, to_update, to_skip = events, [], []

    # Resolve project
    project_id = None
    if project_name:
        project_id = _find_project_id(tt, project_name)
        if project_id:
            logger.info("Using TickTick project: %s", project_name)
        else:
            logger.warning("TickTick project '%s' not found - using Inbox", project_name)

    result = {"created": [], "updated": [], "skipped": [t.get("title", "") for t in to_skip]}

    # Log skips
    for event in to_skip:
        logger.info("Skipping (fingerprint match): %s", event.get("title", "Untitled"))
        print(f"  {DIM}= {event.get('title', 'Untitled')} (already tracked){RESET}")

    # Handle updates
    for event, matched_task in to_update:
        title = matched_task.get("title", "Untitled")
        if dry_run:
            print(f"  {DIM}[dry-run] ~ {title} (would merge new details){RESET}")
            result["updated"].append(title)
            continue

        updated = update_task_details(tt, matched_task, event)
        if updated:
            save_fingerprint(event, matched_task.get("id"))
            print(f"  {GREEN}~{RESET} {BOLD}{title}{RESET} {DIM}(merged new details){RESET}")
            result["updated"].append(title)
        else:
            print(f"  {DIM}= {title} (nothing new to merge){RESET}")
            result["skipped"].append(title)

    # Handle creates
    now = datetime.now(timezone.utc)
    for event in to_create:
        title = event.get("title", "Untitled")[:80]
        notes = event.get("notes", "")
        priority = event.get("priority", 0)

        # Build due date from event date field
        due_date = None
        if event.get("date"):
            try:
                from dateutil import parser as dateutil_parser
                dt = dateutil_parser.parse(event["date"])
                due_date = dt.strftime("%Y-%m-%dT%H:%M:%S+0000")
            except (ValueError, OverflowError):
                pass

        prio_label = PRIORITY_LABEL.get(priority, "none")

        if dry_run:
            print(
                f"  {DIM}[dry-run]{RESET} {GREEN}+{RESET} {BOLD}{title}{RESET}\n"
                f"    {DIM}{notes}{RESET}\n"
            )
            result["created"].append(title)
            continue

        try:
            task_obj = tt.task.builder(
                title=title,
                projectId=project_id or tt.inbox_id,
                content=notes,
                priority=priority,
                dueDate=due_date,
            )
            created_task = tt.task.create(task_obj)
            save_fingerprint(event, created_task.get("id") if isinstance(created_task, dict) else None)
            result["created"].append(title)
            print(f"  {GREEN}+{RESET} {BOLD}{title}{RESET} {DIM}({prio_label}){RESET}")
        except Exception as e:
            print(f"  {RED}x Failed to create '{title}': {e}{RESET}")

    return result
