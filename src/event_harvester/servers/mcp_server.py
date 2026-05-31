"""MCP server for Event Harvester.

The MCP surface intentionally mirrors the existing CLI workflow while keeping
side effects explicit:

* ``harvest_events`` gathers messages and extracts event candidates.
* ``approve_event`` creates/updates TickTick tasks for a selected event.
* ``decline_event`` records a fingerprint so the event is skipped later.

The external ``mcp`` SDK is imported lazily so normal CLI/tests still work in
environments where the server dependency has not been installed yet.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from event_harvester import __version__
from event_harvester.cli.parse_helpers import VALID_PLATFORMS, resolve_platforms
from event_harvester.config import AppConfig, load_config
from event_harvester.pipeline.event_match import find_acted_fingerprint, save_fingerprint
from event_harvester.pipeline import Pipeline
from event_harvester.outputs.report import generate_report
from event_harvester.utils import load_json, save_json

logger = logging.getLogger("event_harvester.mcp")

DATA_DIR = Path("data")
LATEST_EVENTS_FILE = DATA_DIR / ".mcp_events.json"


def _parse_source_filter(value: str | list[str] | tuple[str, ...] | None) -> set[str] | None:
    """Parse MCP source filters from comma-separated strings or JSON arrays."""
    if value is None:
        return None
    if isinstance(value, str):
        if not value.strip():
            return None
        items = {item.strip().lower() for item in value.split(",") if item.strip()}
    else:
        items = {str(item).strip().lower() for item in value if str(item).strip()}
        if not items:
            return None

    bad = items - VALID_PLATFORMS
    if bad:
        valid = ", ".join(sorted(VALID_PLATFORMS))
        raise ValueError(f"Unknown source(s): {', '.join(sorted(bad))}. Valid sources: {valid}")
    return items


def _source_kwargs(
    cfg: AppConfig,
    *,
    only: str | list[str] | tuple[str, ...] | None = None,
    skip: str | list[str] | tuple[str, ...] | None = None,
) -> dict[str, bool]:
    """Resolve MCP source filters into harvest_messages no_* kwargs."""
    only_set = _parse_source_filter(only)
    skip_set = _parse_source_filter(skip)
    if only_set is not None and skip_set is not None:
        raise ValueError("Use either 'only' or 'skip', not both.")
    if only_set is None and skip_set is None:
        return cfg.sources.to_no_kwargs()
    return resolve_platforms(only_set, skip_set)


def _enabled_sources(no_kwargs: dict[str, bool]) -> list[str]:
    return sorted(key.removeprefix("no_") for key, disabled in no_kwargs.items() if not disabled)


def _event_status(event: dict[str, Any]) -> str:
    if event.get("_status"):
        return str(event["_status"])
    fp = find_acted_fingerprint(event)
    if fp is None:
        return "new"
    return fp.get("status") or "new"


def _events_for_report(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert extracted events into the report.py shape."""
    out: list[dict[str, Any]] = []
    for event in events:
        source = event.get("source", "") or ""
        author = source.split(" in ")[0].strip().lstrip("@") if " in " in source else ""
        channel = source.split(" in ")[1].strip().lstrip("#") if " in " in source else ""
        out.append({
            "title": event.get("title", "Untitled"),
            "date": event.get("date"),
            "time": event.get("time"),
            "location": event.get("location"),
            "link": event.get("link"),
            "details": event.get("details") or event.get("notes", ""),
            "score": event.get("priority", 3),
            "source": source,
            "author": author,
            "channel": channel,
        })
    return out


def _public_event(event: dict[str, Any], index: int) -> dict[str, Any]:
    """Return a compact, stable event shape for MCP clients."""
    return {
        "index": index,
        "status": _event_status(event),
        "title": event.get("title"),
        "date": event.get("date"),
        "time": event.get("time"),
        "location": event.get("location"),
        "link": event.get("link"),
        "source": event.get("source"),
        "notes": event.get("notes") or event.get("details"),
        "priority": event.get("priority"),
    }


def _load_latest_events() -> dict[str, Any]:
    data = load_json(LATEST_EVENTS_FILE, default={})
    if not isinstance(data, dict):
        return {}
    events = data.get("events")
    if not isinstance(events, list):
        data["events"] = []
    return data


def _save_latest_events(
    events: list[dict[str, Any]],
    *,
    stats: dict[str, Any],
    source_counts: dict[str, int],
    links: list[dict[str, Any]],
) -> None:
    save_json(
        LATEST_EVENTS_FILE,
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "version": __version__,
            "stats": stats,
            "source_counts": source_counts,
            "links": links,
            "events": events,
        },
    )


