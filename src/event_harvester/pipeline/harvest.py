"""Message harvesting from sources with auto-caching."""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from event_harvester.config import AppConfig
from event_harvester.ui.display import DIM, RESET
from event_harvester.logging_tags import tag_task
from event_harvester.sources import (
    fetch_gmail_messages,
    fetch_web_sources,
    read_discord_messages,
    read_signal_messages,
    read_telegram_messages,
)
from event_harvester.observability.tracing import get_tracer

logger = logging.getLogger("event_harvester")
tracer = get_tracer("event_harvester.pipeline.harvest")

# NOTE: four levels, not three.  pipeline -> event_harvester -> src -> repo.
# This was `.parent.parent.parent`, which lands on <repo>/src/data -- so the
# message cache was written to a stray src/data/ directory that nothing else
# in the codebase knows about, while every other data path (web_utils,
# web_session, ui.server) correctly resolves <repo>/data.
#
# Two consequences, both silent:
#   * the "Delete data/.message_cache.json to force refresh" hint below
#     pointed at a file that did not exist, so following it did nothing and
#     the stale cache kept being served;
#   * `harvest_events(web_source=...)` for a DIFFERENT source would return the
#     previous source's cached messages, which reads as a successful harvest.
#
# Same off-by-one as the classifier models path (see pipeline/classifier.py).
# Prefer deriving from a single anchor over counting .parent calls -- the
# count encodes the layout in a place that does not move when the layout does.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DATA_DIR = _REPO_ROOT / "data"
CACHE_FILE = _DATA_DIR / ".message_cache.json"
CACHE_MAX_AGE = timedelta(hours=4)


_WEB_EVENT_DOMAINS = (
    r"eventbrite|lu\.ma|luma|meetup\.com|partiful"
    r"|facebook\.com/events|calendar\.google"
)


