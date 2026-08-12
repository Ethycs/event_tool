"""Single source of truth for the harvest → filter → extract → match pipeline.

Before this module, three entry points (CLI `harvest`, Flet `serve`, MCP
`harvest_events`) each re-implemented ~100 lines of the same harvest →
filter → extract → match sequence with subtle drift between them. This
module collapses the shared core into one ``Pipeline`` class.

What stays with each caller (true divergence, not duplication):
  * harvest.py: terminal printing, TickTick sync, Markdown/Obsidian reports
  * serve.py:   ProgressReporter wiring, quality_check stage, set_events()
  * mcp_server: on-disk event cache, include_known filter, public_event shape

What this class owns (the duplicated middle):
  * harvest_messages → filter_read_sent → extract_links → extract_events_llm
  * fingerprint-based ``_status`` tagging on each event
  * Standard rejects shape (6 keys: classifier/reranker/caps/llm_past/llm_no_events/
    llm_errors — the last added 2026-08-12; it is not a rejection but an outage,
    and reading it as "no events" destroyed a harvest)
  * Optional ProgressReporter integration that is a no-op when the
    reporter is None (so CLI and MCP callers pay nothing for it)
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any, Protocol

from event_harvester.config import AppConfig, validate_config
from event_harvester.observability.metrics import count as _count
from event_harvester.observability.metrics import record as _record
from event_harvester.observability.tracing import get_tracer
from event_harvester.pipeline.analysis import REJECT_BUCKETS, extract_events_llm
from event_harvester.pipeline.event_match import find_acted_fingerprint
from event_harvester.pipeline.harvest import harvest_messages, save_messages
from event_harvester.pipeline.weights import extract_links
from event_harvester.sources import filter_read_sent

tracer = get_tracer("event_harvester.pipeline.orchestrator")


class _ReporterProtocol(Protocol):
    """The subset of ProgressReporter that Pipeline uses.

    Kept as a Protocol to avoid an import cycle with app.py and to keep
    Pipeline testable without instantiating Flet.
    """

    def update_stage(self, stage_id: str, label: str | None = None) -> None: ...
    def log(self, line: str, color: Any = None) -> None: ...
    async def checkpoint(self) -> None: ...


@dataclass
class PipelineResult:
    """Everything a caller might need after the pipeline runs.

    Empty when there were no messages or skip_analyze is set; callers
    check ``no_messages`` and ``skipped_analysis`` to decide what to do.
    """
    all_messages: list[dict]
    actionable: list[dict]
    events: list[dict]
    summary: str
    rejects: dict[str, list]
    source_counts: dict[str, int]
    links: list[dict]
    skipped_analysis: bool = False
    no_messages: bool = False


_ALL_PLATFORMS: tuple[str, ...] = ("discord", "telegram", "gmail", "signal", "web")


def _empty_rejects() -> dict[str, list]:
    """The 5-key reject shape, sourced from `analysis.REJECT_BUCKETS`.

    Kept as a thin wrapper rather than a second literal so the bucket list
    cannot drift between the producer and this consumer.
    """
    return {bucket: [] for bucket in REJECT_BUCKETS}


class Pipeline:
    """Run the harvest pipeline once with an optional progress reporter.

    Typical use:

        result = await Pipeline(cfg, reporter=reporter).run(
            no_kwargs=cfg.sources.to_no_kwargs(),
        )

    The reporter is optional. When None, all stage/log/checkpoint calls
    are no-ops so CLI and MCP callers don't pay any UI cost.
    """

    def __init__(self, cfg: AppConfig, *, reporter: _ReporterProtocol | None = None):
        self.cfg = cfg
        self.reporter = reporter

    # ── Public API ──────────────────────────────────────────────────────

    def validate(
        self,
        *,
        no_kwargs: dict[str, bool],
        need_ticktick: bool = False,
    ) -> list[str]:
        """Run config validation against the active source set.

        Returns warning strings. Callers decide whether to log/print them.
        """
        return validate_config(
            self.cfg,
            need_telegram=not no_kwargs["no_telegram"],
            need_discord=not no_kwargs["no_discord"],
            need_gmail=not no_kwargs["no_gmail"],
            need_analysis=not self.cfg.skip_analyze,
            need_ticktick=need_ticktick,
        )

    async def run(
        self,
        *,
        no_kwargs: dict[str, bool],
        load_path: str | None = None,
        save_path: str | None = None,
        web_source: str | None = None,
        web_sources: list[str] | None = None,
        no_cooldown: bool | None = None,
        unlimited_caps: bool = False,
    ) -> PipelineResult:
        """Run harvest → filter → extract → match. Returns a PipelineResult.

        ``unlimited_caps``: bypass per-source caps in LLM extraction. Used
        by the CLI when ``--only`` is set, since the user has already
        scoped the run and caps would silently drop their target source.

        ``no_cooldown``: ``None`` means "respect cfg.web.no_cooldown";
        callers that want to override per-run pass an explicit bool.
        """
        with tracer.start_as_current_span("pipeline.run") as run_span:
            run_span.set_attribute("pipeline.days_back", self.cfg.days_back)
            run_span.set_attribute("pipeline.unlimited_caps", unlimited_caps)
            run_span.set_attribute("pipeline.skip_analyze", bool(self.cfg.skip_analyze))
            run_span.set_attribute("pipeline.replayed_from_file", bool(load_path))
            for plat, disabled in no_kwargs.items():
                run_span.set_attribute(f"pipeline.{plat}", not disabled)

            # ── Harvest ─────────────────────────────────────────────────
            await self._checkpoint()
            cooldown_flag = (
                self.cfg.web.no_cooldown if no_cooldown is None else no_cooldown
            )
            with self._stage("harvest") as span:
                span.set_attribute("harvest.no_cooldown", bool(cooldown_flag))
                all_messages = await harvest_messages(
                    self.cfg,
                    load_path=load_path,
                    skip_cache=bool(save_path),
                    web_source=web_source,
                    web_sources=web_sources,
                    no_cooldown=cooldown_flag,
                    **no_kwargs,
                )
                if save_path:
                    save_messages(all_messages, save_path)

                source_counts = {
                    plat: sum(1 for m in all_messages if m.get("platform") == plat)
                    for plat in _ALL_PLATFORMS
                }
                span.set_attribute("message.count", len(all_messages))
                for plat, n in source_counts.items():
                    span.set_attribute(f"message.count.{plat}", n)

            if not all_messages:
                run_span.set_attribute("pipeline.outcome", "no_messages")
                return PipelineResult(
                    all_messages=[],
                    actionable=[],
                    events=[],
                    summary="",
                    rejects=_empty_rejects(),
                    source_counts=source_counts,
                    links=[],
                    no_messages=True,
                )

            # ── Filter read/sent (Gmail) ────────────────────────────────
            await self._checkpoint()
            with self._stage("filter") as span:
                # drop_read is False by default for harvesting: read mail was
                # 42% of the mailbox measured 2026-08-10, and an invite you
                # opened is still an invite. Your own sent mail is still cut.
                actionable = filter_read_sent(
                    all_messages, drop_read=self.cfg.gmail.drop_read,
                )
                span.set_attribute("message.in", len(all_messages))
                span.set_attribute("message.actionable", len(actionable))
                span.set_attribute("message.dropped", len(all_messages) - len(actionable))
                self._log(
                    f"Filtered: {len(all_messages)} → {len(actionable)} actionable."
                )

                # Extract links (cheap; do it before LLM gate)
                links = extract_links(actionable)
                span.set_attribute("link.count", len(links))

            # ── Skip-analyze short-circuit ──────────────────────────────
            if self.cfg.skip_analyze:
                run_span.set_attribute("pipeline.outcome", "skipped_analysis")
                return PipelineResult(
                    all_messages=all_messages,
                    actionable=actionable,
                    events=[],
                    summary="",
                    rejects=_empty_rejects(),
                    source_counts=source_counts,
                    links=links,
                    skipped_analysis=True,
                )

            # ── Extract events via LLM ──────────────────────────────────
            await self._checkpoint()
            with self._stage("extract") as span:
                caps = None if unlimited_caps else self.cfg.caps
                span.set_attribute("extract.capped", caps is not None)
                span.set_attribute("extract.message.in", len(actionable))
                # `extract_events_llm` is synchronous and the slowest stage in
                # the pipeline (classifier + cross-encoder + N batched LLM
                # calls). Called directly it blocked this coroutine's event
                # loop for the whole stage, which froze the Flet UI and meant
                # Pause/Stop could not be delivered until it returned -- the
                # UI reported "Stopped" while the run was still going.
                extraction = await asyncio.to_thread(
                    extract_events_llm,
                    actionable, self.cfg.days_back, self.cfg.llm, caps=caps,
                )
                summary, events = extraction.summary, extraction.events
                span.set_attribute("event.count", len(events))
                self._log(f"Extracted {len(events)} event candidate(s).")

            # ── Match fingerprints → tag each event with _status ────────
            await self._checkpoint()
            with self._stage("match") as span:
                for ev in events:
                    ev["_status"] = self._status_for(ev)
                n_new = sum(1 for ev in events if ev["_status"] == "new")
                n_known = len(events) - n_new
                span.set_attribute("event.new", n_new)
                span.set_attribute("event.known", n_known)
                self._log(
                    f"Matched against fingerprint store: "
                    f"{n_new} new, {n_known} previously approved/declined."
                )

            # Rejects come back with the result, from whichever backend
            # actually ran. They used to be read off module-level function
            # attributes, which is why a `backend="local"` run reported the
            # cloud path's leftovers.
            rejects = dict(extraction.rejects)
            for bucket in REJECT_BUCKETS:
                rejects.setdefault(bucket, [])
            # Reject counts per stage are the pipeline's actual yield curve;
            # they were only ever visible in the UI's debug panels.
            for bucket, items in rejects.items():
                run_span.set_attribute(f"reject.{bucket}", len(items))

            run_span.set_attribute("pipeline.outcome", "ok")
            run_span.set_attribute("event.count", len(events))
            return PipelineResult(
                all_messages=all_messages,
                actionable=actionable,
                events=events,
                summary=summary,
                rejects=rejects,
                source_counts=source_counts,
                links=links,
            )

    # ── Reporter helpers (no-op when reporter is None) ──────────────────

    def _update_stage(self, stage_id: str, label: str | None = None) -> None:
        if self.reporter is not None:
            self.reporter.update_stage(stage_id, label)

    @contextlib.contextmanager
    def _stage(self, stage_id: str, label: str | None = None):
        """Enter a pipeline stage: tell the reporter, open a span, time it.

        These were already the same event expressed twice -- the UI got
        ``_update_stage`` and the trace got nothing. Binding them here means a
        stage cannot be added to one and forgotten in the other, and the same
        argument extends to the stage-duration metric: "which stage is slow"
        is a question about the distribution across runs, not about one span.
        """
        import time as _time

        self._update_stage(stage_id, label)
        t0 = _time.monotonic()
        failed = False
        with tracer.start_as_current_span(f"pipeline.{stage_id}") as span:
            span.set_attribute("pipeline.stage", stage_id)
            try:
                yield span
            except Exception:
                failed = True
                raise
            finally:
                elapsed_ms = int((_time.monotonic() - t0) * 1000)
                span.set_attribute("pipeline.stage_duration_ms", elapsed_ms)
                _record("pipeline.stage.duration", elapsed_ms, unit="ms",
                        stage=stage_id, ok=not failed,
                        description="Wall-clock duration of one pipeline stage")
                _count("pipeline.stages", stage=stage_id, ok=not failed,
                       description="Pipeline stages entered")

    def _log(self, line: str) -> None:
        if self.reporter is not None:
            self.reporter.log(line)

    async def _checkpoint(self) -> None:
        if self.reporter is not None:
            await self.reporter.checkpoint()

    # ── Fingerprint status (mirrors mcp_server._event_status) ───────────

    @staticmethod
    def _status_for(event: dict) -> str:
        existing = event.get("_status")
        if existing:
            return str(existing)
        fp = find_acted_fingerprint(event)
        if fp is None:
            return "new"
        return fp.get("status") or "new"
