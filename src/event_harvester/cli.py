"""CLI entry point and main orchestration."""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Optional

from event_harvester.analysis import extract_events_llm
from event_harvester.config import LLMConfig, load_config, validate_config
from event_harvester.display import (
    BOLD,
    DIM,
    GREEN,
    RED,
    RESET,
    print_links,
    print_message,
    print_recruiter_grades,
)
from event_harvester.report import generate_report
from event_harvester.sources import filter_read_sent
from event_harvester.ticktick import create_ticktick_tasks, get_ticktick_client
from event_harvester.watch import watch_mode
from event_harvester.weights import extract_links

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


def _run_recruiter_grading(all_messages, cfg, args) -> list:
    """Grade Gmail recruiter emails and optionally auto-trash."""
    from event_harvester.recruiter_score import grade_emails_batch
    from event_harvester.sources import fetch_full_bodies, gmail_trash as trash

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
    print_recruiter_grades(grades)

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


def _build_parser() -> argparse.ArgumentParser:
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
        "--no-signal", action="store_true", help="Skip Signal",
    )
    parser.add_argument(
        "--no-web", action="store_true", help="Skip web page fetching",
    )
    parser.add_argument(
        "--web-login", action="store_true",
        help="Open browser to log into event sites, save session for future runs",
    )
    parser.add_argument(
        "--test-url", metavar="URL",
        help="Fetch a single URL with Playwright and display extracted text (test mode)",
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
        "--train-classifier", action="store_true",
        help="Label messages with LLM and train a local binary classifier",
    )
    parser.add_argument(
        "--save-labels", metavar="FILE",
        help="Save labeled data as JSON for review/correction",
    )
    parser.add_argument(
        "--load-labels", metavar="FILE",
        help="Train classifier from previously saved/corrected labels JSON",
    )
    parser.add_argument(
        "--eval-classifier", action="store_true",
        help="Evaluate classifier accuracy on post-filter messages using LLM as ground truth",
    )
    parser.add_argument(
        "--save-eval", metavar="DIR",
        help="Save eval samples to DIR for manual review (500 per stage)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable debug logging",
    )
    return parser


