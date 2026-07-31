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
from event_harvester.cli.commands.mcp import mcp_cmd
from event_harvester.cli.commands.recruiters import recruiters_cmd
from event_harvester.cli.commands.serve import serve_cmd
from event_harvester.cli.commands.site import site_cmd
from event_harvester.cli.commands.watch import watch_cmd
from event_harvester.cli.commands.web import web_cmd
from event_harvester.cli.parser import build_parser
from event_harvester.config import load_config

logger = logging.getLogger("event_harvester")

KNOWN_COMMANDS = frozenset({
    "harvest", "watch", "web", "recruiters", "classifier", "serve", "site", "mcp",
})

_HANDLERS = {
    "harvest": harvest_cmd,
    "watch": watch_cmd,
    "web": web_cmd,
    "recruiters": recruiters_cmd,
    "classifier": classifier_cmd,
    "serve": serve_cmd,
    "site": site_cmd,
    "mcp": mcp_cmd,
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

    # Initialize OpenTelemetry for commands that opt in via --debug-otel, or
    # globally if OTEL_EXPORTER_OTLP_ENDPOINT is set. This must run before any
    # harvest/analysis/serve code so the root spans get exported.
    #
    # Metrics are installed alongside traces, not instead of them: spans answer
    # "what happened in this run", metrics answer "what is happening across
    # runs" — and replay-rot, vision spend and step failure rates are all the
    # second kind of question.
    try:
        import os

        from event_harvester.observability.metrics import init_metrics
        from event_harvester.observability.tracing import init_tracing
        _otel_debug = getattr(args, "debug_otel", False)
        _otlp = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        init_tracing(debug=_otel_debug, otlp_endpoint=_otlp)
        init_metrics(debug=_otel_debug, otlp_endpoint=_otlp)
    except Exception as e:
        logger.warning("OpenTelemetry init failed: %s", e)

    # One root span per invocation. Without it every span the run produces is
    # its own orphan trace -- `harvest.discord` and the `llm.chat` calls it
    # caused end up unrelated in the backend, so you cannot ask "what did this
    # run do" or "how long did the whole thing take", only "what happened".
    from event_harvester.observability.metrics import count as _count
    from event_harvester.observability.metrics import shutdown_metrics
    from event_harvester.observability.tracing import get_tracer

    tracer = get_tracer("event_harvester.cli")
    try:
        return await _run_command(args, tracer)
    finally:
        # A CLI usually exits well inside the periodic reader's export interval,
        # so without an explicit flush the run's metrics are simply lost.
        _count("cli.invocations", command=str(args.command),
               description="CLI invocations by command")
        shutdown_metrics()


async def _run_command(args, tracer) -> int:
    with tracer.start_as_current_span(f"cli.{args.command}") as root:
        root.set_attribute("cli.command", args.command)
        for attr in ("web_command", "recruiters_command", "classifier_command"):
            sub = getattr(args, attr, None)
            if sub:
                root.set_attribute(f"cli.{attr}", sub)
        root.set_attribute("cli.verbose", bool(args.verbose))
        root.set_attribute("cli.quiet", bool(args.quiet))

        cfg = load_config()

        handler = _HANDLERS.get(args.command)
        if handler is None:
            root.set_attribute("error.message", f"unknown command {args.command}")
            root.set_attribute("cli.exit_code", 1)
            logger.error("Unknown command: %s", args.command)
            return 1

        try:
            code = await handler(args, cfg) or 0
            root.set_attribute("cli.exit_code", code)
            return code
        except KeyboardInterrupt:
            root.set_attribute("cli.exit_code", 130)
            root.set_attribute("cli.interrupted", True)
            print("\nStopped.")
            return 130  # standard SIGINT exit code
        except Exception as e:
            root.set_attribute("cli.exit_code", 2)
            root.set_attribute("error.message", str(e))
            root.record_exception(e)
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