async def harvest_messages(
    cfg: AppConfig,
    *,
    load_path: str | None = None,
    no_discord: bool = False,
    no_telegram: bool = False,
    no_gmail: bool = False,
    no_signal: bool = False,
    no_web: bool = False,
    skip_cache: bool = False,
    web_source: str | None = None,
    web_sources: list[str] | None = None,
    no_cooldown: bool = False,
) -> list[dict]:
    """Fetch messages from sources, with auto-caching.

    Returns the list of message dicts, or an empty list on failure.
    """
    active_sources = sorted(
        s for s, skip in [
            ("discord", no_discord),
            ("telegram", no_telegram),
            ("gmail", no_gmail),
            ("signal", no_signal),
            ("web", no_web),
        ] if not skip
    )

    # ── Load from file ────────────────────────────────────────────────────
    if load_path:
        try:
            messages = json.loads(Path(load_path).read_text())
            logger.info("Loaded %d messages from %s", len(messages), load_path)
            return messages
        except Exception as e:
            logger.error("Failed to load %s: %s", load_path, e)
            return []

    # ── Try cache ─────────────────────────────────────────────────────────
    if not skip_cache and CACHE_FILE.exists():
        try:
            cache_data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            cached_at = datetime.fromisoformat(cache_data["cached_at"])
            cached_sources = sorted(cache_data.get("sources", []))
            if (
                datetime.now(timezone.utc) - cached_at < CACHE_MAX_AGE
                and cached_sources == active_sources
            ):
                messages = cache_data["messages"]
                age_min = int((datetime.now(timezone.utc) - cached_at).total_seconds() / 60)
                print(
                    f"{DIM}Using cached messages ({len(messages)} msgs, {age_min}m old). "
                    f"Delete {CACHE_FILE} to force refresh.{RESET}\n"
                )
                return messages
            else:
                logger.debug("Message cache stale or sources changed, re-fetching.")
        except Exception:
            logger.debug("Message cache invalid, re-fetching.")

    # ── Harvest from sources (concurrent) ─────────────────────────────────
    cutoff = datetime.now(timezone.utc) - timedelta(days=cfg.days_back)

    # Per-source async wrappers. Each wrapper sets a task_tag so log
    # records emitted while it runs get prefixed in the UI handler, and
    # prints the existing banner once at task entry. Each wrapper is
    # wrapped in an OpenTelemetry span ``harvest.<name>``.

    async def _run_discord() -> list[dict]:
        with tag_task("discord"):
            print("[ Discord ]")
            try:
                with tracer.start_as_current_span("harvest.discord") as span:
                    span.set_attribute("source.name", "discord")
                    try:
                        msgs = await asyncio.to_thread(
                            read_discord_messages,
                            cutoff,
                            override_path=cfg.discord.cache_path,
                        )
                        span.set_attribute("message.count", len(msgs or []))
                        return msgs
                    except Exception as e:
                        span.set_attribute("error.message", str(e))
                        span.record_exception(e)
                        raise
            finally:
                print()

    async def _run_telegram() -> list[dict]:
        with tag_task("telegram"):
            print("[ Telegram ]")
            try:
                with tracer.start_as_current_span("harvest.telegram") as span:
                    span.set_attribute("source.name", "telegram")
                    try:
                        msgs = await read_telegram_messages(
                            cutoff,
                            cfg.telegram,
                            channels_allowlist=cfg.telegram_channels,
                            channels_blocklist=cfg.telegram_exclude,
                        )
                        span.set_attribute("message.count", len(msgs or []))
                        return msgs
                    except Exception as e:
                        span.set_attribute("error.message", str(e))
                        span.record_exception(e)
                        raise
            finally:
                print()

    async def _run_gmail() -> list[dict]:
        with tag_task("gmail"):
            print("[ Gmail ]")
            try:
                with tracer.start_as_current_span("harvest.gmail") as span:
                    span.set_attribute("source.name", "gmail")
                    try:
                        msgs = await asyncio.to_thread(
                            fetch_gmail_messages, cfg.gmail, cutoff,
                        )
                        span.set_attribute("message.count", len(msgs or []))
                        return msgs
                    except Exception as e:
                        span.set_attribute("error.message", str(e))
                        span.record_exception(e)
                        raise
            finally:
                print()

    async def _run_signal() -> list[dict]:
        with tag_task("signal"):
            print("[ Signal ]")
            try:
                with tracer.start_as_current_span("harvest.signal") as span:
                    span.set_attribute("source.name", "signal")
                    try:
                        msgs = await asyncio.to_thread(
                            read_signal_messages,
                            cutoff,
                            override_path=cfg.signal.db_path,
                        )
                        span.set_attribute("message.count", len(msgs or []))
                        return msgs
                    except Exception as e:
                        span.set_attribute("error.message", str(e))
                        span.record_exception(e)
                        raise
            finally:
                print()

    async def _run_web() -> list[dict]:
        with tag_task("web"):
            print("[ Web Sources ]")
            try:
                with tracer.start_as_current_span("harvest.web") as span:
                    span.set_attribute("source.name", "web")
                    try:
                        web_kw: dict = {
                            "max_events": cfg.web.max_events,
                            "timeout_ms": cfg.web.timeout_ms,
                            "no_cooldown": no_cooldown,
                            "parallel": cfg.web.parallel,
                            "debug_trace": getattr(cfg.web, "debug_trace", False),
                            "debug_screenshots": getattr(cfg.web, "debug_screenshots", False),
                        }
                        # Merge filter sources: explicit `web_sources` list wins,
                        # then the single-name `web_source` (back-compat shim),
                        # then `cfg.web.selected_names` from env/profile.
                        filter_names: list[str] = []
                        if web_sources:
                            filter_names = list(web_sources)
                        elif web_source:
                            filter_names = [web_source]
                        elif cfg.web.selected_names:
                            filter_names = list(cfg.web.selected_names)
                        if filter_names:
                            from event_harvester.sources.web_fetch import _load_web_sources
                            all_web = _load_web_sources()
                            wanted = {n.lower() for n in filter_names}
                            matched = [
                                s for s in all_web
                                if s.get("name", "").lower() in wanted
                            ]
                            if not matched:
                                names = ", ".join(
                                    s.get("name", s["url"]) for s in all_web
                                )
                                logger.warning(
                                    "Web sources %s not found. Available: %s",
                                    filter_names, names,
                                )
                            else:
                                missing = wanted - {
                                    s.get("name", "").lower() for s in matched
                                }
                                if missing:
                                    logger.warning(
                                        "Profile lists web sources not in %s: %s",
                                        cfg.web.sources_file, sorted(missing),
                                    )
                                web_kw["sources"] = matched
                        msgs = await asyncio.to_thread(fetch_web_sources, **web_kw)
                        span.set_attribute("message.count", len(msgs or []))
                        return msgs
                    except Exception as e:
                        span.set_attribute("error.message", str(e))
                        span.record_exception(e)
                        raise
            finally:
                print()

    # Build the task list in deterministic source order. Native-async
    # sources (telegram, web) get scheduled directly; sync sources get
    # wrapped via asyncio.to_thread inside their _run_* helpers above.
    source_specs: list[tuple[str, bool, "asyncio.coroutines"]] = [
        ("discord", no_discord, _run_discord),
        ("telegram", no_telegram, _run_telegram),
        ("gmail", no_gmail, _run_gmail),
        ("signal", no_signal, _run_signal),
        ("web", no_web, _run_web),
    ]

    # Root "harvest" span. Per-source spans become children of this one
    # by virtue of being started inside the same task contexts.
    with tracer.start_as_current_span("harvest") as _root_span:
        _root_span.set_attribute("days", cfg.days_back)
        _root_span.set_attribute("source.count", len(active_sources))
        # `cap.limit` is best-effort — caps live on cfg.caps.total when
        # configured; -1 means "no cap".
        try:
            _root_span.set_attribute(
                "cap.limit", int(getattr(cfg.caps, "total", -1)),
            )
        except Exception:
            pass

        tasks: list[asyncio.Task] = []
        task_names: list[str] = []
        for name, skip, runner in source_specs:
            if skip:
                continue
            task = asyncio.create_task(runner(), name=f"harvest.{name}")
            tasks.append(task)
            task_names.append(name)

        gathered = await asyncio.gather(*tasks, return_exceptions=True)

        # Map results by source name so we can re-assemble in the canonical
        # order regardless of which task finished first.
        results: dict[str, list[dict]] = {}
        for name, outcome in zip(task_names, gathered):
            if isinstance(outcome, BaseException):
                logger.error("%s source failed: %s", name, outcome)
                results[name] = []
            else:
                results[name] = outcome or []

        messages: list[dict] = []
        for name, _, _ in source_specs:
            if name in results:
                messages.extend(results[name])
        _root_span.set_attribute("message.count", len(messages))

        # ── Auto-save to cache (decimate web content to reduce cache size) ──
        if messages:
            try:
                from event_harvester.sources.web_fetch import _decimate_text

                cache_msgs = []
                for m in messages:
                    if m.get("platform") == "web" and len(m.get("content", "")) > 2000:
                        m = {**m, "content": _decimate_text(m["content"])}
                    cache_msgs.append(m)

                CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
                CACHE_FILE.write_text(
                    json.dumps({
                        "cached_at": datetime.now(timezone.utc).isoformat(),
                        "sources": active_sources,
                        "messages": cache_msgs,
                    }, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception as e:
                logger.debug("Failed to write message cache: %s", e)

    return messages


def save_messages(messages: list[dict], path: str) -> None:
    """Save messages to JSON file."""
    Path(path).write_text(
        json.dumps(messages, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Raw messages saved -> {path}\n")