def _mark_cached_event(index: int, status: str) -> dict[str, Any]:
    data = _load_latest_events()
    events = data.get("events", [])
    if index < 0 or index >= len(events):
        raise IndexError(f"Event index {index} is out of range.")
    events[index]["_status"] = status
    save_json(LATEST_EVENTS_FILE, data)
    return events[index]


def _summarize_config(cfg: AppConfig) -> dict[str, Any]:
    return {
        "version": __version__,
        "days_back": cfg.days_back,
        "sources": {
            "discord": cfg.sources.discord,
            "telegram": cfg.sources.telegram,
            "gmail": cfg.sources.gmail,
            "signal": cfg.sources.signal,
            "web": cfg.sources.web,
        },
        "caps": {
            "discord": cfg.caps.discord,
            "telegram": cfg.caps.telegram,
            "gmail": cfg.caps.gmail,
            "signal": cfg.caps.signal,
            "web": cfg.caps.web,
            "total": cfg.caps.total,
            "no_cap": cfg.caps.no_cap,
        },
        "configured": {
            "telegram": cfg.telegram.is_configured,
            "gmail": cfg.gmail.is_configured,
            "llm": cfg.llm.is_configured,
            "ticktick": cfg.ticktick.is_configured,
        },
        "llm": {
            "backend": cfg.llm.backend,
            "model": cfg.llm.display_name,
        },
        "ticktick_project": cfg.ticktick.project,
    }


async def run_event_pipeline(
    *,
    days: int | None = None,
    only: str | list[str] | None = None,
    skip: str | list[str] | None = None,
    include_known: bool = False,
    load_path: str | None = None,
    save_path: str | None = None,
    web_source: str | None = None,
    no_cooldown: bool | None = None,
    skip_analysis: bool | None = None,
    no_cap: bool = False,
    total_cap: int | None = None,
) -> dict[str, Any]:
    """Run the harvest/filter/extract pipeline and cache the extracted events.

    Wraps the shared ``Pipeline`` class with MCP-specific concerns:
    on-disk caching, the include_known filter, and the public event shape.
    """
    cfg = load_config()
    if days is not None:
        if days <= 0:
            raise ValueError("'days' must be greater than zero.")
        cfg.days_back = days
    if no_cooldown is not None:
        cfg.web.no_cooldown = no_cooldown
    if skip_analysis is not None:
        cfg.skip_analyze = skip_analysis
    if no_cap:
        cfg.caps.no_cap = True
    if total_cap is not None:
        if total_cap < -1:
            raise ValueError("'total_cap' must be -1 or greater.")
        cfg.caps.total = total_cap

    no_kwargs = _source_kwargs(cfg, only=only, skip=skip)
    pipeline = Pipeline(cfg)
    warnings = pipeline.validate(no_kwargs=no_kwargs, need_ticktick=False)

    # When the caller scopes to specific sources via ``only``, bypass
    # per-source caps so the requested sources aren't silently trimmed.
    unlimited_caps = _parse_source_filter(only) is not None

    result = await pipeline.run(
        no_kwargs=no_kwargs,
        load_path=load_path,
        save_path=save_path,
        web_source=web_source,
        unlimited_caps=unlimited_caps,
    )

    # MCP exposes a fixed-key per-platform source_counts; the pipeline
    # only counts the canonical platforms (same set), so this is a
    # safe pass-through with a sort for deterministic order.
    source_counts = {p: result.source_counts.get(p, 0) for p in sorted(VALID_PLATFORMS)}

    if result.skipped_analysis or result.no_messages:
        stats = {
            "total_messages": len(result.all_messages),
            "actionable_messages": len(result.actionable),
            "events": 0,
            "new_events": 0,
            "known_events": 0,
            "sources": _enabled_sources(no_kwargs),
            "analysis_skipped": True if result.skipped_analysis else False,
        }
        _save_latest_events(
            [], stats=stats, source_counts=source_counts, links=result.links,
        )
        return {"ok": True, "warnings": warnings, "stats": stats, "events": []}

    if include_known:
        visible_events = result.events
    else:
        visible_events = [ev for ev in result.events if ev.get("_status") == "new"]

    stats = {
        "total_messages": len(result.all_messages),
        "actionable_messages": len(result.actionable),
        "events": len(visible_events),
        "new_events": sum(1 for ev in result.events if ev.get("_status") == "new"),
        "known_events": sum(1 for ev in result.events if ev.get("_status") != "new"),
        "sources": _enabled_sources(no_kwargs),
        "analysis_skipped": False,
        "rejects": {key: len(value) for key, value in result.rejects.items()},
    }
    _save_latest_events(
        visible_events, stats=stats, source_counts=source_counts, links=result.links,
    )

    return {
        "ok": True,
        "warnings": warnings,
        "summary": result.summary,
        "stats": stats,
        "events": [_public_event(ev, i) for i, ev in enumerate(visible_events)],
    }


