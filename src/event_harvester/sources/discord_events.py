"""Discord guild *scheduled events*, ingested from a local client-mod export.

Discord stopped writing message JSON to its desktop HTTP cache, so
``discord.py`` finds nothing there any more (verified 2026-08-16: 0 of 4,257
cache entries were ``/channels/{id}/messages``). Guild **scheduled events** were
never in that cache at all — they live only in the running client's in-memory
Flux store, ``GuildScheduledEventStore``, and arrive over the gateway as
``GUILD_SCHEDULED_EVENT_CREATE`` / ``_UPDATE`` dispatches.

The supported ways to read them are a bot with the ``GUILD_SCHEDULED_EVENTS``
intent (needs *Manage Server* to invite) or a client mod that reads the store
from inside the renderer. This module is the harvester half of the second path:
a Vencord/BetterDiscord plugin (see ``integrations/discord-vencord-plugin/``)
writes the store to a local JSON file, and this reads it. **No network, no
automation, no messages sent** — the plugin only reads and writes a file, which
is the least-exposed shape a client mod can take.

## Why these become messages, not events directly

A scheduled event is already structured — it has an exact start time, a title
and (for external events) a location. It would be tempting to inject it straight
past the LLM. It flows through the normal pipeline instead, as a ``discord``
message whose ``content`` states the date in plain text, because that is the one
path every other source already takes and the local model extracts a
pre-dated message reliably (the date is resolved from the same content it
reads). Keeping one pipeline is worth more here than saving one cheap call.

The start time is converted from Discord's UTC to the **machine's local zone**
before it is written into the content, because the harvester runs on the
viewer's own machine, so its local zone is the right one to show — a 7pm event
must not read as 2am the next day just because the source stored it in UTC.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("event_harvester.discord_events")

#: A scheduled event's status. Discord: 1 SCHEDULED, 2 ACTIVE, 3 COMPLETED,
#: 4 CANCELED. We keep 1 and 2 — a completed or cancelled event is not an
#: invitation.
_LIVE_STATUS = {1, 2}


def default_export_path() -> Path:
    """Where the client-mod plugin drops its export.

    ``DISCORD_EVENTS_EXPORT`` overrides it. The default is a directory the
    Discord client's own process can always reach on each OS, deliberately
    outside this repo so the plugin needs no knowledge of where the harvester
    lives.
    """
    override = os.environ.get("DISCORD_EVENTS_EXPORT")
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "discord-harvester" / "scheduled-events.json"
    return Path.home() / ".config" / "discord-harvester" / "scheduled-events.json"


def _local(iso: str, now: datetime) -> datetime | None:
    """Parse a UTC ISO string and move it to the machine's local zone."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(now.astimezone().tzinfo)


def _fmt_time(dt: datetime) -> str:
    """`7:00 PM`, without a platform-specific `%-I` (absent on Windows)."""
    return dt.strftime("%I:%M %p").lstrip("0")


def _content(ev: dict, start: datetime, end: datetime | None) -> str:
    """The message body. The date is stated in plain text so the extractor
    resolves it from the same slice it reads — the pre-dated-message pattern."""
    lines = [str(ev.get("name") or "Discord event").strip()]
    desc = str(ev.get("description") or "").strip()
    if desc:
        lines.append(desc)
    when = f"When: {start.strftime('%A %Y-%m-%d')} at {_fmt_time(start)}"
    if end:
        when += f" until {_fmt_time(end)}"
    lines.append(when)
    loc = str(ev.get("location") or "").strip()
    if loc:
        lines.append(f"Where: {loc}")
    guild = str(ev.get("guild_name") or "").strip()
    lines.append(f"Discord scheduled event{f' · {guild}' if guild else ''}")
    return "\n".join(lines)


def scheduled_events_to_messages(
    data: dict, now: datetime | None = None,
) -> list[dict]:
    """Convert one export payload into ``discord`` message records.

    Pure and offline — the whole reason the file/reader split exists. Drops
    events that are cancelled, completed, past, or missing a start time. The
    message ``timestamp`` is *now* (when we learned of it), not the event's
    start: the event's date belongs in ``content`` for extraction, and a future
    timestamp would confuse recency scoring and the days-back cutoff.
    """
    now = now or datetime.now(timezone.utc)
    events = (data or {}).get("events") or []
    out: list[dict] = []
    seen: set[str] = set()

    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("status") is not None and ev["status"] not in _LIVE_STATUS:
            continue
        ev_id = str(ev.get("id") or "").strip()
        start = _local(str(ev.get("scheduled_start_time") or ""), now)
        if not ev_id or start is None or ev_id in seen:
            continue
        # Past events are not invitations. Compare in UTC to avoid a tz trap.
        if start.astimezone(timezone.utc) < now.astimezone(timezone.utc):
            continue
        seen.add(ev_id)
        end = _local(str(ev.get("scheduled_end_time") or ""), now)
        guild = str(ev.get("guild_name") or "").strip()
        rec = {
            "platform": "discord",
            "id": f"gse:{ev_id}",
            "timestamp": now.astimezone(timezone.utc).isoformat(),
            "author": str(ev.get("creator") or guild or "Discord").strip(),
            "channel": guild or "Scheduled Events",
            "content": _content(ev, start, end),
        }
        out.append(rec)

    out.sort(key=lambda m: m["id"])
    return out


def read_scheduled_events(
    path: Path | None = None, now: datetime | None = None,
) -> list[dict]:
    """Read the plugin's export file and return message records.

    A missing file is not an error — it means the plugin is not installed or
    Discord is not running, and the source's 0-message warning already covers
    that. A stale export is logged but still used: a scheduled event for next
    week is valid even if it was dumped two days ago.
    """
    path = path or default_export_path()
    now = now or datetime.now(timezone.utc)
    if not path.exists():
        logger.debug("Discord events: no export at %s", path)
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Discord events: could not read %s: %s", path, e)
        return []

    exported_at = _local(str(data.get("exported_at") or ""), now)
    if exported_at is not None:
        age_h = (now.astimezone(timezone.utc)
                 - exported_at.astimezone(timezone.utc)).total_seconds() / 3600
        if age_h > 24:
            logger.warning(
                "Discord events: export is %.0fh old (%s) — is the plugin "
                "running?", age_h, path,
            )

    msgs = scheduled_events_to_messages(data, now=now)
    logger.info("Discord events: %d scheduled event(s) from %s", len(msgs), path)
    return msgs