async def main() -> None:
    args = _build_parser().parse_args()

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

    # ── Web login mode ─────────────────────────────────────────────────────
    if args.web_login:
        from event_harvester.sources.web_fetch import web_login
        web_login()
        return

    # ── Test URL mode ─────────────────────────────────────────────────────
    if args.test_url:
        from event_harvester.sources.web_fetch import fetch_event_pages
        print(f"Testing: {args.test_url}\n")
        msgs = fetch_event_pages(urls=[{"url": args.test_url, "headless": False}])
        if msgs:
            print(f"  {len(msgs)} message(s) extracted.\n")
            for i, m in enumerate(msgs):
                print(f"  --- [{i+1}/{len(msgs)}] {m['channel'][:80]} ---")
                print(f"  {m['content'][:500]}")
                print()
        else:
            print("  No content extracted.")
        return

    # ── Reparse mode ────────────────────────────────────────────────────────
    if args.reparse:
        from event_harvester.obsidian import reparse_recruiter_report

        reparse_recruiter_report(args.reparse, cfg.gmail)
        return

    # ── Watch mode ──────────────────────────────────────────────────────────
    if args.watch:
        await watch_mode(cfg, args.interval, args.no_telegram, args.no_discord)
        return

    # ── Eval classifier from saved labels (no fetching needed) ─────────────
    if args.eval_classifier and args.load_labels:
        from event_harvester.eval_classifier import run_eval

        run_eval(args.load_labels, save_eval=args.save_eval)
        return

    # ── One-shot mode ───────────────────────────────────────────────────────
    from event_harvester.harvest import harvest_messages, save_messages

    W = 64
    print(f"\n{'=' * W}")
    print(f"  Event Harvester - last {cfg.days_back} day(s)")
    print(f"{'=' * W}\n")

    all_messages = await harvest_messages(
        cfg,
        load_path=args.load,
        no_discord=args.no_discord,
        no_telegram=args.no_telegram,
        no_gmail=args.no_gmail,
        no_signal=args.no_signal,
        no_web=args.no_web,
        skip_cache=bool(args.save),
    )

    if not all_messages:
        print("No messages found. Check credentials / cache and try again.")
        return

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

    # ── Train classifier (skip if eval mode) ────────────────────────────────
    if (args.train_classifier or args.load_labels) and not args.eval_classifier:
        from event_harvester.classifier import train as train_classifier

        if args.load_labels:
            # Train from previously saved labels
            try:
                labeled = json.loads(Path(args.load_labels).read_text(encoding="utf-8"))
                print(f"Loaded {len(labeled)} labeled messages from {args.load_labels}")
            except Exception as e:
                logger.error("Failed to load labels: %s", e)
                return
        else:
            # Label with LLM then train
            from event_harvester.label import label_messages

            print(f"\n{'=' * W}")
            print("[ Labeling messages with LLM ]")
            print(f"{'=' * W}\n")

            labeled = label_messages(all_messages, cfg.llm)

        # Optionally save labels for review
        if args.save_labels:
            Path(args.save_labels).write_text(
                json.dumps(labeled, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            print(f"Labels saved -> {args.save_labels}\n")

        # Train the classifier
        msgs_for_train = [{k: v for k, v in m.items() if k != "label"} for m in labeled]
        labels_for_train = [m["label"] for m in labeled]

        print(f"\n{'=' * W}")
        print("[ Training classifier ]")
        print(f"{'=' * W}\n")

        train_classifier(msgs_for_train, labels_for_train)
        return

    # ── Filter out already-read/sent Gmail messages for LLM steps ──────────
    actionable = filter_read_sent(all_messages)
    n_filtered = len(all_messages) - len(actionable)
    if n_filtered:
        print(
            f"{DIM}Filtered {n_filtered} read/sent messages, "
            f"{len(actionable)} remain for analysis.{RESET}\n"
        )

    # ── Extract links ──────────────────────────────────────────────────────
    links = extract_links(actionable)
    print_links(links)

    source_counts = {"discord": n_d, "telegram": n_t, "gmail": n_g, "signal": n_s, "web": n_w}

    # ── Recruiter email grading ──────────────────────────────────────────────
    grades = []
    if args.grade_recruiters:
        grades = _run_recruiter_grading(all_messages, cfg, args)

    # ── Obsidian recruiter report ─────────────────────────────────────────────
    if args.obsidian and cfg.obsidian_recruiters_dir and grades:
        from event_harvester.obsidian import write_recruiter_report

        path = write_recruiter_report(grades, cfg.obsidian_recruiters_dir)
        print(f"Obsidian recruiters -> {path}\n")

    # ── LLM event extraction (unified path: classifier → reranker → LLM) ──
    if args.no_analysis:
        return

    print(f"{'=' * W}")
    print("[ LLM - extracting events ]")
    print(f"{'=' * W}\n")

    summary, events = extract_events_llm(actionable, cfg.days_back, cfg.llm)

    if not events:
        print("No events extracted.")
        return

    # Check which events are already fingerprinted (in TickTick)
    from event_harvester.event_match import find_fingerprint

    print(f"\n{BOLD}Events ({len(events)}){RESET}")
    for i, t in enumerate(events, 1):
        title = t.get("title") or "Untitled"
        date_str = t.get("date") or ""
        time_str = t.get("time") or ""
        location = t.get("location") or ""
        source = t.get("source") or ""
        notes = t.get("notes") or ""

        fp = find_fingerprint(t)
        status = "in TickTick" if fp else "new"

        print(f"  [{i}] {BOLD}{title}{RESET} {DIM}({status}){RESET}")
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

    # ── Reports (use LLM-extracted events) ────────────────────────────────
    validated_events = []
    for t in events:
        source = t.get("source", "")
        # Strip leading @ from source to avoid double-@ in report
        author = source.split(" in ")[0].strip().lstrip("@") if " in " in source else ""
        channel = source.split(" in ")[1].strip().lstrip("#") if " in " in source else ""

        validated_events.append({
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

    # ── TickTick event sync ─────────────────────────────────────────────────
    if args.no_ticktick:
        return

    print(f"\n{'=' * W}")
    mode_label = "[ TickTick - dry run ]" if args.dry_run else "[ TickTick - syncing events ]"
    print(mode_label)
    print(f"{'=' * W}\n")

    tt = get_ticktick_client(cfg.ticktick)
    if tt is None:
        return

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


def main_sync() -> None:
    """Synchronous wrapper for the entry point."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        from event_harvester.llm import shutdown_local
        shutdown_local()


if __name__ == "__main__":
    main_sync()
