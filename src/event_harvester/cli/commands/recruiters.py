"""Recruiters command — grade Gmail recruiter emails or reparse a saved report."""

import logging

from event_harvester.config import validate_config
from event_harvester.display import DIM, RED, RESET, print_recruiter_grades

logger = logging.getLogger("event_harvester")


async def recruiters_cmd(args, cfg) -> int:
    sub = args.recruiters_command
    if sub == "grade":
        return await _recruiters_grade(args, cfg)
    if sub == "reparse":
        return _recruiters_reparse(args, cfg)
    logger.error("Unknown recruiters subcommand: %s", sub)
    return 1


async def _recruiters_grade(args, cfg) -> int:
    """Harvest gmail messages and run recruiter grading."""
    if args.days is not None:
        cfg.days_back = args.days

    warnings = validate_config(
        cfg,
        need_telegram=False,
        need_discord=False,
        need_gmail=True,
        need_analysis=not args.no_analyze,
        need_ticktick=False,
    )
    for w in warnings:
        logger.warning(w)

    # Fetch only Gmail by disabling all other sources
    from event_harvester.harvest import harvest_messages

    all_messages = await harvest_messages(
        cfg,
        no_discord=True,
        no_telegram=True,
        no_gmail=False,
        no_signal=True,
        no_web=True,
    )

    if not all_messages:
        print("No Gmail messages found.")
        return 0

    grades = _run_recruiter_grading(all_messages, cfg, args)

    # Optional Obsidian recruiter report
    if args.obsidian and cfg.obsidian_recruiters_dir and grades:
        from event_harvester.obsidian import write_recruiter_report

        path = write_recruiter_report(grades, cfg.obsidian_recruiters_dir)
        print(f"Obsidian recruiters -> {path}\n")

    return 0


def _recruiters_reparse(args, cfg) -> int:
    """Interactively act on a saved recruiter report."""
    from event_harvester.obsidian import reparse_recruiter_report

    reparse_recruiter_report(args.file, cfg.gmail)
    return 0


# ── Shared helper used by both `recruiters grade` and `harvest --grade-recruiters` ──


def _run_recruiter_grading(all_messages, cfg, args) -> list:
    """Grade Gmail recruiter emails and optionally auto-trash.

    Args:
        all_messages: list of message dicts from any source
        cfg: AppConfig
        args: argparse Namespace; expected attributes: no_analyze, auto_trash
    """
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

    # Tolerate either no_analyze (new) or no_analysis (legacy attr name)
    no_analyze = getattr(args, "no_analyze", getattr(args, "no_analysis", False))
    grades = grade_emails_batch(
        gmail_msgs,
        bodies=bodies,
        llm_cfg=cfg.llm if not no_analyze else None,
    )
    print_recruiter_grades(grades)

    # Auto-trash
    if getattr(args, "auto_trash", False):
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
