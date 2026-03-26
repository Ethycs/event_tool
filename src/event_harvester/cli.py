"""CLI entry point and main orchestration."""

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from event_harvester.analysis import PRIORITY_LABEL, analyse_and_extract_tasks
from event_harvester.config import LLMConfig, load_config, validate_config
from event_harvester.display import BOLD, CYAN, DIM, GREEN, RED, RESET, YELLOW, print_message
from event_harvester.llm_filter import validate_events
from event_harvester.report import generate_report
from event_harvester.sources.discord import read_discord_messages
from event_harvester.sources.gmail import fetch_messages as fetch_gmail_messages
from event_harvester.sources.telegram import read_telegram_messages
from event_harvester.ticktick import create_ticktick_tasks, get_ticktick_client
from event_harvester.watch import watch_mode
from event_harvester.weights import extract_events, extract_links, prefilter_events

logger = logging.getLogger("event_harvester")


def _setup_logging(verbose: bool) -> None:
    # Fix encoding for Windows cp1252 terminals
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="  %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )


def _print_links(links: list[dict], max_links: int = 15) -> None:
    """Print weighted links section."""
    W = 64
    if not links:
        return
    print(f"{'=' * W}")
    print(f"  {BOLD}Links{RESET}  {DIM}(recency x type){RESET}")
    print(f"{'=' * W}\n")
    for i, lnk in enumerate(links[:max_links], 1):
        score_color = GREEN if lnk["score"] >= 8 else YELLOW if lnk["score"] >= 5 else DIM
        pin_tag = f" {YELLOW}[PIN]{RESET}" if lnk.get("pinned") else ""
        print(
            f"  {score_color}{lnk['score']:4.1f}{RESET}{pin_tag}  "
            f"{lnk['url'][:90]}"
        )
        ctx = lnk["context"].strip()
        url_only = ctx == lnk["url"][:120].strip()
        meta = f"@{lnk['author']} {DIM}{lnk['timestamp']}{RESET}"
        if not url_only and ctx:
            ctx_short = ctx[:80]
            if ctx_short != ctx:
                ctx_short += "..."
            print(f"         {DIM}\"{ctx_short}\"{RESET}")
            print(f"         {meta}")
        else:
            print(f"         {meta}")
        print()


def _print_validated_events(events: list[dict], max_events: int = 20) -> None:
    """Print LLM-validated events."""
    W = 64
    if not events:
        print(f"{DIM}No validated events.{RESET}\n")
        return
    print(f"{'=' * W}")
    print(f"  {BOLD}Validated Events{RESET}  {DIM}(LLM-filtered){RESET}")
    print(f"{'=' * W}\n")
    for i, evt in enumerate(events[:max_events], 1):
        pin_tag = f" {YELLOW}[PIN]{RESET}" if evt.get("pinned") else ""
        score = evt.get("score", 0)
        score_color = GREEN if score >= 10 else YELLOW if score >= 6 else DIM

        title = evt.get("title", "Untitled")
        date = evt.get("date") or "TBD"
        source = evt.get("source", "?")
        author = evt.get("author", "?")
        details = evt.get("details", "")

        print(
            f"  {score_color}{score:2}{RESET}{pin_tag}  "
            f"{BOLD}{title}{RESET}"
        )
        print(f"         {CYAN}{date}{RESET} in {source} (@{author})")
        if details:
            print(f"         {DIM}{details[:120]}{RESET}")
        print()


def _print_raw_events(events: list[dict], max_events: int = 15) -> None:
    """Print raw weighted events (fallback when LLM is unavailable)."""
    W = 64
    if not events:
        return
    print(f"{'=' * W}")
    print(f"  {BOLD}Events & Dates{RESET}  {DIM}(recency + scheduling){RESET}")
    print(f"{'=' * W}\n")
    for i, evt in enumerate(events[:max_events], 1):
        sched_tag = f" {CYAN}[SCHED]{RESET}" if evt["scheduling"] else ""
        pin_tag = f" {YELLOW}[PIN]{RESET}" if evt.get("pinned") else ""
        score_color = GREEN if evt["score"] >= 10 else YELLOW if evt["score"] >= 6 else DIM
        print(
            f"  {score_color}{evt['score']:2d}{RESET}{sched_tag}{pin_tag}  "
            f"@{evt['author']} {DIM}{evt['timestamp']}{RESET}"
        )
        dates_str = ", ".join(evt["dates"])
        times_str = ", ".join(evt["times"])
        refs = []
        if dates_str:
            refs.append(f"dates=[{dates_str}]")
        if times_str:
            refs.append(f"times=[{times_str}]")
        if refs:
            print(f"         {BOLD}{' '.join(refs)}{RESET}")
        content = evt["content"][:120]
        if content != evt["content"][:200]:
            content += "..."
        print(f"         {DIM}\"{content}\"{RESET}")
        print()


