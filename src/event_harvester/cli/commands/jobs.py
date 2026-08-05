"""Jobs command — inspect and control the harvest queue."""

import logging

from event_harvester.observability.tracing import get_tracer
from event_harvester.ui.display import BOLD, DIM, GREEN, RED, RESET, YELLOW

logger = logging.getLogger("event_harvester")
tracer = get_tracer("event_harvester.cli.commands.jobs")

_COLOUR = {
    "queued": DIM,
    "running": YELLOW,
    "done": GREEN,
    "failed": RED,
    "cancelled": DIM,
    "interrupted": RED,
}


async def jobs_cmd(args, cfg) -> int:
    from event_harvester import jobs as jobs_mod

    sub = args.jobs_command
    with tracer.start_as_current_span(f"cli.jobs.{sub}") as span:
        span.set_attribute("cli.subcommand", sub)
        if sub == "list":
            return _list(args, jobs_mod)
        if sub == "show":
            return _show(args, jobs_mod)
        if sub == "cancel":
            return _cancel(args, jobs_mod)
        if sub == "submit":
            return _submit(args, jobs_mod)
        if sub == "prune":
            removed = jobs_mod.prune()
            print(f"\n  Removed {BOLD}{removed}{RESET} old job(s)\n")
            return 0
    logger.error("Unknown jobs subcommand: %s", sub)
    return 1


def _line(job) -> str:
    colour = _COLOUR.get(job.state, "")
    extra = ""
    if job.state == "running" and (job.progress or {}).get("stage"):
        extra = f" {DIM}{job.progress['stage']}{RESET}"
    elif job.state == "done":
        extra = f" {DIM}{(job.result or {}).get('events', 0)} event(s){RESET}"
    elif job.state in ("failed", "interrupted"):
        extra = f" {DIM}{str((job.result or {}).get('error'))[:60]}{RESET}"
    coalesced = f" {DIM}+{job.coalesced}{RESET}" if job.coalesced else ""
    return (f"  {colour}{job.state:<11}{RESET} {BOLD}{job.id}{RESET} "
            f"{DIM}{job.submitted_by}{RESET}{coalesced}{extra}")


def _list(args, jobs_mod) -> int:
    summary = jobs_mod.queue_summary()
    listed = jobs_mod.list_jobs(state=args.state, limit=args.limit)

    print(f"\n  {BOLD}Queue{RESET}  {summary['queued']} waiting", end="")
    if summary["running"]:
        stage = summary["running_stage"] or "starting"
        print(f", running {BOLD}{summary['running']}{RESET} {DIM}({stage}){RESET}")
    else:
        print(f", {DIM}nothing running{RESET}")

    if not listed:
        print(f"  {DIM}No jobs.{RESET}\n")
        return 0
    print()
    for job in listed:
        print(_line(job))
    print()
    return 0


def _show(args, jobs_mod) -> int:
    job = jobs_mod.load(args.job_id)
    if job is None:
        print(f"\n  {RED}No job {args.job_id}{RESET}\n")
        return 1

    print(f"\n  {BOLD}{job.id}{RESET}  {_COLOUR.get(job.state, '')}{job.state}{RESET}")
    print(f"  {DIM}submitted by {job.submitted_by} at {job.submitted_at}{RESET}")
    if job.coalesced:
        print(f"  {DIM}{job.coalesced} identical request(s) joined this one{RESET}")
    if job.started_at:
        print(f"  {DIM}started  {job.started_at}{RESET}")
    if job.finished_at:
        print(f"  {DIM}finished {job.finished_at}{RESET}")
    print(f"  {DIM}request  {job.request or 'defaults'}{RESET}")
    if job.progress:
        print(f"  {DIM}progress {job.progress}{RESET}")
    if job.result:
        print(f"  {DIM}result   {job.result}{RESET}")
    if job.log_tail:
        print(f"\n  {BOLD}log{RESET} {DIM}(last {len(job.log_tail)}){RESET}")
        for line in job.log_tail[-args.lines:]:
            print(f"    {line}")
    print()
    return 0


def _cancel(args, jobs_mod) -> int:
    job = jobs_mod.request_cancel(args.job_id)
    if job is None:
        print(f"\n  {RED}No job {args.job_id}{RESET}\n")
        return 1
    if job.state == "cancelled":
        print(f"\n  {GREEN}Cancelled{RESET} {BOLD}{job.id}{RESET} "
              f"{DIM}(it had not started){RESET}\n")
    else:
        # Cancel is cooperative: the runner sees the flag at a stage boundary,
        # which during a long LLM extract can be a minute away. Say so.
        print(f"\n  {YELLOW}Stopping{RESET} {BOLD}{job.id}{RESET}")
        print(f"  {DIM}It stops at the next stage boundary, not instantly.{RESET}\n")
    return 0


def _submit(args, jobs_mod) -> int:
    from event_harvester.job_executor import build_request

    request = build_request(
        days=args.days, only=args.only, skip=args.skip,
        web_source=args.web_source, no_cooldown=args.no_cooldown or None,
    )
    try:
        job = jobs_mod.submit(request, submitted_by="cli")
    except jobs_mod.QueueFull as e:
        print(f"\n  {RED}{e}{RESET}\n")
        return 1

    if job.coalesced:
        print(f"\n  {YELLOW}Joined{RESET} an identical queued job "
              f"{BOLD}{job.id}{RESET}\n")
    else:
        print(f"\n  {GREEN}Queued{RESET} {BOLD}{job.id}{RESET}\n")
    print(f"  {DIM}The MCP service runs it. Watch: "
          f"event-harvester jobs show {job.id}{RESET}\n")
    return 0
