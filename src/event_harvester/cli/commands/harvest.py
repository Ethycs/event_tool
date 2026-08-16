"""Harvest command — the main pipeline.

Wires argparse args to the shared ``Pipeline`` class. Everything specific
to the CLI lives here: terminal printing, TickTick sync, Markdown /
Obsidian reports.
"""

import logging
from collections import defaultdict

from event_harvester.cli.parse_helpers import apply_caps_to_config, resolve_platforms
from event_harvester.outputs.report import generate_report
from event_harvester.outputs.ticktick import create_ticktick_tasks, get_ticktick_client
from event_harvester.pipeline import Pipeline
from event_harvester.profiles import apply_profile_by_name as _apply_profile_by_name
from event_harvester.ui.display import (
    BOLD,
    DIM,
    GREEN,
    RESET,
    YELLOW,
    print_links,
    print_message,
)

logger = logging.getLogger("event_harvester")

W = 64


async def harvest_cmd(args, cfg) -> int:
    """Run the harvest pipeline. Returns an exit code."""
    # Apply CLI overrides to config
    if args.days is not None:
        cfg.days_back = args.days

    # Apply profile FIRST so explicit --cap / --web-source flags below win.
    if getattr(args, "profile", None):
        if not _apply_profile_by_name(cfg, args.profile):
            return 2

    # Caps default to unlimited so the scheduled/site path collects everything
    # and the page filters it. An interactive run wants a bounded bill, so the
    # CLI restores the old ceilings — but only where nothing else set them, so
    # a profile (applied above), a CAP_* env var or a /admin overlay still win.
    cfg.caps.apply_cli_defaults()
    apply_caps_to_config(cfg, args.cap)
    if args.group_by_source:
        cfg.caps.group_by_source = True

    if getattr(args, "debug_web_trace", False):
        cfg.web.debug_trace = True
    if getattr(args, "debug_web_screenshots", False):
        cfg.web.debug_screenshots = True
        cfg.web.debug_trace = True  # screenshots imply trace

    # ``no_analyze`` is a per-run override, not a permanent config field;
    # mirror it onto cfg.skip_analyze so Pipeline sees it consistently.
    if args.no_analyze:
        cfg.skip_analyze = True

    platform_kwargs = resolve_platforms(args.only, args.skip)
    pipeline = Pipeline(cfg)
    warnings = pipeline.validate(
        no_kwargs=platform_kwargs,
        need_ticktick=not args.no_sync and not args.dry_run,
    )
    for w in warnings:
        logger.warning(w)

    print(f"\n{'=' * W}")
    print(f"  Event Harvester - last {cfg.days_back} day(s)")
    print(f"{'=' * W}\n")

    # When --only is used, the user has explicitly scoped to specific
    # sources — don't apply per-source caps that would silently drop
    # events. Caps exist to prevent noisy sources from crowding out
    # quiet ones in the default mixed-source pipeline; that concern
    # doesn't apply when --only is set.
    if args.only is not None:
        logger.info("--only set; ignoring per-source caps")

    result = await pipeline.run(
        no_kwargs=platform_kwargs,
        load_path=args.load,
        save_path=args.save,
        web_source=getattr(args, "web_source", None),
        no_cooldown=getattr(args, "no_cooldown", False),
        unlimited_caps=args.only is not None,
    )

    if result.no_messages:
        print("No messages found. Check credentials / cache and try again.")
        for w in result.source_warnings:
            print(f"  {YELLOW}⚠ {w}{RESET}")
        return 0

    # ── Print harvested messages + per-platform counts ──────────────────
    for msg in sorted(result.all_messages, key=lambda m: m["timestamp"]):
        print_message(msg)

    sc = result.source_counts
    counts = (
        f"Discord: {sc['discord']}, Telegram: {sc['telegram']}, "
        f"Gmail: {sc['gmail']}, Signal: {sc['signal']}"
    )
    if sc["web"]:
        counts += f", Web: {sc['web']}"
    print(f"{DIM}Total: {len(result.all_messages)}  ({counts}){RESET}")
    for w in result.source_warnings:
        print(f"  {YELLOW}⚠ {w}{RESET}")
    print()

    n_filtered = len(result.all_messages) - len(result.actionable)
    if n_filtered:
        print(
            f"{DIM}Filtered {n_filtered} read/sent messages, "
            f"{len(result.actionable)} remain for analysis.{RESET}\n"
        )

    print_links(result.links)

    # ── Recruiter grading (convenience integration) ─────────────────────
    grades = []
    if args.grade_recruiters:
        from event_harvester.cli.commands.recruiters import _run_recruiter_grading
        grades = _run_recruiter_grading(result.all_messages, cfg, args)

    if args.obsidian and cfg.obsidian_recruiters_dir and grades:
        from event_harvester.outputs.obsidian import write_recruiter_report

        path = write_recruiter_report(grades, cfg.obsidian_recruiters_dir)
        print(f"Obsidian recruiters -> {path}\n")

    # ── LLM event extraction ────────────────────────────────────────────
    if result.skipped_analysis:
        return 0

    print(f"{'=' * W}")
    print("[ LLM - extracting events ]")
    print(f"{'=' * W}\n")

    if getattr(args, "show_rejects", False):
        _print_rejects(result.rejects)

    if not result.events:
        print("No events extracted.")
        return 0

    _print_events(result.events, cfg)

    # ── Reports ─────────────────────────────────────────────────────────
    validated_events = _events_for_report(result.events)

    if args.report:
        # Format follows the extension, so `--report out.md` keeps working for
        # anyone who wants the deprecated Markdown and nobody has to learn a
        # second flag. Default is HTML.
        if str(args.report).lower().endswith((".md", ".markdown")):
            logger.warning(
                "Markdown reports are deprecated — use a .html path for the "
                "self-contained mobile view.",
            )
            report_path = generate_report(
                validated_events=validated_events,
                raw_events=[],
                links=result.links,
                source_counts=result.source_counts,
                total_messages=len(result.all_messages),
                output_path=args.report,
            )
        else:
            from event_harvester.outputs.html_report import generate_html_report

            report_path = generate_html_report(
                validated_events=validated_events,
                raw_events=[],
                source_counts=result.source_counts,
                total_messages=len(result.all_messages),
                output_path=args.report,
            )
        print(f"\nReport saved -> {report_path}\n")

    if args.obsidian and cfg.obsidian_events_dir:
        from event_harvester.outputs.obsidian import write_events_report

        path = write_events_report(
            validated_events=validated_events,
            raw_events=[],
            links=result.links,
            source_counts=result.source_counts,
            total_messages=len(result.all_messages),
            output_dir=cfg.obsidian_events_dir,
        )
        print(f"Obsidian events -> {path}\n")

    # ── Gmail label ─────────────────────────────────────────────────────
    # Deliberately before the TickTick block: that block returns early when
    # TickTick is unconfigured, and labelling your own mail should not depend
    # on a third-party task service being set up.
    _label_event_mail(
        cfg, result.events, dry_run=args.dry_run, messages=result.all_messages,
    )

    # ── TickTick sync ───────────────────────────────────────────────────
    if args.no_sync:
        return 0

    print(f"\n{'=' * W}")
    mode_label = "[ TickTick - dry run ]" if args.dry_run else "[ TickTick - syncing events ]"
    print(mode_label)
    print(f"{'=' * W}\n")

    tt = get_ticktick_client(cfg.ticktick)
    if tt is None:
        return 1

    sync_result = create_ticktick_tasks(
        tt, result.events, project_name=cfg.ticktick.project, dry_run=args.dry_run,
    )

    n_created = len(sync_result["created"])
    n_updated = len(sync_result["updated"])
    n_skipped = len(sync_result["skipped"])
    print(
        f"\n{BOLD}Summary:{RESET} "
        f"{GREEN}{n_created} created{RESET}, "
        f"{n_updated} updated, "
        f"{DIM}{n_skipped} skipped{RESET}\n"
    )
    return 0


