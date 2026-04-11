"""Top-level argparse setup with subcommands.

Subcommand structure:
    event-harvester                              -> harvest (default, via _normalize_argv)
    event-harvester harvest [OPTIONS]
    event-harvester watch [--interval N]
    event-harvester web list|add URL|test URL|login
    event-harvester recruiters grade|reparse FILE
    event-harvester classifier train|eval --labels FILE
    event-harvester serve

Bare invocation `event-harvester --days 14 --skip web` is normalized
to `event-harvester harvest --days 14 --skip web` BEFORE argparse runs
(see dispatch._normalize_argv). This keeps argparse simple and avoids
double-registration via parents=.
"""

import argparse

from event_harvester import __version__
from event_harvester.cli.parse_helpers import (
    parse_cap_arg,
    parse_platform_csv,
)


def _add_harvest_args(p: argparse.ArgumentParser) -> None:
    """Register the full set of options for the harvest subcommand."""
    p.add_argument(
        "--days", type=int, default=None,
        help="Days to look back (default: from DAYS_BACK env or 7)",
    )

    src = p.add_mutually_exclusive_group()
    src.add_argument(
        "--only", type=parse_platform_csv, default=None,
        metavar="PLATFORMS",
        help="Comma-separated list of platforms to harvest (e.g. discord,gmail)",
    )
    src.add_argument(
        "--skip", type=parse_platform_csv, default=None,
        metavar="PLATFORMS",
        help="Comma-separated list of platforms to skip (e.g. web,signal)",
    )

    p.add_argument(
        "--cap", type=parse_cap_arg, action="append", default=[],
        metavar="KEY=VAL[,KEY=VAL]",
        help="Per-source caps, e.g. --cap discord=20,telegram=30 --cap total=100",
    )
    p.add_argument(
        "--group-by-source", action="store_true",
        help="Sort and display events grouped by source",
    )

    p.add_argument(
        "--no-analyze", action="store_true",
        help="Skip LLM analysis + event extraction",
    )
    p.add_argument(
        "--no-sync", action="store_true",
        help="Skip TickTick task creation",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Show tasks that would be created without creating them",
    )

    p.add_argument(
        "--save", metavar="FILE",
        help="Save raw harvested messages to JSON",
    )
    p.add_argument(
        "--load", metavar="FILE",
        help="Load messages from JSON instead of harvesting",
    )

    p.add_argument(
        "--report", nargs="?", const="events_report.md", default=None,
        metavar="FILE",
        help="Generate markdown report (default file: events_report.md)",
    )
    p.add_argument(
        "--obsidian", action="store_true",
        help="Write Obsidian-compatible event report to configured vault dir",
    )

    # Convenience: chain recruiter grading into the harvest pipeline
    p.add_argument(
        "--grade-recruiters", action="store_true",
        help="Also grade Gmail recruiter emails as part of this harvest",
    )
    p.add_argument(
        "--auto-trash", action="store_true",
        help="Auto-trash recruiter emails scoring below 20 (with --grade-recruiters)",
    )


