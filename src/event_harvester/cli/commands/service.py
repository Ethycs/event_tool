"""Service command — run the events site as a Windows service via NSSM."""

import logging

from event_harvester.observability.tracing import get_tracer
from event_harvester.ui.display import BOLD, DIM, GREEN, RED, RESET, YELLOW

logger = logging.getLogger("event_harvester")
tracer = get_tracer("event_harvester.cli.commands.service")


async def service_cmd(args, cfg) -> int:
    """Dispatch to the appropriate service subcommand."""
    from event_harvester.servers import service as svc

    sub = args.service_command
    with tracer.start_as_current_span(f"cli.service.{sub}") as span:
        span.set_attribute("cli.subcommand", sub)
        try:
            if sub == "install":
                return _install(args, svc)
            if sub == "uninstall":
                svc.uninstall(name=args.name)
                print(f"\n  {GREEN}Removed{RESET} {BOLD}{args.name}{RESET}\n")
                return 0
            if sub in ("start", "stop", "restart"):
                svc.control(sub, name=args.name)
                print(f"\n  {GREEN}{sub}ed{RESET} {BOLD}{args.name}{RESET}\n")
                return 0
            if sub == "status":
                return _status(args, svc)
            if sub == "logs":
                return _logs(args, svc)
        except svc.ServiceError as e:
            span.set_attribute("error.message", str(e))
            print(f"\n  {RED}{e}{RESET}\n")
            return 1

        logger.error("Unknown service subcommand: %s", sub)
        return 1


def _install(args, svc) -> int:
    info = svc.install(
        name=args.name,
        host=args.host,
        port=args.port,
        interval_hours=args.interval_hours,
    )
    print(f"\n  {GREEN}Installed{RESET} {BOLD}{info['name']}{RESET} -> {info['url']}")
    print(f"  {DIM}logs: {info['logs']}{RESET}\n")

    # The single most important thing on this screen. Under LocalSystem the
    # Discord cache, Signal DB, Gmail token and browser profile all resolve to
    # a different profile directory, so those sources go quiet rather than
    # failing loudly — a harvest that looks fine with four fewer sources.
    print(f"  {YELLOW}Runs as LocalSystem until you change it.{RESET}")
    print("  Discord, Signal, Gmail and the browser profile live in your user")
    print("  profile and will be invisible to LocalSystem. Set the account:\n")
    print(f"    {BOLD}{info['account_command']}{RESET}\n")
    print(f"  Then start it:  {BOLD}event-harvester service start{RESET}")
    print(f"  {DIM}Not started automatically — the first run harvests every source "
          f"and spends LLM tokens.{RESET}\n")
    return 0


def _status(args, svc) -> int:
    info = svc.status(name=args.name)
    if not info.get("installed"):
        print(f"\n  {DIM}{args.name} is not installed.{RESET}")
        print(f"  {DIM}Install it: event-harvester service install{RESET}\n")
        return 1

    state = info["state"]
    colour = GREEN if "RUNNING" in state.upper() else YELLOW
    print(f"\n  {BOLD}{info['name']}{RESET}  {colour}{state}{RESET}")
    print(f"  {DIM}runs as {RESET}{info['runs_as']}")
    if info.get("account_is_localsystem"):
        print(f"  {YELLOW}LocalSystem — user-scoped sources (Discord, Signal, Gmail, "
              f"browser profile) will be invisible.{RESET}")
    print(f"  {DIM}{info['application']} {info['arguments']}{RESET}")
    print(f"  {DIM}cwd  {info['directory']}{RESET}")
    print(f"  {DIM}logs {info['stdout_log']}{RESET}\n")
    return 0


def _logs(args, svc) -> int:
    info = svc.tail_logs(name=args.name, lines=args.lines)
    for key, label in (("stdout_log", "stdout"), ("stderr_log", "stderr")):
        lines = info.get(key) or []
        print(f"\n  {BOLD}{label}{RESET} {DIM}(last {len(lines)}){RESET}")
        if not lines:
            print(f"    {DIM}(empty){RESET}")
        for line in lines:
            print(f"    {line}")
    print()
    return 0
