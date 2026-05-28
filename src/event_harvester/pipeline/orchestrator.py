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
  * Standard rejects shape (5 keys: classifier/reranker/caps/llm_past/llm_no_events)
  * Optional ProgressReporter integration that is a no-op when the
    reporter is None (so CLI and MCP callers pay nothing for it)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from event_harvester.pipeline.analysis import extract_events_llm
from event_harvester.config import AppConfig, validate_config
from event_harvester.pipeline.event_match import find_acted_fingerprint
from event_harvester.pipeline.harvest import harvest_messages, save_messages
from event_harvester.sources import filter_read_sent
from event_harvester.pipeline.weights import extract_links


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
    return {
        "classifier": [],
        "reranker": [],
        "caps": [],
        "llm_past": [],
        "llm_no_events": [],
    }


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
        # ── Harvest ─────────────────────────────────────────────────────
        await self._checkpoint()
        self._update_stage("harvest")
        cooldown_flag = (
            self.cfg.web.no_cooldown if no_cooldown is None else no_cooldown
        )
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

        if not all_messages:
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

        # ── Filter read/sent (Gmail) ────────────────────────────────────
        await self._checkpoint()
        self._update_stage("filter")
        actionable = filter_read_sent(all_messages)
        self._log(
            f"Filtered: {len(all_messages)} → {len(actionable)} actionable."
        )

        # ── Extract links (cheap; do it before LLM gate) ────────────────
        links = extract_links(actionable)

        # ── Skip-analyze short-circuit ──────────────────────────────────
        if self.cfg.skip_analyze:
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

        # ── Extract events via LLM ──────────────────────────────────────
        await self._checkpoint()
        self._update_stage("extract")
        caps = None if unlimited_caps else self.cfg.caps
        summary, events = extract_events_llm(
            actionable, self.cfg.days_back, self.cfg.llm, caps=caps,
        )
        self._log(f"Extracted {len(events)} event candidate(s).")

        # ── Match fingerprints → tag each event with _status ────────────
        await self._checkpoint()
        self._update_stage("match")
        for ev in events:
            ev["_status"] = self._status_for(ev)
        n_new = sum(1 for ev in events if ev["_status"] == "new")
        n_known = len(events) - n_new
        self._log(
            f"Matched against fingerprint store: "
            f"{n_new} new, {n_known} previously approved/declined."
        )

        # ── Collect rejects from analysis module attributes ─────────────
        from event_harvester.pipeline.analysis import _extract_events_cloud
        rejects = dict(getattr(extract_events_llm, "_last_rejects", {}) or {})
        rejects.setdefault("classifier", [])
        rejects.setdefault("reranker", [])
        rejects.setdefault("caps", [])
        rejects["llm_past"] = list(
            getattr(_extract_events_cloud, "_last_llm_dropped", []) or []
        )
        rejects["llm_no_events"] = list(
            getattr(_extract_events_cloud, "_last_llm_no_events", []) or []
        )

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
