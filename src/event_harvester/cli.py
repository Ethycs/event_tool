"""CLI entry point and main orchestration."""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from event_harvester.analysis import extract_events_llm
from event_harvester.config import load_config, validate_config
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


def _run_url_diagnostics(url: str) -> str | None:
    """Open a URL and run all detection modes. Returns the recommended mode string."""
    import concurrent.futures

    from event_harvester.sources.uc_browser import UCBrowser
    from event_harvester.sources.web_fetch import (
        _extract_event_links,
        _extract_text,
        _wait_cloudflare,
    )

    detected_mode = [None]  # mutable container for thread result

    def _do_diagnostics():
        with UCBrowser() as uc:
            print(f"  Testing: {url}\n")
            page = uc.open(url, wait_ms=3000)
            html = _wait_cloudflare(page, headless=False)

            if "just a moment" in html.lower():
                print(f"  {RED}BLOCKED{RESET} — Cloudflare challenge not resolved.")
                print("  Recommendation: use native_chrome=True\n")
                page.close()
                return

            # ── 1. Raw text ──────────────────────────────────────
            text = _extract_text(html)
            print(f"  Raw text: {len(text)} chars")

            # ── 2. Event link extraction (calendar mode) ─────────
            event_links = _extract_event_links(page)
            date_links = [l for l in event_links if l.get("date_hint")]
            inline_links = [l for l in event_links if l.get("inline")]
            fetch_links = [l for l in event_links if not l.get("inline")]

            print(f"  Event links: {len(event_links)} total "
                  f"({len(date_links)} with dates, "
                  f"{len(inline_links)} inline, "
                  f"{len(fetch_links)} fetchable)")

            # ── 3. UC pattern detection ──────────────────────────
            patterns = uc.detect_all(page)
            if patterns:
                for ptype, hits in patterns.items():
                    if hits:
                        best = hits[0].get("confidence", 0)
                        print(f"  UC {ptype}: {len(hits)} hit(s), best={best:.2f}")

            # ── 4. Feed detection ────────────────────────────────
            feed_items = uc.get_feed_items(page)
            feed_items = [t for t in feed_items if len(t) > 30]
            print(f"  Feed items: {len(feed_items)}")

            # ── 5. Search bar ────────────────────────────────────
            search_hits = uc.detect(page, "search")
            has_search = len(search_hits) > 0

            # ── Recommendation ───────────────────────────────────
            print(f"\n  {BOLD}Recommendation:{RESET}")
            if len(event_links) >= 3:
                mode = "calendar"
                print(f"  {GREEN}mode: calendar{RESET} — {len(event_links)} event links found")
                if inline_links:
                    print(f"    {len(inline_links)} inline (no fetch needed)")
                if fetch_links:
                    print(f"    {len(fetch_links)} detail pages to fetch")
            elif has_search:
                mode = "search"
                ph = search_hits[0].get("placeholder", "")
                print(f"  {GREEN}mode: search{RESET} — search bar detected" +
                      (f' (placeholder: "{ph}")' if ph else ""))
            elif len(feed_items) >= 3:
                mode = "feed"
                print(f"  {GREEN}mode: feed{RESET} — {len(feed_items)} feed items")
            else:
                mode = "raw"
                print("  mode: raw — no structured content detected")

            detected_mode[0] = mode

            # ── Sample output ────────────────────────────────────
            print(f"\n  {BOLD}Sample events:{RESET}")
            if event_links:
                for lnk in event_links[:5]:
                    dh = lnk.get("date_hint", "")
                    tag = " [inline]" if lnk.get("inline") else ""
                    print(f"    {dh:12s} {lnk['text'][:70]}{tag}")
            elif feed_items:
                for item in feed_items[:5]:
                    print(f"    {item[:80]}")
            else:
                print(f"    {text[:200]}")

            # ── Suggested config ─────────────────────────────────
            print(f"\n  {BOLD}Suggested config:{RESET}")
            from urllib.parse import urlparse
            name = urlparse(url).netloc.split(".")[0]
            if name in ("www", "business"):
                name = urlparse(url).netloc.split(".")[-2]
            cfg = f'    {{"url": "{url}", "name": "{name}", "mode": "{mode}"'
            if mode == "feed":
                cfg += ', "api_pattern": r"api|graphql", "scroll_seconds": 15'
            if mode == "search":
                cfg += ', "query": "events", "scroll_seconds": 10'
            cfg += "}"
            print(f"  {cfg}")

            page.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(_do_diagnostics).result()

    return detected_mode[0]


