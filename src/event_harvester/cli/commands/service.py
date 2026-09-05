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
        # up/down/doctor act on the whole set, so they carry no --service.
        span.set_attribute("service.selected", getattr(args, "service", "*"))
        span.set_attribute("service.all", bool(getattr(args, "all", False)))
        try:
            if sub in ("up", "down", "doctor"):
                return _lifecycle(sub, args, svc)

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
        allow_insecure=getattr(args, "allow_insecure", False),
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
        _gateway_exposure_notice(
            host=args.host, allow_insecure=getattr(args, "allow_insecure", False),
        )

    print(f"  Then start it:  {BOLD}event-harvester service start "
          f"--service {info['service']}{RESET}")
    print(f"  {DIM}Not started automatically — the first run harvests every source "
          f"and spends LLM tokens.{RESET}\n")
    return 0


def _gateway_exposure_notice(*, host: str = "127.0.0.1", allow_insecure: bool = False) -> None:
    """The gateway is the only service meant to face outward. Say what that costs."""
    from event_harvester.servers.gateway import auth_mode, is_loopback

    mode = auth_mode()
    print(f"  {BOLD}This is the endpoint a remote connector points at.{RESET}")
    print(f"  {DIM}auth: EH_GATEWAY_AUTH={mode}{RESET}")
    if mode == "none" and not is_loopback(host) and allow_insecure:
        print(f"  {YELLOW}Binding {host} with auth=none and "
              f"EH_GATEWAY_ALLOW_INSECURE=1.{RESET}")
        print("  Every mesh peer that reaches this port gets harvest, approve, chat")
        print("  and the grok tools with no further auth — sound only if the network")
        print("  itself authenticates (a ZeroTier mesh). Not for the public internet.")
    elif mode == "none":
        print(f"  {YELLOW}With auth=none it will refuse to bind anything but "
              f"loopback.{RESET}")
        print("  For a mesh bind add --allow-insecure; for a Claude custom connector")
        print("  set an OAuth provider (github/google/...), because that flow is OAuth.")
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


# ── verified transitions ──────────────────────────────────────────────────


def _lifecycle(sub: str, args, svc) -> int:
    if sub == "down":
        return _down(args, svc)
    if sub == "up":
        return _up(args, svc)
    return _doctor(args, svc)


def _down(args, svc) -> int:
    info = svc.down()
    for name in info["stopped"]:
        print(f"  {GREEN}stopped{RESET} {BOLD}{name}{RESET}")
    for failure in info["failed"]:
        print(f"  {RED}failed {RESET} {BOLD}{failure['name']}{RESET} "
              f"{DIM}{failure['error']}{RESET}")

    survivors = info["unmanaged_survivors"]
    if survivors:
        # The whole reason `down` exists rather than `stop --all`: a process
        # nothing owns is invisible to the service manager and survives it.
        print(f"\n  {YELLOW}{len(survivors)} harvester process(es) survived — "
              f"no service owns them:{RESET}")
        for proc in survivors:
            print(f"    pid {BOLD}{proc['pid']}{RESET} {DIM}{proc['command'][:110]}{RESET}")
        print(f"\n  {DIM}Stop-Process -Id <pid> -Force{RESET}")
    else:
        print(f"\n  {GREEN}Nothing left running.{RESET}")
    print()
    return 0 if info["ok"] else 1


def _up(args, svc) -> int:
    info = svc.up(host=args.host)
    for name in info["started"]:
        print(f"  {GREEN}started{RESET} {BOLD}{name}{RESET}")
    for failure in info["failed"]:
        print(f"  {RED}failed {RESET} {BOLD}{failure['name']}{RESET} "
              f"{DIM}{failure['error']}{RESET}")

    print()
    for check in info["health"]:
        _print_health(check)

    # Say this *before* anything drains it. A job can sit queued for days and
    # then run a full unattended harvest the moment a runner appears.
    queue = info["queue_before"] or {}
    if queue.get("queued"):
        print(f"\n  {YELLOW}{queue['queued']} job(s) were already queued{RESET} — "
              f"the runner will start on them immediately.")
        for job_id in queue.get("next", []):
            print(f"    {DIM}{job_id}{RESET}")
        print(f"  {DIM}Cancel with: event-harvester jobs cancel <id>{RESET}")
    print()
    return 0 if info["ok"] else 1


def _print_health(check: dict) -> None:
    name = check["service"]
    if check["reachable"] is None:
        print(f"  {DIM}{name:<9} {check['detail']}{RESET}")
        return
    if not check["reachable"]:
        print(f"  {RED}{name:<9} not answering{RESET}")
        return
    if not check.get("sha"):
        # Reachable but silent about itself — it predates buildinfo.
        print(f"  {YELLOW}{name:<9} answering, but reports no identity "
              f"(pre-buildinfo build){RESET}")
        return
    flag = f" {RED}STALE{RESET}" if check.get("stale") else ""
    print(f"  {GREEN}{name:<9} ok{RESET} {DIM}v{check.get('version')} "
          f"@{check['sha']} pid={check.get('pid')}{RESET}{flag}")


def _doctor(args, svc) -> int:
    info = svc.doctor(host=args.host)

    print(f"\n  {BOLD}checked out{RESET} {DIM}{info['checked_out_sha']}{RESET}\n")
    for entry in info["services"]:
        if not entry["installed"]:
            print(f"  {DIM}{entry['service']:<9} not installed{RESET}")
            continue
        state = entry.get("state") or "?"
        colour = GREEN if "RUNNING" in state.upper() else YELLOW
        print(f"  {colour}{entry['service']:<9} {state}{RESET} "
              f"{DIM}as {entry.get('runs_as')}{RESET}")
        if entry.get("health"):
            print("  ", end="")
            _print_health(entry["health"])

    queue = info["queue"]
    print(f"\n  {BOLD}queue{RESET} {queue['queued']} waiting"
          f"{', running ' + queue['running'] if queue.get('running') else ''}")

    stdio = info.get("stdio_clients") or []
    if stdio:
        print(f"  {DIM}{len(stdio)} client-spawned stdio MCP server(s) — expected; "
              f"they bypass the queue{RESET}")

    if info["findings"]:
        print(f"\n  {YELLOW}{len(info['findings'])} finding(s){RESET}")
        for line in info["findings"]:
            print(f"    {YELLOW}·{RESET} {line}")
    else:
        print(f"\n  {GREEN}Nothing to report.{RESET}")
    print()
    return 0 if info["ok"] else 1
