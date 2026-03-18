"""TickTick OAuth2 authentication and task creation."""

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from event_harvester.analysis import PRIORITY_LABEL
from event_harvester.config import TickTickConfig
from event_harvester.display import BOLD, DIM, GREEN, RED, RESET

logger = logging.getLogger("event_harvester.ticktick")

_DEDUP_FILE = Path.home() / ".event_harvester" / "created_tasks.json"


def _load_created_hashes() -> set[str]:
    """Load previously created task title hashes."""
    if not _DEDUP_FILE.exists():
        return set()
    try:
        data = json.loads(_DEDUP_FILE.read_text())
        return set(data)
    except Exception:
        return set()


def _save_created_hashes(hashes: set[str]) -> None:
    """Persist created task title hashes."""
    _DEDUP_FILE.parent.mkdir(parents=True, exist_ok=True)
    _DEDUP_FILE.write_text(json.dumps(sorted(hashes)))


def _hash_task(title: str) -> str:
    return hashlib.sha256(title.strip().lower().encode()).hexdigest()[:16]


def get_ticktick_client(cfg: TickTickConfig):
    """Build and return an authenticated TickTickClient."""
    from ticktick.api import TickTickClient
    from ticktick.oauth2 import OAuth2

    if not cfg.is_configured:
        logger.warning(
            "TickTick: missing credentials — set TICKTICK_CLIENT_ID, "
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


def create_ticktick_tasks(
    tt,
    tasks: list[dict],
    project_name: str = "",
    dry_run: bool = False,
) -> list[dict]:
    """Create tasks in TickTick. Returns list of created/proposed task dicts."""
    if not tasks:
        return []

    # Load dedup hashes
    existing_hashes = _load_created_hashes()
    new_hashes: set[str] = set()

    # Resolve optional target project
    project_id = None
    if project_name:
        project_id = _find_project_id(tt, project_name)
        if project_id:
            logger.info("Using TickTick project: %s", project_name)
        else:
            logger.warning("TickTick project '%s' not found — using Inbox", project_name)

    created = []
    now = datetime.now(timezone.utc)

    for task in tasks:
        title = task.get("title", "Untitled task")[:80]
        notes = task.get("notes", "")
        priority = task.get("priority", 0)
        due_days = task.get("due_in_days")

        # Deduplication check
        task_hash = _hash_task(title)
        if task_hash in existing_hashes:
            logger.info("Skipping duplicate task: %s", title)
            continue

        due_date = None
        if due_days is not None:
            due_date = (now + timedelta(days=due_days)).strftime("%Y-%m-%dT%H:%M:%S+0000")

        prio_label = PRIORITY_LABEL.get(priority, "none")

        if dry_run:
            print(
                f"  {DIM}[dry-run]{RESET} {BOLD}{title}{RESET}\n"
                f"    priority={prio_label}"
                + (f"  due_in={due_days}d" if due_days is not None else "")
                + f"\n    {DIM}{notes}{RESET}\n"
            )
            created.append(task)
            new_hashes.add(task_hash)
            continue

        try:
            task_obj = tt.task.builder(
                title=title,
                projectId=project_id or tt.inbox_id,
                content=notes,
                priority=priority,
                dueDate=due_date,
            )
            result = tt.task.create(task_obj)
            created.append(result)
            new_hashes.add(task_hash)
            print(
                f"  {GREEN}✓{RESET} {BOLD}{title}{RESET} "
                f"{DIM}(priority={prio_label}"
                + (f", due in {due_days}d" if due_days else "")
                + f"){RESET}"
            )
        except Exception as e:
            print(f"  {RED}✗ Failed to create '{title}': {e}{RESET}")

    # Persist dedup hashes
    if new_hashes:
        _save_created_hashes(existing_hashes | new_hashes)

    return created
