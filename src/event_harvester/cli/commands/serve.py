"""Serve command — launch the Flet events browser.

The Flet UI starts immediately. The pipeline (harvest → filter → extract →
match) runs as a background task inside the Flet event loop and reports
progress back into the UI's progress panel. When extraction completes the
UI swaps from the progress view to the events review view.
"""

import asyncio
import logging
from pathlib import Path

from event_harvester.analysis import extract_events_llm
from event_harvester.cli.parse_helpers import resolve_platforms
from event_harvester.event_match import find_acted_fingerprint
from event_harvester.profiles import apply_profile_by_name

logger = logging.getLogger("event_harvester")


def _latest_runs_by_source(
    traces_root: Path = Path("data/.web_traces"),
) -> list[tuple[str, str]]:
    """Return [(source_name, run_id), ...] for each source's most recent JSONL trace.

    run_id is the JSONL filename stem (e.g. '20260514T132050_8a6e').
    Sources with no JSONL files are skipped.
    """
    out: list[tuple[str, str]] = []
    if not traces_root.exists():
        return out
    for source_dir in sorted(traces_root.iterdir()):
        if not source_dir.is_dir():
            continue
        jsonl_files = sorted(source_dir.glob("*.jsonl"), reverse=True)
        if jsonl_files:
            out.append((source_dir.name, jsonl_files[0].stem))
    return out


async def serve_cmd(args, cfg) -> int:
    # Seed cfg from CLI args once. After that, the UI mutates cfg for
    # subsequent runs (Re-run button reads the current cfg state).
    if args.days is not None:
        cfg.days_back = args.days
    if getattr(args, "only", None) is not None or getattr(args, "skip", None) is not None:
        no_kwargs = resolve_platforms(args.only, args.skip)
        cfg.sources.discord = not no_kwargs["no_discord"]
        cfg.sources.telegram = not no_kwargs["no_telegram"]
        cfg.sources.gmail = not no_kwargs["no_gmail"]
        cfg.sources.signal = not no_kwargs["no_signal"]
        cfg.sources.web = not no_kwargs["no_web"]

    # Apply profile (if any) before launching the UI so the dropdown
    # starts on the right entry and caps/web_sources reflect the choice.
    if getattr(args, "profile", None):
        if not apply_profile_by_name(cfg, args.profile):
            return 2

    if getattr(args, "debug_web_trace", False):
        cfg.web.debug_trace = True
    if getattr(args, "debug_web_screenshots", False):
        cfg.web.debug_screenshots = True
        cfg.web.debug_trace = True  # screenshots imply trace

    async def run_pipeline(reporter, app) -> None:
        from event_harvester.harvest import harvest_messages
        from event_harvester.sources import filter_read_sent
        from event_harvester.analysis import _extract_events_cloud

        platform_kwargs = cfg.sources.to_no_kwargs()

        await reporter.checkpoint()
        reporter.update_stage("harvest")
        all_messages = await harvest_messages(
            cfg, no_cooldown=cfg.web.no_cooldown, **platform_kwargs,
        )
        reporter.log(f"Harvested {len(all_messages)} message(s).")
        if not all_messages:
            reporter.update_stage("done", "No messages found")
            app.set_events([], rejects={}, stats={"total_messages": 0})
            return

        await reporter.checkpoint()
        reporter.update_stage("filter")
        actionable = filter_read_sent(all_messages)
        reporter.log(
            f"Filtered: {len(all_messages)} → {len(actionable)} actionable."
        )

        if cfg.skip_analyze:
            reporter.update_stage(
                "done",
                f"Skipped LLM analysis — {len(actionable)} actionable message(s) harvested",
            )
            reporter.log("Skip analyze: returning without extracting events.")
            app.set_events(
                [],
                rejects={},
                stats={
                    "total_messages": len(all_messages),
                    "actionable": len(actionable),
                    "events": 0,
                },
            )
            return

        await reporter.checkpoint()
        reporter.update_stage("extract")
        summary, events = extract_events_llm(
            actionable, cfg.days_back, cfg.llm, caps=cfg.caps,
        )
        reporter.log(f"Extracted {len(events)} event candidate(s).")

        await reporter.checkpoint()
        reporter.update_stage("match")
        new_events = [ev for ev in events if not find_acted_fingerprint(ev)]
        n_acted = len(events) - len(new_events)
        reporter.log(
            f"Matched against fingerprint store: "
            f"{len(new_events)} new, {n_acted} previously approved/declined."
        )

        rejects = getattr(extract_events_llm, "_last_rejects", {}) or {}
        rejects = dict(rejects)
        rejects["llm_past"] = getattr(_extract_events_cloud, "_last_llm_dropped", []) or []
        rejects["llm_no_events"] = getattr(_extract_events_cloud, "_last_llm_no_events", []) or []

        stats = {
            "total_messages": len(all_messages),
            "actionable": len(actionable),
            "events": len(new_events),
        }

        # Quality check stage — only when screenshots are enabled AND an
        # LLM is configured. Score the most recent run for each web source
        # that produced a trace this pipeline run.
        has_llm = bool(
            getattr(cfg.llm, "api_key", "")
            or getattr(cfg.llm, "vision_model", "")
        )
        if getattr(cfg.web, "debug_screenshots", False) and has_llm:
            await reporter.checkpoint()
            reporter.update_stage("quality_check")
            # Lazy import so the scorer (and its litellm dependency) isn't
            # paid for when screenshots are disabled.
            from event_harvester.nav_quality import NavQualityScorer

            NavQualityScorer.reset_counter()
            scorer = NavQualityScorer()
            latest_runs = _latest_runs_by_source()
            reporter.log(
                f"Quality check: scoring {len(latest_runs)} source(s)."
            )
            for source_name, run_id in latest_runs:
                try:
                    q = await asyncio.to_thread(
                        scorer.score_source, source_name, run_id, cfg,
                    )
                except Exception as ex:  # never fail the pipeline on QC
                    logger.warning(
                        "quality_check: scoring %s/%s raised %s",
                        source_name, run_id, ex,
                    )
                    continue
                if q is None:
                    continue
                if q.classification == "content":
                    emoji = "\U0001F7E2"  # green circle
                elif q.classification in {"empty", "cookie_banner", "unknown"}:
                    emoji = "\U0001F7E1"  # yellow circle
                else:
                    emoji = "\U0001F534"  # red circle
                reporter.log(
                    f"  {emoji} {source_name}: {q.classification} "
                    f"(score {q.nav_score})"
                )

        reporter.update_stage("done", f"Pipeline complete — {len(new_events)} new event(s)")
        app.set_events(new_events, rejects=rejects, stats=stats)

    import flet as ft

    from event_harvester.app import EventsApp

    app = EventsApp(cfg, pipeline_runner=run_pipeline)
    await ft.app_async(target=app.main, view=ft.AppView.WEB_BROWSER, port=8550)
    return 0