# ── Helpers ──────────────────────────────────────────────────────────


def _label_event_mail(
    cfg, events: list[dict], *, dry_run: bool, messages: list[dict] | None = None,
) -> None:
    """Tag the mail that produced an event with `cfg.gmail.events_label`.

    Additive: it adds a label, never removes one and never moves a message
    out of the inbox, so re-running is a no-op rather than a second edit.

    `--dry-run` prints the actual event→subject pairings and writes nothing.
    It prints the pairings rather than a count on purpose: this is the one
    sink that edits a mailbox, and "would tag 16 messages" is not something a
    reader can check, whereas a wrong pairing is obvious on sight.
    """
    label = (getattr(cfg.gmail, "events_label", "") or "").strip()
    if not label or not events:
        return

    ids = {
        mid
        for ev in events
        for mid in (ev.get("_source_ids") or [])
    }
    unlinked = sum(1 for ev in events if not ev.get("_source_ids"))
    if not ids:
        return

    if dry_run:
        subjects = {
            m["id"]: (m.get("content") or "").partition("\n")[0]
            for m in (messages or [])
        }
        print(f"{DIM}Gmail label '{label}': would tag {len(ids)} message(s) "
              f"[dry run]{RESET}")
        for ev in events:
            for mid in ev.get("_source_ids") or []:
                subj = subjects.get(mid, mid)
                print(f"{DIM}    {(ev.get('title') or '?')[:44]:<46} <- {subj[:60]}{RESET}")
        print()
        return

    from event_harvester.sources import gmail as gmail_src

    n = gmail_src.label_event_messages(cfg.gmail, events, name=label)
    note = f", {unlinked} event(s) had no resolved source" if unlinked else ""
    print(f"{GREEN}Gmail label '{label}':{RESET} tagged {n} message(s){note}\n")


