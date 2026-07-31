"""TickTick OAuth2 authentication and task creation."""

import logging
from pathlib import Path
from typing import Optional

from event_harvester.config import TickTickConfig
from event_harvester.observability.metrics import count
from event_harvester.observability.tracing import fail_current_span, set_span_attrs, traced
from event_harvester.pipeline.analysis import PRIORITY_LABEL
from event_harvester.pipeline.event_match import save_fingerprint
from event_harvester.ui.display import BOLD, DIM, GREEN, RED, RESET

logger = logging.getLogger("event_harvester.outputs.ticktick")
_TRACER = "event_harvester.outputs.ticktick"

# ticktick-py's OAuth2 defaults cache_path to '.token-oauth' relative to the
# CWD, so leaving it unset drops a live access token wherever the process
# happened to start -- usually the repo root, where `git add -A` can catch it.
# Pin it under data/ instead. .gitignore covers both paths; the root one is
# kept as a guard for any token written before this was fixed.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_TOKEN_CACHE = _REPO_ROOT / "data" / ".token-oauth"


def _migrate_legacy_token_cache() -> None:
    """Move a pre-fix repo-root token into data/ so consent isn't re-prompted."""
    legacy = _REPO_ROOT / ".token-oauth"
    if legacy.exists() and not _TOKEN_CACHE.exists():
        _TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
        legacy.replace(_TOKEN_CACHE)
        logger.info("TickTick: moved token cache %s -> %s", legacy, _TOKEN_CACHE)


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
        _migrate_legacy_token_cache()
        _TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
        auth = OAuth2(
            client_id=cfg.client_id,
            client_secret=cfg.client_secret,
            redirect_uri=cfg.redirect_uri,
            cache_path=str(_TOKEN_CACHE),
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


@traced("ticktick.create_tasks", tracer_name=_TRACER)
def create_ticktick_tasks(
    tt,
    events: list[dict],
    project_name: str = "",
    dry_run: bool = False,
) -> dict[str, list]:
    """Create/update/skip events in TickTick with dedup.

    Returns ``{"created": [...], "updated": [...], "skipped": [...],
    "failed": [...]}``.

    ``failed`` exists because per-task exceptions are caught and logged here
    so one bad event cannot abort the batch — but that left callers unable to
    tell a successful sync from a failed one. The Flet UI was marking events
    approved on the strength of this function *returning*, which it does
    either way.
    """
    # The only stage that writes to a system outside this machine, so it is
    # the one where "what actually happened" matters most.
    set_span_attrs(**{
        "ticktick.event.in": len(events),
        "ticktick.dry_run": bool(dry_run),
        "ticktick.project": project_name or "(default)",
    })
    if not events:
        return {"created": [], "updated": [], "skipped": [], "failed": []}

    # Sync existing tasks for dedup
    existing_tasks = sync_tasks(tt, project_name)

    # Run dedup if we have existing tasks
    if existing_tasks:
        from event_harvester.pipeline.dedup import dedup_events_against_ticktick
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

    result = {
        "created": [],
        "updated": [],
        "skipped": [t.get("title", "") for t in to_skip],
        "failed": [],
    }

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
            save_fingerprint(event, matched_task.get("id"), status="approved")
            print(f"  {GREEN}~{RESET} {BOLD}{title}{RESET} {DIM}(merged new details){RESET}")
            result["updated"].append(title)
        else:
            print(f"  {DIM}= {title} (nothing new to merge){RESET}")
            result["skipped"].append(title)

    # Handle creates
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
            # status="approved", not the "auto" default. Creating a TickTick
            # task *is* the approval — but `find_acted_fingerprint` only
            # honours "approved"/"declined", so an "auto" entry meant every
            # CLI-synced event came back tagged `new` in the next MCP or Flet
            # run and was offered for approval again.
            save_fingerprint(
                event,
                created_task.get("id") if isinstance(created_task, dict) else None,
                status="approved",
            )
            result["created"].append(title)
            print(f"  {GREEN}+{RESET} {BOLD}{title}{RESET} {DIM}({prio_label}){RESET}")
        except Exception as e:
            result["failed"].append(title)
            logger.warning("TickTick: failed to create %r: %s", title, e)
            print(f"  {RED}x Failed to create '{title}': {e}{RESET}")

    set_span_attrs(**{
        "ticktick.created": len(result["created"]),
        "ticktick.updated": len(result["updated"]),
        "ticktick.skipped": len(result["skipped"]),
        "ticktick.failed": len(result["failed"]),
    })
    # The only stage that writes outside this machine, so it is the one worth
    # being able to alert on. A per-task failure is caught and printed inside
    # the loop, which means without this counter a run that created nothing
    # and failed everything is indistinguishable from a quiet night.
    for outcome in ("created", "updated", "skipped", "failed"):
        if result[outcome]:
            count("ticktick.tasks", len(result[outcome]), outcome=outcome,
                  dry_run=bool(dry_run),
                  description="TickTick task operations by outcome")
    if result["failed"]:
        fail_current_span(
            f"{len(result['failed'])} TickTick task(s) failed to create",
            error_type="ticktick_partial_failure",
        )
    return result
