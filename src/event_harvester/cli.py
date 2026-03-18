"""CLI entry point and main orchestration."""

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from event_harvester.analysis import PRIORITY_LABEL, analyse_and_extract_tasks
from event_harvester.config import load_config, validate_config
from event_harvester.display import BOLD, DIM, GREEN, RESET, print_message
from event_harvester.sources.discord import read_discord_messages
from event_harvester.sources.telegram import read_telegram_messages
from event_harvester.ticktick import create_ticktick_tasks, get_ticktick_client
from event_harvester.watch import watch_mode

logger = logging.getLogger("event_harvester")


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="  %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )


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
        need_analysis=not args.no_analysis,
        need_ticktick=not args.no_ticktick and not args.dry_run,
    )
    for w in warnings:
        logger.warning(w)

    # ── Watch mode ──────────────────────────────────────────────────────────
    if args.watch:
        await watch_mode(cfg, args.interval, args.no_telegram, args.no_discord)
        return

    # ── One-shot mode ───────────────────────────────────────────────────────
    cutoff = datetime.now(timezone.utc) - timedelta(days=cfg.days_back)
    W = 64
    print(f"\n{'═' * W}")
    print(f"  Event Harvester — last {cfg.days_back} day(s)")
    print(f"{'═' * W}\n")

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

    if not all_messages:
        print("No messages found. Check credentials / cache and try again.")
        return

    # Print messages
    for msg in sorted(all_messages, key=lambda m: m["timestamp"]):
        print_message(msg)

    n_d = sum(1 for m in all_messages if m["platform"] == "discord")
    n_t = sum(1 for m in all_messages if m["platform"] == "telegram")
    print(f"{DIM}Total: {len(all_messages)}  (Discord: {n_d}, Telegram: {n_t}){RESET}\n")

    if args.save:
        Path(args.save).write_text(json.dumps(all_messages, indent=2, ensure_ascii=False))
        print(f"Raw messages saved → {args.save}\n")

    # ── OpenRouter analysis + task extraction ───────────────────────────────
    if args.no_analysis:
        return

    print(f"{'═' * W}")
    print("[ OpenRouter — extracting action items ]")
    print(f"{'═' * W}\n")

    summary, tasks = await analyse_and_extract_tasks(all_messages, cfg.days_back, cfg.openrouter)

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
        print(f"  {i}. {BOLD}{t['title']}{RESET}{DIM}  [{prio}]{due}{RESET}")
        print(f"     {DIM}{t.get('notes', '')}{RESET}")

    # ── TickTick task creation ──────────────────────────────────────────────
    if args.no_ticktick:
        return

    print(f"\n{'═' * W}")
    mode_label = "[ TickTick — dry run ]" if args.dry_run else "[ TickTick — creating tasks ]"
    print(mode_label)
    print(f"{'═' * W}\n")

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