def build_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    streamable_http_path: str = "/mcp",
    log_level: str = "INFO",
    allow_any_host: bool = False,
):
    """Build and return the FastMCP server instance.

    ``allow_any_host`` disables FastMCP's DNS-rebinding protection. Set this
    when running behind a reverse proxy or tunnel (ngrok, Cloudflare, etc.)
    where the inbound ``Host`` header doesn't match the bound interface.
    Loopback binds default to host-validated mode otherwise.
    """
    try:
        from mcp.server.fastmcp import FastMCP
        from mcp.server.transport_security import TransportSecuritySettings
    except ImportError as exc:  # pragma: no cover - depends on optional runtime install
        raise RuntimeError(
            "The MCP SDK is not installed. Install the project dependencies again "
            "or run: pip install 'mcp[cli]>=1,<2'"
        ) from exc

    transport_security = None
    if allow_any_host:
        # Explicit opt-out of the FastMCP loopback host check.
        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        )

    server = FastMCP(
        "event-harvester",
        instructions=(
            "Harvest events from Discord, Telegram, Gmail, Signal, and web sources. "
            "Use harvest_events first, then approve_event or decline_event by index."
        ),
        host=host,
        port=port,
        streamable_http_path=streamable_http_path,
        json_response=True,
        stateless_http=True,
        log_level=log_level,
        transport_security=transport_security,
    )

    @server.resource(
        "event-harvester://config",
        name="config",
        mime_type="application/json",
    )
    def config_resource() -> str:
        """Current Event Harvester configuration summary."""
        return json.dumps(_summarize_config(load_config()), indent=2)

    @server.resource(
        "event-harvester://latest-events",
        name="latest-events",
        mime_type="application/json",
    )
    def latest_events_resource() -> str:
        """Latest MCP harvest result cached on disk."""
        return json.dumps(_load_latest_events(), indent=2)

    @server.tool()
    def config_summary() -> dict[str, Any]:
        """Return enabled sources, caps, and credential readiness."""
        return _summarize_config(load_config())

    @server.tool()
    async def harvest_events(
        days: int | None = None,
        only: str | None = None,
        skip: str | None = None,
        include_known: bool = False,
        load_path: str | None = None,
        save_path: str | None = None,
        web_source: str | None = None,
        no_cooldown: bool | None = None,
        skip_analysis: bool | None = None,
        no_cap: bool = False,
        total_cap: int | None = None,
    ) -> dict[str, Any]:
        """Harvest messages, extract event candidates, and cache the result.

        Args:
            days: Override DAYS_BACK for this run.
            only: Comma-separated source list, e.g. "discord,gmail".
            skip: Comma-separated source list to exclude.
            include_known: Include previously approved/declined matches.
            load_path: Load raw messages from a JSON file instead of harvesting.
            save_path: Save raw harvested messages to a JSON file.
            web_source: Limit web harvesting to one configured source name.
            no_cooldown: Ignore web-source cooldowns for this run.
            skip_analysis: Return harvested/actionable counts without LLM extraction.
            no_cap: Bypass per-source and total analysis caps.
            total_cap: Override the global analysis cap; -1 means unlimited.
        """
        return await run_event_pipeline(
            days=days,
            only=only,
            skip=skip,
            include_known=include_known,
            load_path=load_path,
            save_path=save_path,
            web_source=web_source,
            no_cooldown=no_cooldown,
            skip_analysis=skip_analysis,
            no_cap=no_cap,
            total_cap=total_cap,
        )

    @server.tool()
    def list_latest_events(status: str = "all") -> dict[str, Any]:
        """List events from the latest MCP harvest cache."""
        wanted = status.strip().lower()
        if wanted not in {"all", "new", "known", "approved", "declined"}:
            return {"ok": False, "error": "status must be all, new, known, approved, or declined"}

        data = _load_latest_events()
        events = data.get("events", [])
        public = [_public_event(event, i) for i, event in enumerate(events)]
        if wanted != "all":
            public = [event for event in public if event.get("status") == wanted]
        return {
            "ok": True,
            "generated_at": data.get("generated_at"),
            "stats": data.get("stats", {}),
            "events": public,
        }

    @server.tool()
    def approve_event(index: int, dry_run: bool = False) -> dict[str, Any]:
        """Approve an event from the latest harvest and sync it to TickTick."""
        try:
            event = _mark_cached_event(index, "approved")
        except IndexError as exc:
            return {"ok": False, "error": str(exc)}

        cfg = load_config()
        if dry_run:
            save_fingerprint(event, status="approved")
            return {
                "ok": True,
                "dry_run": True,
                "event": _public_event(event, index),
                "ticktick": {
                    "created": [event.get("title", "Untitled")],
                    "updated": [],
                    "skipped": [],
                },
            }

        if not cfg.ticktick.is_configured:
            return {
                "ok": False,
                "error": "TickTick is not configured.",
                "event": _public_event(event, index),
            }

        from event_harvester.outputs.ticktick import create_ticktick_tasks, get_ticktick_client

        tt = get_ticktick_client(cfg.ticktick)
        if tt is None:
            return {
                "ok": False,
                "error": "TickTick authentication failed.",
                "event": _public_event(event, index),
            }

        result = create_ticktick_tasks(
            tt,
            [event],
            project_name=cfg.ticktick.project,
            dry_run=False,
        )
        save_fingerprint(event, status="approved")
        return {"ok": True, "event": _public_event(event, index), "ticktick": result}

    @server.tool()
    def decline_event(index: int) -> dict[str, Any]:
        """Decline an event from the latest harvest and save its fingerprint."""
        try:
            event = _mark_cached_event(index, "declined")
        except IndexError as exc:
            return {"ok": False, "error": str(exc)}

        save_fingerprint(event, status="declined")
        return {"ok": True, "event": _public_event(event, index)}

    @server.tool()
    def generate_latest_report(output_path: str = "events_report.md") -> dict[str, Any]:
        """Generate a Markdown report from the latest MCP harvest cache."""
        data = _load_latest_events()
        events = data.get("events", [])
        if not events:
            return {"ok": False, "error": "No cached events. Run harvest_events first."}

        report_path = generate_report(
            validated_events=_events_for_report(events),
            raw_events=[],
            links=data.get("links", []),
            source_counts=data.get("source_counts", {}),
            total_messages=int(data.get("stats", {}).get("total_messages", 0)),
            output_path=output_path,
        )
        return {"ok": True, "path": str(report_path), "events": len(events)}

    @server.prompt()
    def review_latest_events() -> str:
        """Prompt an MCP client to review the cached event candidates."""
        return (
            "Review event-harvester://latest-events. Identify likely real-world events, "
            "call approve_event for events worth adding to TickTick, and call "
            "decline_event for obvious non-events or duplicates."
        )

    # Site-specific automation tools (Grok). Lazy: doesn't spin up a
    # browser until the first grok_* tool is called. Implementation
    # lives in the universal_controller submodule so other projects
    # using uc_browser get the same MCP surface for free.
    try:
        from uc_browser.mcp.grok_tools import register_grok_tools

        register_grok_tools(server)
    except Exception as e:  # pragma: no cover
        logger.warning("Skipped Grok MCP tools (import failed): %s", e)

    return server


async def run_server_async(
    *,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8765,
    path: str = "/mcp",
    log_level: str = "INFO",
    allow_any_host: bool = False,
) -> None:
    """Run the MCP server inside an existing asyncio loop."""
    server = build_server(
        host=host,
        port=port,
        streamable_http_path=path,
        log_level=log_level,
        allow_any_host=allow_any_host,
    )
    if transport == "stdio":
        await server.run_stdio_async()
    elif transport == "streamable-http":
        await server.run_streamable_http_async()
    else:
        raise ValueError(f"Unsupported MCP transport: {transport}")


def run_server(
    *,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8765,
    path: str = "/mcp",
    log_level: str = "INFO",
) -> None:
    """Run the MCP server as a synchronous entry point."""
    server = build_server(
        host=host,
        port=port,
        streamable_http_path=path,
        log_level=log_level,
    )
    server.run(transport=transport)


if __name__ == "__main__":  # pragma: no cover
    run_server()