def _source_key(event: dict) -> str:
    """Extract a stable group key from an event's source field.

    Source format from the LLM is typically '@author in #channel'.
    The channel name is the most useful grouping key; fall back to
    author or 'unknown'.
    """
    src = event.get("source", "") or "unknown"
    if " in " in src:
        channel = src.split(" in ", 1)[1].strip().lstrip("#")
        return channel or "unknown"
    return src.lstrip("@") or "unknown"


def _print_events(events: list[dict], cfg) -> None:
    """Print events grouped by source, sorted by date (soonest first).

    Events without parseable dates are listed under a TBD section
    at the bottom of each source group.
    """
    from event_harvester.pipeline.event_match import _normalize_date, find_acted_fingerprint

    def _print_event(idx: int, t: dict) -> None:
        title = t.get("title") or "Untitled"
        date_str = t.get("date") or ""
        time_str = t.get("time") or ""
        location = t.get("location") or ""
        source = t.get("source") or ""
        notes = t.get("notes") or ""

        fp = find_acted_fingerprint(t)
        status = (fp.get("status") if fp else None) or "new"

        print(f"  [{idx}] {BOLD}{title}{RESET} {DIM}({status}){RESET}")
        if date_str:
            print(f"     date: {date_str}")
        if time_str:
            print(f"     time: {time_str}")
        if location:
            print(f"     location: {location}")
        if notes:
            print(f"     {DIM}details: {notes}{RESET}")
        if source:
            print(f"     {DIM}source: {source}{RESET}")
        print()

    # Bucket events by source group
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in events:
        groups[_source_key(t)].append(t)

    # Sort each bucket: dated events first (ascending), TBD at the bottom
    for key in groups:
        dated, tbd = [], []
        for t in groups[key]:
            iso = _normalize_date(t.get("date"))
            if iso:
                dated.append((iso, t))
            else:
                tbd.append(t)
        dated.sort(key=lambda pair: pair[0])
        groups[key] = [t for _, t in dated] + tbd

    print(f"\n{BOLD}Events ({len(events)}) grouped by source, sorted by date{RESET}")
    idx = 1
    for source_key in sorted(groups.keys()):
        bucket = groups[source_key]
        print(f"\n{BOLD}── {source_key} ({len(bucket)}) ──{RESET}")
        for t in bucket:
            _print_event(idx, t)
            idx += 1


