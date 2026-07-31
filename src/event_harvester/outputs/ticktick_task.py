"""A TickTick Open API v1 task object, and the path from an event to one.

Until now a task was assembled inline in `create_ticktick_tasks` and handed to
`ticktick-py`'s `task.builder(...)`, so the shape that reached the API was
whatever that library produced from whatever the pipeline happened to hold.
Two things were wrong against the published schema, and both are silent:

* **Priority is an enum, not a scale.** The API accepts ``0 | 1 | 3 | 5``. The
  pipeline's `priority` is an LLM-assigned importance where 2 and 4 occur, and
  those were forwarded verbatim.
* **Every due date claimed UTC.** `dt.strftime("%Y-%m-%dT%H:%M:%S+0000")` pins
  `+0000` onto a naive datetime parsed from a venue page. A 9pm San Francisco
  show became 21:00Z — 2pm local. Tasks landed seven hours early, every time,
  and nothing in the pipeline would notice.

Reference: `POST /open/v1/task` (create), `POST /open/v1/task/{taskId}`
(update), base `https://api.ticktick.com/open/v1`. Date format
``yyyy-MM-dd'T'HH:mm:ssZ``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from enum import IntEnum
from typing import Any


class Priority(IntEnum):
    """TickTick task priority. The API rejects values outside this set."""

    NONE = 0
    LOW = 1
    MEDIUM = 3
    HIGH = 5


class TaskStatus(IntEnum):
    """Task completion state. `1` is a checklist-item value, not a task value."""

    ACTIVE = 0
    COMPLETED = 2


class ItemStatus(IntEnum):
    """Checklist-item state. Note this differs from :class:`TaskStatus`."""

    INCOMPLETE = 0
    COMPLETE = 1


#: The exact wire format: `yyyy-MM-dd'T'HH:mm:ssZ`, e.g. 2026-07-30T21:00:00-0700.
API_DATETIME = "%Y-%m-%dT%H:%M:%S%z"


def coerce_priority(value: Any) -> Priority:
    """Map an arbitrary importance score onto a legal API priority.

    The pipeline emits 0–5 and `PRIORITY_LABEL` only names 0/1/3/5, so 2 and 4
    are produced by the LLM and were previously sent as-is. Rounding *up* is
    deliberate: an event the model thought was a 4 is closer to "high" than to
    "medium", and under-prioritising a real event costs more than the reverse.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return Priority.NONE
    if number <= 0:
        return Priority.NONE
    if number <= 1:
        return Priority.LOW
    if number <= 3:
        return Priority.MEDIUM
    return Priority.HIGH


