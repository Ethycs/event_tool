"""CLI entry point and command dispatcher.

Routes parsed argparse args to the appropriate command function in
event_harvester.cli.commands. Each command function is async and
returns an exit code (0 success, 1 user error, 2 system error).

The bare invocation `event-harvester` (with no subcommand) is
normalized to `event-harvester harvest` BEFORE argparse runs, so
backward-compat one-shot calls like `event-harvester --days 14`
continue to work.
"""

import asyncio
import logging
import sys

from event_harvester.cli.commands.classifier import classifier_cmd
from event_harvester.cli.commands.harvest import harvest_cmd
from event_harvester.cli.commands.recruiters import recruiters_cmd
from event_harvester.cli.commands.serve import serve_cmd
from event_harvester.cli.commands.watch import watch_cmd
from event_harvester.cli.commands.web import web_cmd
from event_harvester.cli.parser import build_parser
from event_harvester.config import load_config

logger = logging.getLogger("event_harvester")

KNOWN_COMMANDS = frozenset({
    "harvest", "watch", "web", "recruiters", "classifier", "serve",
})

_HANDLERS = {
    "harvest": harvest_cmd,
    "watch": watch_cmd,
    "web": web_cmd,
    "recruiters": recruiters_cmd,
    "classifier": classifier_cmd,
    "serve": serve_cmd,
}


def _normalize_argv(argv: list[str]) -> list[str]:
    """Insert 'harvest' if no subcommand is present.

    This implements backward compatibility: `event-harvester --days 14`
    runs the harvest pipeline as if the user had typed
    `event-harvester harvest --days 14`. Bare invocation of
    `event-harvester` (no arguments) also defaults to harvest.

    Help and version flags pass through untouched so the top-level
    parser handles them.
    """
    if not argv:
        return ["harvest"]
    first = argv[0]
    if first in KNOWN_COMMANDS:
        return argv
    if first in ("-h", "--help", "--version"):
        return argv
    # Looks like flags only — bare invocation, default to harvest
    return ["harvest"] + argv


def _setup_logging(verbose: bool, quiet: bool = False) -> None:
    """Configure logging based on verbosity flags.

    `--verbose` → DEBUG
    `--quiet` → WARNING
    Default → INFO
    """
    # Fix encoding for Windows cp1252 terminals
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    if verbose:
        level = logging.DEBUG
    elif quiet:
        level = logging.WARNING
    else:
        level = logging.INFO
    logging.basicConfig(
        level=level,
        format="  %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )


async def main() -> int:
    """Parse args, route to the right command, return exit code."""
    argv = _normalize_argv(sys.argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)

    _setup_logging(args.verbose, args.quiet)

    cfg = load_config()

    handler = _HANDLERS.get(args.command)
    if handler is None:
        logger.error("Unknown command: %s", args.command)
        return 1

    try:
        return await handler(args, cfg) or 0
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130  # standard SIGINT exit code
    except Exception as e:
        logger.exception("Command %s failed: %s", args.command, e)
        return 2


def main_sync() -> None:
    """Synchronous wrapper for the entry point."""
    try:
        exit_code = asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        exit_code = 130
    finally:
        from event_harvester.llm import shutdown_local
        shutdown_local()
    sys.exit(exit_code)