def _print_weighted_analysis(
    messages: list[dict],
    llm_cfg: Optional["LLMConfig"] = None,
    max_links: int = 15,
    max_events: int = 20,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Extract, weight, validate, and print links and events.

    Returns (validated_or_raw_events, raw_events, links).
    """
    links = extract_links(messages)
    events = extract_events(messages)
    validated: list[dict] = []

    _print_links(links, max_links)

    if events:
        # Layer 1: Structural pre-filter (future date, scheduling+date, event link)
        candidates = prefilter_events(events)
        logger.info(
            "Layer 1 (structural): %d / %d events passed pre-filter.",
            len(candidates), len(events),
        )

        if not candidates:
            # Nothing passed structural filter — show raw events
            _print_raw_events(events, max_events)
        else:
            # Layer 2: LLM validation — is it actually an event?
            validated = validate_events(candidates[:max_events], cfg=llm_cfg)

            if validated and isinstance(validated[0], dict) and "title" in validated[0]:
                _print_validated_events(validated, max_events)
            else:
                validated = []
                _print_raw_events(candidates, max_events)

    if not links and not events:
        print(f"{DIM}No links or date references found.{RESET}\n")

    return validated, events, links


def _print_recruiter_grades(grades: list, max_items: int = 30) -> None:
    """Print graded recruiter emails with color-coded scores."""
    W = 64
    print(f"{'=' * W}")
    print(f"  {BOLD}Recruiter Email Grades{RESET}  {DIM}(0-100){RESET}")
    print(f"{'=' * W}\n")

    for grade in grades[:max_items]:
        if grade.score >= 66:
            color, tag = GREEN, "[RESPOND]"
        elif grade.score >= 46:
            color, tag = YELLOW, "[REVIEW] "
        elif grade.score >= 21:
            color, tag = DIM, "[IGNORE] "
        else:
            color, tag = RED, "[TRASH]  "

        print(
            f"  {color}{grade.score:3d}{RESET} {color}{tag}{RESET}  "
            f"{BOLD}{grade.subject[:60]}{RESET}"
        )
        print(f"       From: {grade.sender[:50]}")
        for reason in grade.reasons[:3]:
            print(f"       {DIM}- {reason}{RESET}")
        print()


def _run_recruiter_grading(all_messages, cfg, args) -> list:
    """Grade Gmail recruiter emails and optionally auto-trash."""
    from event_harvester.recruiter_score import grade_emails_batch
    from event_harvester.sources.gmail import fetch_full_bodies, trash

    gmail_msgs = [m for m in all_messages if m["platform"] == "gmail"]
    if not gmail_msgs:
        print(f"{DIM}No Gmail messages to grade.{RESET}\n")
        return []

    # Fetch full bodies for better grading
    print(f"  Fetching full bodies for {len(gmail_msgs)} Gmail messages...")
    bodies = fetch_full_bodies(cfg.gmail, [m["id"] for m in gmail_msgs])
    print(f"  Got {len(bodies)} bodies.\n")

    grades = grade_emails_batch(
        gmail_msgs,
        bodies=bodies,
        llm_cfg=cfg.llm if not args.no_analysis else None,
    )
    _print_recruiter_grades(grades)

    # Auto-trash
    if args.auto_trash:
        trash_candidates = [g for g in grades if g.action == "trash"]
        if trash_candidates:
            print(
                f"\n{RED}Trashing {len(trash_candidates)} low-scoring email(s)...{RESET}"
            )
            for g in trash_candidates:
                trash(cfg.gmail, g.message_id)
                print(f"  {DIM}Trashed: {g.subject[:50]}{RESET}")
            print()
        else:
            print(f"{DIM}No emails below trash threshold.{RESET}\n")

    return grades


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Harvest Discord + Telegram messages, extract tasks "
            "via OpenRouter, create in TickTick."
        ),
    )
    parser.add_argument(
        "--days", type=int, default=None,
        help="Days to look back (default: from DAYS_BACK env or 7)",
    )
    parser.add_argument(
        "--watch", action="store_true",
        help="Watch mode: poll continuously and print new messages",
    )
    parser.add_argument(
        "--interval", type=int, default=30,
        help="Poll interval in seconds for --watch mode (default: 30)",
    )
    parser.add_argument(
        "--no-telegram", action="store_true", help="Skip Telegram",
    )
    parser.add_argument(
        "--no-discord", action="store_true", help="Skip Discord",
    )
    parser.add_argument(
        "--no-gmail", action="store_true", help="Skip Gmail",
    )
    parser.add_argument(
        "--no-analysis", action="store_true",
        help="Skip OpenRouter analysis + task extraction",
    )
    parser.add_argument(
        "--no-ticktick", action="store_true",
        help="Skip TickTick task creation",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show tasks that would be created without creating them",
    )
    parser.add_argument(
        "--save", metavar="FILE", help="Save raw messages to JSON",
    )
    parser.add_argument(
        "--load", metavar="FILE",
        help="Load messages from JSON instead of harvesting",
    )
    parser.add_argument(
        "--report", nargs="?", const="events_report.md", default=None,
        metavar="FILE", help="Generate markdown report with TickTick links",
    )
    parser.add_argument(
        "--grade-recruiters", action="store_true",
        help="Grade Gmail recruiter emails by quality",
    )
    parser.add_argument(
        "--auto-trash", action="store_true",
        help="Auto-trash recruiter emails scoring below 20 (requires --grade-recruiters)",
    )
    parser.add_argument(
        "--obsidian", action="store_true",
        help="Write Obsidian-compatible reports to configured vault directories",
    )
    parser.add_argument(
        "--reparse", metavar="FILE",
        help="Interactively act on a recruiter report (open/reply/trash)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    _setup_logging(args.verbose)

    # Load and validate config
    cfg = load_config()
    if args.days is not None:
        cfg.days_back = args.days

    warnings = validate_config(
        cfg,
        need_telegram=not args.no_telegram,
        need_discord=not args.no_discord,
        need_gmail=not args.no_gmail,
        need_analysis=not args.no_analysis,
        need_ticktick=not args.no_ticktick and not args.dry_run,
    )
    for w in warnings:
        logger.warning(w)

    # ── Reparse mode ────────────────────────────────────────────────────────
    if args.reparse:
        from event_harvester.obsidian import reparse_recruiter_report

        reparse_recruiter_report(args.reparse, cfg.gmail)
        return

    # ── Watch mode ──────────────────────────────────────────────────────────
    if args.watch:
        await watch_mode(cfg, args.interval, args.no_telegram, args.no_discord)
        return

    # ── One-shot mode ───────────────────────────────────────────────────────
    cutoff = datetime.now(timezone.utc) - timedelta(days=cfg.days_back)
    W = 64
    print(f"\n{'=' * W}")
    print(f"  Event Harvester - last {cfg.days_back} day(s)")
    print(f"{'=' * W}\n")

    all_messages: list[dict] = []

    if args.load:
        # Load from previously saved JSON
        try:
            all_messages = json.loads(Path(args.load).read_text())
            logger.info("Loaded %d messages from %s", len(all_messages), args.load)
        except Exception as e:
            logger.error("Failed to load %s: %s", args.load, e)
            return
    else:
        # Harvest from sources
        if not args.no_discord:
            print("[ Discord ]")
            all_messages.extend(
                read_discord_messages(cutoff, override_path=cfg.discord.cache_path)
            )
            print()

        if not args.no_telegram:
            print("[ Telegram ]")
            all_messages.extend(
                await read_telegram_messages(
                    cutoff,
                    cfg.telegram,
                    channels_allowlist=cfg.telegram_channels,
                    channels_blocklist=cfg.telegram_exclude,
                )
            )
            print()

        if not args.no_gmail:
            print("[ Gmail ]")
            all_messages.extend(fetch_gmail_messages(cfg.gmail, cutoff))
            print()

    if not all_messages:
        print("No messages found. Check credentials / cache and try again.")
        return

    # Print messages
    for msg in sorted(all_messages, key=lambda m: m["timestamp"]):
        print_message(msg)

    n_d = sum(1 for m in all_messages if m["platform"] == "discord")
    n_t = sum(1 for m in all_messages if m["platform"] == "telegram")
    n_g = sum(1 for m in all_messages if m["platform"] == "gmail")
    print(
        f"{DIM}Total: {len(all_messages)}  "
        f"(Discord: {n_d}, Telegram: {n_t}, Gmail: {n_g}){RESET}\n"
    )

    if args.save:
        Path(args.save).write_text(
            json.dumps(all_messages, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"Raw messages saved -> {args.save}\n")

    # ── Local weighted analysis ─────────────────────────────────────────────
    validated, raw_events, links = _print_weighted_analysis(
        all_messages, llm_cfg=cfg.llm,
    )

    source_counts = {"discord": n_d, "telegram": n_t, "gmail": n_g}

    # ── Markdown report ──────────────────────────────────────────────────────
    if args.report:
        report_path = generate_report(
            validated_events=validated,
            raw_events=raw_events,
            links=links,
            source_counts=source_counts,
            total_messages=len(all_messages),
            output_path=args.report,
        )
        print(f"Report saved -> {report_path}\n")

    # ── Obsidian events report ────────────────────────────────────────────────
    if args.obsidian and cfg.obsidian_events_dir:
        from event_harvester.obsidian import write_events_report

        path = write_events_report(
            validated_events=validated,
            raw_events=raw_events,
            links=links,
            source_counts=source_counts,
            total_messages=len(all_messages),
            output_dir=cfg.obsidian_events_dir,
        )
        print(f"Obsidian events -> {path}\n")

    # ── Recruiter email grading ──────────────────────────────────────────────
    grades = []
    if args.grade_recruiters:
        grades = _run_recruiter_grading(all_messages, cfg, args)

    # ── Obsidian recruiter report ─────────────────────────────────────────────
    if args.obsidian and cfg.obsidian_recruiters_dir and grades:
        from event_harvester.obsidian import write_recruiter_report

        path = write_recruiter_report(grades, cfg.obsidian_recruiters_dir)
        print(f"Obsidian recruiters -> {path}\n")

    # ── OpenRouter analysis + task extraction ───────────────────────────────
    if args.no_analysis:
        return

    print(f"{'=' * W}")
    print("[ OpenRouter - extracting action items ]")
    print(f"{'=' * W}\n")

    summary, tasks = analyse_and_extract_tasks(all_messages, cfg.days_back, cfg.llm)

    if summary:
        print(f"\n{BOLD}Summary{RESET}")
        print(f"{DIM}{summary}{RESET}\n")

    if not tasks:
        print("No action items extracted.")
        return

    print(f"\n{BOLD}Suggested tasks ({len(tasks)}){RESET}")
    for i, t in enumerate(tasks, 1):
        prio = PRIORITY_LABEL.get(t.get("priority", 0), "none")
        due = f"  due in {t['due_in_days']}d" if t.get("due_in_days") else ""
        title = t.get("title") or t.get("name") or t.get("task", "Untitled")
        notes = t.get("notes") or t.get("description", "")
        print(f"  {i}. {BOLD}{title}{RESET}{DIM}  [{prio}]{due}{RESET}")
        print(f"     {DIM}{notes}{RESET}")

    # ── TickTick task creation ──────────────────────────────────────────────
    if args.no_ticktick:
        return

    print(f"\n{'=' * W}")
    mode_label = "[ TickTick - dry run ]" if args.dry_run else "[ TickTick - creating tasks ]"
    print(mode_label)
    print(f"{'=' * W}\n")

    tt = get_ticktick_client(cfg.ticktick)
    if tt is None:
        return

    created = create_ticktick_tasks(
        tt, tasks, project_name=cfg.ticktick.project, dry_run=args.dry_run,
    )

    if not args.dry_run:
        print(f"\n{GREEN}{BOLD}{len(created)} task(s) created in TickTick.{RESET}\n")


def main_sync() -> None:
    """Synchronous wrapper for the entry point."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main_sync()
