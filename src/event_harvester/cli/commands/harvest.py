"""Harvest command — the main pipeline.

Fetches messages from configured sources, extracts events with the LLM,
optionally writes reports, and syncs to TickTick.

Reuses the existing pipeline functions verbatim — this file just
glues argparse args to the functions in harvest.py / analysis.py /
ticktick.py / etc.
"""

import logging
from collections import defaultdict

from event_harvester.analysis import extract_events_llm
from event_harvester.cli.parse_helpers import apply_caps_to_config, resolve_platforms
from event_harvester.config import validate_config
from event_harvester.display import (
    BOLD,
    DIM,
    GREEN,
    RESET,
    print_links,
    print_message,
)
from event_harvester.report import generate_report
from event_harvester.sources import filter_read_sent
from event_harvester.ticktick import create_ticktick_tasks, get_ticktick_client
from event_harvester.weights import extract_links

logger = logging.getLogger("event_harvester")

W = 64


async def harvest_cmd(args, cfg) -> int:
    """Run the harvest pipeline. Returns an exit code."""
    # Apply CLI overrides to config
    if args.days is not None:
        cfg.days_back = args.days
    apply_caps_to_config(cfg, args.cap)
    if args.group_by_source:
        cfg.caps.group_by_source = True

    # Validate config based on which sources are active
    platform_kwargs = resolve_platforms(args.only, args.skip)
    warnings = validate_config(
        cfg,
        need_telegram=not platform_kwargs["no_telegram"],
        need_discord=not platform_kwargs["no_discord"],
        need_gmail=not platform_kwargs["no_gmail"],
        need_analysis=not args.no_analyze,
        need_ticktick=not args.no_sync and not args.dry_run,
    )
    for w in warnings:
        logger.warning(w)

    # ── Fetch messages ──────────────────────────────────────────────────
    from event_harvester.harvest import harvest_messages, save_messages

    print(f"\n{'=' * W}")
    print(f"  Event Harvester - last {cfg.days_back} day(s)")
    print(f"{'=' * W}\n")

    all_messages = await harvest_messages(
        cfg,
        load_path=args.load,
        skip_cache=bool(args.save),
        web_source=getattr(args, "web_source", None),
        no_cooldown=getattr(args, "no_cooldown", False),
        **platform_kwargs,
    )

    if not all_messages:
        print("No messages found. Check credentials / cache and try again.")
        return 0

    # Print messages
    for msg in sorted(all_messages, key=lambda m: m["timestamp"]):
        print_message(msg)

    n_d = sum(1 for m in all_messages if m["platform"] == "discord")
    n_t = sum(1 for m in all_messages if m["platform"] == "telegram")
    n_g = sum(1 for m in all_messages if m["platform"] == "gmail")
    n_s = sum(1 for m in all_messages if m["platform"] == "signal")
    n_w = sum(1 for m in all_messages if m["platform"] == "web")
    counts = f"Discord: {n_d}, Telegram: {n_t}, Gmail: {n_g}, Signal: {n_s}"
    if n_w:
        counts += f", Web: {n_w}"
    print(f"{DIM}Total: {len(all_messages)}  ({counts}){RESET}\n")

    if args.save:
        save_messages(all_messages, args.save)

    # ── Filter out already-read/sent Gmail messages ─────────────────────
    actionable = filter_read_sent(all_messages)
    n_filtered = len(all_messages) - len(actionable)
    if n_filtered:
        print(
            f"{DIM}Filtered {n_filtered} read/sent messages, "
            f"{len(actionable)} remain for analysis.{RESET}\n"
        )

    # ── Extract links ───────────────────────────────────────────────────
    links = extract_links(actionable)
    print_links(links)

    source_counts = {
        "discord": n_d, "telegram": n_t, "gmail": n_g, "signal": n_s, "web": n_w,
    }

    # ── Recruiter grading (convenience integration) ─────────────────────
    grades = []
    if args.grade_recruiters:
        from event_harvester.cli.commands.recruiters import _run_recruiter_grading
        grades = _run_recruiter_grading(all_messages, cfg, args)

    if args.obsidian and cfg.obsidian_recruiters_dir and grades:
        from event_harvester.obsidian import write_recruiter_report

        path = write_recruiter_report(grades, cfg.obsidian_recruiters_dir)
        print(f"Obsidian recruiters -> {path}\n")

    # ── LLM event extraction ────────────────────────────────────────────
    if args.no_analyze:
        return 0

    print(f"{'=' * W}")
    print("[ LLM - extracting events ]")
    print(f"{'=' * W}\n")

    # When --only is used, the user has explicitly scoped to specific
    # sources — don't apply per-source caps that would silently drop
    # events. Caps exist to prevent noisy sources from crowding out
    # quiet ones in the default mixed-source pipeline; that concern
    # doesn't apply when --only is set.
    caps_for_llm = None if args.only is not None else cfg.caps
    if args.only is not None:
        logger.info("--only set; ignoring per-source caps")

    summary, events = extract_events_llm(
        actionable, cfg.days_back, cfg.llm, caps=caps_for_llm,
    )

    if getattr(args, "show_rejects", False):
        from event_harvester.analysis import _extract_events_cloud
        rejects = getattr(extract_events_llm, "_last_rejects", {})
        rejects["llm_past"] = getattr(
            _extract_events_cloud, "_last_llm_dropped", [],
        )
        rejects["llm_no_events"] = getattr(
            _extract_events_cloud, "_last_llm_no_events", [],
        )
        _print_rejects(rejects)

    if not events:
        print("No events extracted.")
        return 0

    _print_events(events, cfg)

    # ── Reports ─────────────────────────────────────────────────────────
    validated_events = _events_for_report(events)

    if args.report:
        report_path = generate_report(
            validated_events=validated_events,
            raw_events=[],
            links=links,
            source_counts=source_counts,
            total_messages=len(all_messages),
            output_path=args.report,
        )
        print(f"\nReport saved -> {report_path}\n")

    if args.obsidian and cfg.obsidian_events_dir:
        from event_harvester.obsidian import write_events_report

        path = write_events_report(
            validated_events=validated_events,
            raw_events=[],
            links=links,
            source_counts=source_counts,
            total_messages=len(all_messages),
            output_dir=cfg.obsidian_events_dir,
        )
        print(f"Obsidian events -> {path}\n")

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

    result = create_ticktick_tasks(
        tt, events, project_name=cfg.ticktick.project, dry_run=args.dry_run,
    )

    n_created = len(result["created"])
    n_updated = len(result["updated"])
    n_skipped = len(result["skipped"])
    print(
        f"\n{BOLD}Summary:{RESET} "
        f"{GREEN}{n_created} created{RESET}, "
        f"{n_updated} updated, "
        f"{DIM}{n_skipped} skipped{RESET}\n"
    )
    return 0


# ── Helpers ──────────────────────────────────────────────────────────


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
    from event_harvester.event_match import _normalize_date, find_acted_fingerprint

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
    from pathlib import Path

    stage_labels = {
        "classifier": "Classifier",
        "reranker": "Reranker",
        "caps": "Per-source caps",
        "llm_past": "LLM (past events)",
        "llm_no_events": "LLM (no events extracted)",
    }
    stages = ("classifier", "reranker", "caps", "llm_past", "llm_no_events")

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
    out_path = Path("data/rejects.ini")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    idx = 0

    # Message-level rejects (classifier, reranker, caps, llm_no_events)
    for stage in ("classifier", "reranker", "caps", "llm_no_events"):
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