def _add_source_to_config(url: str, detected_mode: str | None = None) -> None:
    """Add a web source to data/web_sources.json using the detected mode."""
    from urllib.parse import urlparse

    from event_harvester.sources.web_fetch import _WEB_SOURCES_FILE, _load_web_sources
    from event_harvester.utils import save_json

    sources = _load_web_sources()

    # Check for duplicate
    if any(s["url"] == url for s in sources):
        print(f"\n  {BOLD}Already configured{RESET} — {url} is already in web_sources.json")
        return

    name = urlparse(url).netloc.split(".")[0]
    if name in ("www", "business"):
        name = urlparse(url).netloc.split(".")[-2]

    mode = detected_mode or "calendar"
    source = {"url": url, "name": name, "mode": mode}
    if mode == "feed":
        source["api_pattern"] = r"api|graphql"
        source["scroll_seconds"] = 15
    elif mode == "search":
        source["query"] = "events"
        source["scroll_seconds"] = 10

    sources.append(source)
    save_json(_WEB_SOURCES_FILE, sources)
    print(f"\n  {GREEN}{BOLD}Added{RESET} {name} (mode={mode}) -> {_WEB_SOURCES_FILE}")


def _run_recruiter_grading(all_messages, cfg, args) -> list:
    """Grade Gmail recruiter emails and optionally auto-trash."""
    from event_harvester.recruiter_score import grade_emails_batch
    from event_harvester.sources import fetch_full_bodies
    from event_harvester.sources import gmail_trash as trash

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
        "--group-by-source", action="store_true",
        help="Sort and display events grouped by source platform",
    )
    parser.add_argument(
        "--cap-discord", type=int, default=None, help="Per-source cap for Discord (default: 50)",
    )
    parser.add_argument(
        "--cap-telegram", type=int, default=None, help="Per-source cap for Telegram (default: 50)",
    )
    parser.add_argument(
        "--cap-gmail", type=int, default=None, help="Per-source cap for Gmail (default: 30)",
    )
    parser.add_argument(
        "--cap-signal", type=int, default=None, help="Per-source cap for Signal (default: 30)",
    )
    parser.add_argument(
        "--cap-web", type=int, default=None, help="Per-source cap for web sources (default: 30)",
    )
    parser.add_argument(
        "--cap-total", type=int, default=None,
        help="Global cap after per-source caps (default: 150)",
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
        "--add-source", metavar="URL",
        help="Run diagnostics on a URL, then add it to data/web_sources.json",
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
        "--serve", action="store_true",
        help="Start local web server to review and approve/decline events",
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

    # Apply CLI cap overrides
    if args.cap_discord is not None:
        cfg.caps.discord = args.cap_discord
    if args.cap_telegram is not None:
        cfg.caps.telegram = args.cap_telegram
    if args.cap_gmail is not None:
        cfg.caps.gmail = args.cap_gmail
    if args.cap_signal is not None:
        cfg.caps.signal = args.cap_signal
    if args.cap_web is not None:
        cfg.caps.web = args.cap_web
    if args.cap_total is not None:
        cfg.caps.total = args.cap_total
    if args.group_by_source:
        cfg.caps.group_by_source = True

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
        from event_harvester.sources.web_session import web_login
        web_login()
        return

    # ── Test URL mode (diagnostics) ─────────────────────────────────────
    if args.test_url:
        _run_url_diagnostics(args.test_url)
        return

    # ── Add source mode ─────────────────────────────────────────────────
    if args.add_source:
        detected_mode = _run_url_diagnostics(args.add_source)
        _add_source_to_config(args.add_source, detected_mode)
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

    summary, events = extract_events_llm(actionable, cfg.days_back, cfg.llm, caps=cfg.caps)

    if not events:
        print("No events extracted.")
        return

    # Check which events are already fingerprinted (in TickTick)
    from event_harvester.event_match import find_fingerprint

    def _print_event(idx: int, t: dict) -> None:
        title = t.get("title") or "Untitled"
        date_str = t.get("date") or ""
        time_str = t.get("time") or ""
        location = t.get("location") or ""
        source = t.get("source") or ""
        notes = t.get("notes") or ""

        fp = find_fingerprint(t)
        status = "in TickTick" if fp else "new"

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

    if cfg.caps.group_by_source:
        # Group events by their source platform/channel
        from collections import defaultdict

        groups: dict[str, list[dict]] = defaultdict(list)
        for t in events:
            src = t.get("source", "") or "unknown"
            # Extract platform from "@author in #channel" format
            if " in " in src:
                channel = src.split(" in ", 1)[1].strip().lstrip("#")
                key = channel or "unknown"
            else:
                key = src.lstrip("@") or "unknown"
            groups[key].append(t)

        print(f"\n{BOLD}Events ({len(events)}) grouped by source{RESET}")
        idx = 1
        for source_key in sorted(groups.keys()):
            bucket = groups[source_key]
            print(f"\n{BOLD}── {source_key} ({len(bucket)}) ──{RESET}")
            for t in bucket:
                _print_event(idx, t)
                idx += 1
    else:
        print(f"\n{BOLD}Events ({len(events)}){RESET}")
        for i, t in enumerate(events, 1):
            _print_event(i, t)

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

    # ── Serve mode — local server with markdown report ──────────────────────
    if args.serve:
        from event_harvester.server import serve_events
        serve_events(events)
        return

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
