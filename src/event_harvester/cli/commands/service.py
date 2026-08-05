"""Service command — run the site, MCP backend, or dispatch gateway via NSSM."""

import logging

from event_harvester.observability.tracing import get_tracer
from event_harvester.ui.display import BOLD, DIM, GREEN, RED, RESET, YELLOW

logger = logging.getLogger("event_harvester")
tracer = get_tracer("event_harvester.cli.commands.service")


def _targets(args, svc) -> list[str]:
    """The service keys this invocation acts on, in the right order.

    `--all` installs and starts in dependency order and stops and removes in
    reverse, so the gateway is never left pointing at a backend that is going
    away underneath it.
    """
    if not getattr(args, "all", False):
        return [args.service]
    order = list(svc.SERVICE_ORDER)
    if args.service_command in ("stop", "uninstall"):
        order.reverse()
    return order


async def service_cmd(args, cfg) -> int:
    """Dispatch to the appropriate service subcommand."""
    from event_harvester.servers import service as svc

    sub = args.service_command
    with tracer.start_as_current_span(f"cli.service.{sub}") as span:
        span.set_attribute("cli.subcommand", sub)
        span.set_attribute("service.selected", args.service)
        span.set_attribute("service.all", bool(getattr(args, "all", False)))
        try:
            targets = _targets(args, svc)
            # `--name` renames one service; it cannot rename three.
            if len(targets) > 1 and args.name:
                print(f"\n  {RED}--name applies to a single service; "
                      f"drop --all or drop --name.{RESET}\n")
                return 1

            rc = 0
            for key in targets:
                rc |= _one(sub, key, args, svc)
            return rc
        except svc.ServiceError as e:
            span.set_attribute("error.message", str(e))
            print(f"\n  {RED}{e}{RESET}\n")
            return 1


def _one(sub: str, key: str, args, svc) -> int:
    if sub == "install":
        return _install(key, args, svc)
    if sub == "uninstall":
        info = svc.uninstall(service=key, name=args.name)
        print(f"\n  {GREEN}Removed{RESET} {BOLD}{info['name']}{RESET}\n")
        return 0
    if sub in ("start", "stop", "restart"):
        info = svc.control(sub, service=key, name=args.name)
        print(f"\n  {GREEN}{sub}ed{RESET} {BOLD}{info['name']}{RESET}\n")
        return 0
    if sub == "status":
        return _status(key, args, svc)
    if sub == "logs":
        return _logs(key, args, svc)
    logger.error("Unknown service subcommand: %s", sub)
    return 1


def _install(key: str, args, svc) -> int:
    info = svc.install(
        service=key,
        name=args.name,
        host=args.host,
        port=args.port,
        interval_hours=args.interval_hours,
        path=args.path,
        allow_any_host=args.allow_any_host,
        backend_url=args.backend_url,
    )
    print(f"\n  {GREEN}Installed{RESET} {BOLD}{info['name']}{RESET} -> {info['url']}")
    print(f"  {DIM}logs: {info['logs']}{RESET}")
    if info["depends_on"]:
        print(f"  {DIM}starts after: {', '.join(info['depends_on'])}{RESET}")
    print()

    # Under LocalSystem the Discord cache, Signal DB, Gmail token and browser
    # profile all resolve to a different profile directory, so those sources
    # go quiet rather than failing loudly — a harvest that looks fine with
    # four fewer sources. The gateway is a pure proxy and does not care.
    if info["needs_user_account"]:
        print(f"  {YELLOW}Runs as LocalSystem until you change it.{RESET}")
        print("  Discord, Signal, Gmail and the browser profile live in your user")
        print("  profile and will be invisible to LocalSystem. Set the account:\n")
        print(f"    {BOLD}{info['account_command']}{RESET}\n")
    else:
        print(f"  {DIM}Runs as LocalSystem, which is fine here — the gateway "
              f"proxies and reads no user credentials.{RESET}\n")

    if info["service"] == "gateway":
        _gateway_exposure_notice()

    print(f"  Then start it:  {BOLD}event-harvester service start "
          f"--service {info['service']}{RESET}")
    print(f"  {DIM}Not started automatically — the first run harvests every source "
          f"and spends LLM tokens.{RESET}\n")
    return 0


def _gateway_exposure_notice() -> None:
    """The gateway is the only service meant to face outward. Say what that costs."""
    from event_harvester.servers.gateway import auth_mode

    mode = auth_mode()
    print(f"  {BOLD}This is the endpoint a remote connector points at.{RESET}")
    print(f"  {DIM}auth: EH_GATEWAY_AUTH={mode}{RESET}")
    if mode == "none":
        print(f"  {YELLOW}With auth=none it will refuse to bind anything but "
              f"loopback.{RESET}")
        print("  For a Claude custom connector set an OAuth provider "
              "(github/google/...),")
        print("  because that flow is OAuth — a static token cannot be used there.")
    print(f"  {DIM}Reachability is separate: Claude connects inbound over public "
          f"HTTPS,{RESET}")
    print(f"  {DIM}so put cloudflared/ngrok/a reverse proxy in front. "
          f"See the Deploy Playbook §5c.{RESET}\n")


def _status(key: str, args, svc) -> int:
    info = svc.status(service=key, name=args.name)
    if not info.get("installed"):
        print(f"\n  {DIM}{info.get('name', key)} is not installed.{RESET}")
        print(f"  {DIM}Install it: event-harvester service install "
              f"--service {key}{RESET}\n")
        return 1

    state = info["state"]
    colour = GREEN if "RUNNING" in state.upper() else YELLOW
    print(f"\n  {BOLD}{info['name']}{RESET}  {colour}{state}{RESET}")
    print(f"  {DIM}runs as {RESET}{info['runs_as']}")
    if info.get("account_is_localsystem") and svc.spec_for(key).needs_user_account:
        print(f"  {YELLOW}LocalSystem — user-scoped sources (Discord, Signal, Gmail, "
              f"browser profile) will be invisible.{RESET}")
    print(f"  {DIM}{info['application']} {info['arguments']}{RESET}")
    print(f"  {DIM}cwd  {info['directory']}{RESET}")
    print(f"  {DIM}logs {info['stdout_log']}{RESET}\n")
    return 0


def _logs(key: str, args, svc) -> int:
    info = svc.tail_logs(service=key, name=args.name, lines=args.lines)
    for stream, label in (("stdout_log", "stdout"), ("stderr_log", "stderr")):
        lines = info.get(stream) or []
        print(f"\n  {BOLD}{info['name']} {label}{RESET} {DIM}(last {len(lines)}){RESET}")
        if not lines:
            print(f"    {DIM}(empty){RESET}")
        for line in lines:
            print(f"    {line}")
    print()
    return 0