def format_api_datetime(value: datetime, default_tz: tzinfo | None = None) -> str:
    """Render a datetime in TickTick's wire format, with a **real** offset.

    A naive datetime is assumed to be local wall-clock time — which is what a
    scraped event date is — and is attached to `default_tz` (the system zone
    unless given) rather than silently relabelled UTC.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=default_tz or datetime.now().astimezone().tzinfo)
    return value.strftime(API_DATETIME)


@dataclass
class ChecklistItem:
    """One subtask. `status` uses :class:`ItemStatus`, not :class:`TaskStatus`."""

    title: str
    status: ItemStatus = ItemStatus.INCOMPLETE
    id: str | None = None
    sortOrder: int | None = None
    startDate: str | None = None
    isAllDay: bool | None = None
    timeZone: str | None = None
    completedTime: str | None = None

    def to_api(self) -> dict[str, Any]:
        return _drop_none({
            "id": self.id,
            "title": self.title,
            "status": int(self.status),
            "sortOrder": self.sortOrder,
            "startDate": self.startDate,
            "isAllDay": self.isAllDay,
            "timeZone": self.timeZone,
            "completedTime": self.completedTime,
        })


def _drop_none(payload: dict[str, Any]) -> dict[str, Any]:
    """Omit unset fields rather than sending nulls.

    TickTick treats an explicit `null` as "clear this", which on the update
    endpoint would wipe fields the caller never meant to touch.
    """
    return {k: v for k, v in payload.items() if v is not None}


@dataclass
class TickTickTask:
    """A TickTick Open API v1 task.

    Field names are the API's, in the API's casing, deliberately — this object
    is the wire contract, and renaming to snake_case would put a translation
    layer between the schema and the thing that has to match it.
    """

    title: str
    projectId: str | None = None
    id: str | None = None
    content: str | None = None
    desc: str | None = None
    isAllDay: bool | None = None
    startDate: str | None = None
    dueDate: str | None = None
    timeZone: str | None = None
    reminders: list[str] = field(default_factory=list)
    repeatFlag: str | None = None
    priority: Priority = Priority.NONE
    status: TaskStatus = TaskStatus.ACTIVE
    completedTime: str | None = None
    sortOrder: int | None = None
    items: list[ChecklistItem] = field(default_factory=list)

    # ── Validation ──────────────────────────────────────────────────

    def validate(self) -> list[str]:
        """Return API-compliance problems. Empty means the payload is legal."""
        errors: list[str] = []
        if not (self.title or "").strip():
            errors.append("title is required and must be non-empty")
        if int(self.priority) not in {int(p) for p in Priority}:
            errors.append(f"priority {self.priority!r} is not one of 0, 1, 3, 5")
        if int(self.status) not in {int(s) for s in TaskStatus}:
            errors.append(f"status {self.status!r} is not one of 0, 2")
        for label in ("startDate", "dueDate", "completedTime"):
            value = getattr(self, label)
            if value is None:
                continue
            try:
                datetime.strptime(value, API_DATETIME)
            except (ValueError, TypeError):
                errors.append(f"{label} {value!r} is not {API_DATETIME} (yyyy-MM-dd'T'HH:mm:ssZ)")
        if self.isAllDay and self.dueDate is None and self.startDate is None:
            errors.append("isAllDay is set but neither startDate nor dueDate is")
        return errors

    # ── Wire formats ────────────────────────────────────────────────

    def to_api(self) -> dict[str, Any]:
        """The JSON body for `POST /open/v1/task`."""
        payload = _drop_none({
            "id": self.id,
            "title": self.title,
            "projectId": self.projectId,
            "content": self.content,
            "desc": self.desc,
            "isAllDay": self.isAllDay,
            "startDate": self.startDate,
            "dueDate": self.dueDate,
            "timeZone": self.timeZone,
            "repeatFlag": self.repeatFlag,
            "priority": int(self.priority),
            "status": int(self.status),
            "completedTime": self.completedTime,
            "sortOrder": self.sortOrder,
        })
        if self.reminders:
            payload["reminders"] = list(self.reminders)
        if self.items:
            payload["items"] = [i.to_api() for i in self.items]
        return payload

    def to_update_api(self) -> dict[str, Any]:
        """The JSON body for `POST /open/v1/task/{taskId}`.

        The update endpoint requires both `id` and `projectId`; sending a body
        without them silently targets nothing.
        """
        payload = self.to_api()
        payload["id"] = self.id
        payload["projectId"] = self.projectId
        return payload

    # ── Construction ────────────────────────────────────────────────

    @classmethod
    def from_event(
        cls,
        event: dict[str, Any],
        *,
        project_id: str | None = None,
        tz_name: str | None = None,
        default_tz: tzinfo | None = None,
    ) -> TickTickTask:
        """Build a compliant task from a pipeline event.

        `isAllDay` is inferred from whether the event carries a time. An event
        with a date and no time is an all-day task; previously it was sent as a
        timed task at midnight, so "Saturday's street fair" showed up as a
        00:00 appointment.
        """
        title = str(event.get("title") or "Untitled")[:255]

        when = _parse_event_datetime(event)
        is_all_day = bool(when) and not _has_time(event)
        stamp = format_api_datetime(when, default_tz) if when else None

        content_parts = [
            str(event.get("notes") or event.get("details") or "").strip(),
            str(event.get("location") or "").strip(),
            str(event.get("link") or "").strip(),
            str(event.get("source") or "").strip(),
        ]
        content = "\n\n".join(p for p in content_parts if p) or None

        return cls(
            title=title,
            projectId=project_id,
            content=content,
            isAllDay=is_all_day or None,
            startDate=stamp,
            dueDate=stamp,
            timeZone=tz_name,
            priority=coerce_priority(event.get("priority", 0)),
        )


def _has_time(event: dict[str, Any]) -> bool:
    if str(event.get("time") or "").strip():
        return True
    raw = str(event.get("date") or "")
    return "T" in raw or ":" in raw


def _parse_event_datetime(event: dict[str, Any]) -> datetime | None:
    """Parse an event's date/time into a datetime, or None.

    Tries `date` + `time` together first so a `"2026-07-30"` / `"9pm"` pair
    becomes 21:00 rather than midnight.
    """
    raw_date = str(event.get("date") or "").strip()
    if not raw_date:
        return None

    raw_time = str(event.get("time") or "").strip()
    candidates = [f"{raw_date} {raw_time}".strip()] if raw_time else []
    candidates.append(raw_date)

    try:
        from dateutil import parser as dateutil_parser
    except ImportError:  # pragma: no cover - dateutil is a hard dependency
        return None

    for candidate in candidates:
        try:
            return dateutil_parser.parse(candidate)
        except (ValueError, OverflowError, TypeError):
            continue
    return None


def local_tz_name() -> str | None:
    """Best-effort IANA zone name for the `timeZone` field.

    Returns None rather than guessing when the platform will not say — an
    absent `timeZone` lets TickTick apply the account default, which is a
    better failure than asserting the wrong zone.
    """
    try:
        from tzlocal import get_localzone_name

        return get_localzone_name()
    except Exception:
        pass
    try:
        name = datetime.now().astimezone().tzinfo
        key = getattr(name, "key", None)  # zoneinfo carries .key
        if key:
            return str(key)
    except Exception:
        pass
    return None


def utcnow_api() -> str:
    """Current time in the API's format, for `completedTime`."""
    return datetime.now(timezone.utc).strftime(API_DATETIME)