def _events_for_report(events: list[dict]) -> list[dict]:
    """Convert LLM-extracted events into the report.py / obsidian.py shape."""
    out = []
    for t in events:
        source = t.get("source", "")
        author = source.split(" in ")[0].strip().lstrip("@") if " in " in source else ""
        channel = source.split(" in ")[1].strip().lstrip("#") if " in " in source else ""

        out.append({
            "title": t.get("title", "Untitled"),
            "date": t.get("date"),
            "time": t.get("time"),
            "location": t.get("location"),
            "link": t.get("link"),
            "details": t.get("notes", ""),
            "score": t.get("priority", 3),
            "source": source,
            "author": author,
            "channel": channel,
        })
    return out


def _print_rejects(rejects: dict[str, list]) -> None:
    """Print reject summary to terminal and write full details to INI file."""
    from event_harvester.utils import data_path

    stage_labels = {
        "classifier": "Classifier",
        "reranker": "Reranker",
        "caps": "Per-source caps",
        "llm_past": "LLM (past events)",
        "llm_no_events": "LLM (no events extracted)",
        # Not a rejection at all — these never reached the model. Named
        # separately because reading them as "no events" is what turned a
        # billing outage into a silent zero-event harvest on 2026-08-12.
        "llm_errors": "LLM UNREACHABLE (never asked)",
    }
    stages = ("classifier", "reranker", "caps", "llm_past", "llm_no_events",
              "llm_errors")

    total_rejected = sum(len(rejects.get(s, [])) for s in stages)
    if not total_rejected:
        print(f"\n{DIM}No messages rejected (all passed through pipeline).{RESET}\n")
        return

    # Terminal summary
    for stage in stages:
        items = rejects.get(stage, [])
        if items:
            print(f"  {stage_labels[stage]}: {DIM}{len(items)} rejected{RESET}")

    # Write INI file
    out_path = data_path("rejects.ini")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    idx = 0

    # Message-level rejects (classifier, reranker, caps, llm_no_events, llm_errors)
    for stage in ("classifier", "reranker", "caps", "llm_no_events", "llm_errors"):
        msgs = rejects.get(stage, [])
        if not msgs:
            continue
        lines.append(f"; ── Rejected by {stage_labels[stage]} ({len(msgs)}) ──")
        lines.append("")
        for m in msgs:
            idx += 1
            content = m.get("content", "").replace("\n", " | ")[:300]
            lines.append(f"[Reject.{idx}]")
            lines.append(f"stage = {stage}")
            lines.append(f"platform = {m.get('platform', '?')}")
            lines.append(f"channel = {m.get('channel', '?')}")
            lines.append(f"author = {m.get('author', '?')}")
            lines.append(f"timestamp = {m.get('timestamp', '')[:16]}")
            lines.append(f"content = {content}")
            lines.append("")

    # Event-level rejects (llm_past — events extracted then dropped)
    past_events = rejects.get("llm_past", [])
    if past_events:
        lines.append(f"; ── Rejected by {stage_labels['llm_past']} ({len(past_events)}) ──")
        lines.append("")
        for ev in past_events:
            idx += 1
            lines.append(f"[Reject.{idx}]")
            lines.append("stage = llm_past")
            lines.append(f"title = {ev.get('title', '?')}")
            lines.append(f"date = {ev.get('date', '?')}")
            lines.append(f"source = {ev.get('source', '?')}")
            lines.append(f"reason = {ev.get('reason', '?')}")
            lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n  {total_rejected} total rejects -> {out_path}\n")