def _add_root_flags(p: argparse.ArgumentParser) -> None:
    """Flags accepted at the root level by every subcommand."""
    verbosity = p.add_mutually_exclusive_group()
    verbosity.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable debug logging",
    )
    verbosity.add_argument(
        "-q", "--quiet", action="store_true",
        help="Only show warnings and errors",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog="event-harvester",
        description=(
            "Harvest events from Discord, Telegram, Gmail, Signal, and the web. "
            "Extract action items via LLM. Sync to TickTick."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}",
    )
    _add_root_flags(parser)

    sub = parser.add_subparsers(
        dest="command", required=True, metavar="COMMAND",
    )

    # ── harvest (default) ──
    p_harvest = sub.add_parser(
        "harvest",
        help="Run the main harvest pipeline (default if no command given)",
        description="Fetch messages, extract events with LLM, sync to TickTick.",
    )
    _add_harvest_args(p_harvest)
    _add_root_flags(p_harvest)

    # ── watch ──
    p_watch = sub.add_parser(
        "watch",
        help="Poll continuously for new messages",
        description="Watch mode: poll Discord and Telegram for new messages.",
    )
    p_watch.add_argument(
        "--interval", type=int, default=30,
        help="Poll interval in seconds (default: 30)",
    )
    src_w = p_watch.add_mutually_exclusive_group()
    src_w.add_argument("--only", type=parse_platform_csv, default=None, metavar="PLATFORMS")
    src_w.add_argument("--skip", type=parse_platform_csv, default=None, metavar="PLATFORMS")
    _add_root_flags(p_watch)

    # ── web ──
    p_web = sub.add_parser(
        "web",
        help="Manage web sources (list/add/test/login)",
    )
    web_sub = p_web.add_subparsers(dest="web_command", required=True, metavar="WEB_COMMAND")

    web_list = web_sub.add_parser("list", help="List configured web sources")
    _add_root_flags(web_list)

    web_add = web_sub.add_parser(
        "add",
        help="Run diagnostics on a URL and add it to data/web_sources.json",
    )
    web_add.add_argument("url", help="URL of the web source to add")
    web_add.add_argument(
        "--mode", default=None,
        choices=["calendar", "feed", "search", "raw"],
        help="Override the auto-detected mode",
    )
    _add_root_flags(web_add)

    web_test = web_sub.add_parser(
        "test",
        help="Run diagnostics on a URL without saving",
    )
    web_test.add_argument("url", help="URL to test")
    _add_root_flags(web_test)

    web_login = web_sub.add_parser(
        "login",
        help="Open a browser to log into web sites and save session state",
    )
    _add_root_flags(web_login)

    # ── recruiters ──
    p_rec = sub.add_parser(
        "recruiters",
        help="Recruiter email workflows (grade/reparse)",
    )
    rec_sub = p_rec.add_subparsers(
        dest="recruiters_command", required=True, metavar="RECRUITERS_COMMAND",
    )

    rec_grade = rec_sub.add_parser(
        "grade",
        help="Grade Gmail recruiter emails by quality",
    )
    rec_grade.add_argument(
        "--days", type=int, default=None,
        help="Days to look back (default: from DAYS_BACK env or 7)",
    )
    rec_grade.add_argument(
        "--auto-trash", action="store_true",
        help="Auto-trash emails scoring below 20",
    )
    rec_grade.add_argument(
        "--obsidian", action="store_true",
        help="Write Obsidian recruiter report to configured vault dir",
    )
    rec_grade.add_argument(
        "--no-analyze", action="store_true",
        help="Skip LLM-assisted refinement of borderline grades",
    )
    _add_root_flags(rec_grade)

    rec_reparse = rec_sub.add_parser(
        "reparse",
        help="Interactively act on a saved recruiter report (open/reply/trash)",
    )
    rec_reparse.add_argument("file", help="Path to the recruiter report markdown file")
    _add_root_flags(rec_reparse)

    # ── classifier ──
    p_clf = sub.add_parser(
        "classifier",
        help="Train and evaluate the message classifier",
    )
    clf_sub = p_clf.add_subparsers(
        dest="classifier_command", required=True, metavar="CLASSIFIER_COMMAND",
    )

    clf_train = clf_sub.add_parser(
        "train",
        help="Train a classifier (label messages with LLM if needed)",
    )
    clf_train.add_argument(
        "--in-labels", metavar="FILE", default=None,
        help="Use previously saved labels instead of labeling now",
    )
    clf_train.add_argument(
        "--out-labels", metavar="FILE", default=None,
        help="Save labels to this file before training (for review)",
    )
    clf_train.add_argument(
        "--days", type=int, default=None,
        help="Days to look back when harvesting messages to label",
    )
    src_t = clf_train.add_mutually_exclusive_group()
    src_t.add_argument("--only", type=parse_platform_csv, default=None, metavar="PLATFORMS")
    src_t.add_argument("--skip", type=parse_platform_csv, default=None, metavar="PLATFORMS")
    _add_root_flags(clf_train)

    clf_eval = clf_sub.add_parser(
        "eval",
        help="Evaluate classifier accuracy against labeled ground truth",
    )
    clf_eval.add_argument(
        "--labels", metavar="FILE", required=True,
        help="Path to labeled JSON file (created by `classifier train --out-labels`)",
    )
    clf_eval.add_argument(
        "--out-samples", metavar="DIR", default=None,
        help="Save eval samples to DIR for manual review (500 per stage)",
    )
    _add_root_flags(clf_eval)

    # ── serve ──
    p_serve = sub.add_parser(
        "serve",
        help="Run a local web server to review and approve events",
    )
    p_serve.add_argument(
        "--days", type=int, default=None,
        help="Days to look back (default: from DAYS_BACK env or 7)",
    )
    src_s = p_serve.add_mutually_exclusive_group()
    src_s.add_argument("--only", type=parse_platform_csv, default=None, metavar="PLATFORMS")
    src_s.add_argument("--skip", type=parse_platform_csv, default=None, metavar="PLATFORMS")
    _add_root_flags(p_serve)

    return parser
