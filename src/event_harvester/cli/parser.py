"""Top-level argparse setup with subcommands.

Subcommand structure:
    event-harvester                              -> harvest (default, via _normalize_argv)
    event-harvester harvest [OPTIONS]
    event-harvester watch [--interval N]
    event-harvester web list|add URL|test URL|login
    event-harvester recruiters grade|reparse FILE
    event-harvester classifier train|eval --labels FILE
    event-harvester serve
    event-harvester mcp [--transport stdio|streamable-http]

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


def _add_days_back(p: argparse.ArgumentParser) -> None:
    """--days lookback (shared by harvest/serve/recruiters/classifier)."""
    p.add_argument(
        "--days", type=int, default=None,
        help="Days to look back (default: from DAYS_BACK env or 7)",
    )


def _add_platforms_filter(p: argparse.ArgumentParser) -> None:
    """Mutually-exclusive --only/--skip platform filter (shared by harvest/watch/serve/classifier)."""
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


def _add_profile_flag(p: argparse.ArgumentParser) -> None:
    """--profile NAME (shared by harvest/watch/serve)."""
    p.add_argument(
        "--profile", metavar="NAME",
        help="Apply a named profile from data/profiles.json before per-flag overrides",
    )


def _add_debug_flags(p: argparse.ArgumentParser) -> None:
    """Web-trace + screenshot + OTel debug flags (shared by harvest/watch/serve)."""
    p.add_argument(
        "--debug-web-trace", action="store_true",
        help="Record web page navigation events to data/.web_traces/ (JSONL per source)",
    )
    p.add_argument(
        "--debug-web-screenshots", action="store_true",
        help="Capture per-step PNG screenshots + feed GIFs alongside the JSONL trace (implies --debug-web-trace)",
    )
    p.add_argument(
        "--debug-otel", action="store_true",
        help="Enable OpenTelemetry console span exporter (prints spans to stderr)",
    )


def _add_harvest_args(p: argparse.ArgumentParser) -> None:
    """Register the full set of options for the harvest subcommand."""
    _add_days_back(p)
    _add_platforms_filter(p)

    p.add_argument(
        "--cap", type=parse_cap_arg, action="append", default=[],
        metavar="KEY=VAL[,KEY=VAL]",
        help="Per-source caps, e.g. --cap discord=20,telegram=30 --cap total=100",
    )
    p.add_argument(
        "--group-by-source", action="store_true",
        help="(deprecated) Terminal display is always grouped by source. Kept for compatibility.",
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
        "--show-rejects", action="store_true",
        help="Show messages rejected at each pipeline stage (classifier, reranker, caps)",
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
        "--web-source", metavar="NAME",
        help="Only fetch this web source by name (e.g. erobay, luma)",
    )
    p.add_argument(
        "--no-cooldown", action="store_true",
        help="Ignore web source cooldowns (force re-fetch)",
    )
    _add_profile_flag(p)

    p.add_argument(
        "--report", nargs="?", const="events_report.html", default=None,
        metavar="FILE",
        help="Generate a report (default: events_report.html — self-contained, "
             "mobile-friendly, forwardable). Format follows the extension; "
             "a .md path selects the deprecated Markdown report",
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
    _add_debug_flags(p)


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
    _add_platforms_filter(p_watch)
    _add_profile_flag(p_watch)
    _add_debug_flags(p_watch)
    _add_root_flags(p_watch)

    # ── web ──
    p_web = sub.add_parser(
        "web",
        help="Manage web sources (list/add/test/probe/login)",
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
        choices=["calendar", "feed", "search", "raw", "ics"],
        help="Override the auto-detected mode (also skips the feed probe)",
    )
    web_add.add_argument(
        "--no-probe", action="store_true",
        help="Skip the browser-free calendar-feed probe and go straight to Playwright",
    )
    _add_root_flags(web_add)

    web_test = web_sub.add_parser(
        "test",
        help="Run diagnostics on a URL without saving",
    )
    web_test.add_argument("url", help="URL to test")
    web_test.add_argument(
        "--no-probe", action="store_true",
        help="Skip the browser-free calendar-feed probe and go straight to Playwright",
    )
    _add_root_flags(web_test)

    web_probe = web_sub.add_parser(
        "probe",
        help="Find iCalendar feeds for URLs (or every configured non-ICS source)",
    )
    web_probe.add_argument(
        "urls", nargs="*",
        help="URLs to probe; omit to probe every configured source not already in ics mode",
    )
    web_probe.add_argument(
        "--apply", action="store_true",
        help="Rewrite configured sources that have a feed to ics mode (no URL args only)",
    )
    web_probe.add_argument(
        "--timeout", type=int, default=10, metavar="SEC",
        help="Per-request timeout in seconds (default: 10)",
    )
    _add_root_flags(web_probe)

    web_replay = web_sub.add_parser(
        "replay",
        help="Replay a configured source's recorded route and report each step",
    )
    web_replay.add_argument("name", help="Name of a source in data/web_sources.json")
    web_replay.add_argument(
        "--diagnose", action="store_true",
        help="On failure, screenshot the page and ask a vision model what is blocking it",
    )
    _add_root_flags(web_replay)

    web_login = web_sub.add_parser(
        "login",
        help="Open a browser to log into web sites and save session state",
    )
    _add_root_flags(web_login)

    # ── site ──
    p_site = sub.add_parser(
        "site",
        help="Serve the events site and refresh it on a schedule",
    )
    p_site.add_argument("--host", default="127.0.0.1",
                        help="Bind address (default: 127.0.0.1 — loopback only, "
                             "because this server has no authentication)")
    p_site.add_argument("--port", type=int, default=8800, help="Port (default: 8800)")
    p_site.add_argument("--interval-hours", type=float, default=24.0, metavar="H",
                        help="Hours between scheduled harvests (default: 24)")
    p_site.add_argument("--no-schedule", action="store_true",
                        help="Serve without the background refresh loop")
    p_site.add_argument("--no-harvest-on-start", action="store_true",
                        help="Serve the cached events; do not harvest at startup")
    p_site.add_argument("--build-only", action="store_true",
                        help="Build the page and exit without serving")
    p_site.add_argument("--reload", action="store_true",
                        help="Dev: restart on source changes (uvicorn --reload). "
                             "Serves cached events and does not harvest per restart")
    p_site.add_argument("--harvest-on-reload", action="store_true",
                        help="With --reload, allow harvesting and scheduling anyway "
                             "(every file save will start a harvest)")
    _add_root_flags(p_site)

    # ── service ──
    p_svc = sub.add_parser(
        "service",
        help="Run the events site as a Windows service (NSSM)",
    )
    svc_sub = p_svc.add_subparsers(
        dest="service_command", required=True, metavar="SERVICE_COMMAND",
    )

    def _svc_common(sp):
        sp.add_argument("--name", default="EventHarvesterSite",
                        help="Service name (default: EventHarvesterSite)")
        _add_root_flags(sp)

    svc_install = svc_sub.add_parser(
        "install", help="Install the service (elevated). Does not start it",
    )
    svc_install.add_argument("--host", default="127.0.0.1", help="Bind address")
    svc_install.add_argument("--port", type=int, default=8800, help="Port")
    svc_install.add_argument("--interval-hours", type=float, default=24.0, metavar="H",
                             help="Hours between scheduled harvests (default: 24)")
    _svc_common(svc_install)

    for action, blurb in (
        ("uninstall", "Stop and remove the service (elevated)"),
        ("start", "Start the service (elevated)"),
        ("stop", "Stop the service (elevated)"),
        ("restart", "Restart the service (elevated)"),
        ("status", "Show state, account, and configuration"),
    ):
        _svc_common(svc_sub.add_parser(action, help=blurb))

    svc_logs = svc_sub.add_parser("logs", help="Tail the service's stdout/stderr logs")
    svc_logs.add_argument("--lines", type=int, default=40, help="Lines per stream")
    _svc_common(svc_logs)

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
    _add_days_back(rec_grade)
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
    _add_days_back(clf_train)
    _add_platforms_filter(clf_train)
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
    _add_days_back(p_serve)
    _add_platforms_filter(p_serve)
    _add_profile_flag(p_serve)
    _add_debug_flags(p_serve)
    _add_root_flags(p_serve)

    # ── MCP ──
    p_mcp = sub.add_parser(
        "mcp",
        help="Run the Model Context Protocol server",
        description="Expose Event Harvester tools and resources to MCP clients.",
    )
    p_mcp.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="MCP transport to use (default: stdio)",
    )
    p_mcp.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for streamable-http transport (default: 127.0.0.1)",
    )
    p_mcp.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Port for streamable-http transport (default: 8765)",
    )
    p_mcp.add_argument(
        "--path",
        default="/mcp",
        help="HTTP path for streamable-http transport (default: /mcp)",
    )
    p_mcp.add_argument(
        "--allow-any-host",
        action="store_true",
        help=(
            "Disable FastMCP's DNS-rebinding host-header check. Required when "
            "running behind a reverse proxy or tunnel (ngrok, Cloudflare, etc.) "
            "so requests with non-loopback Host headers aren't rejected with 421."
        ),
    )
    _add_root_flags(p_mcp)

    return parser
