"""Extraction A/B command — measure whether a weaker model would do."""

import logging

from event_harvester.observability.tracing import get_tracer
from event_harvester.ui.display import RESET, YELLOW

logger = logging.getLogger("event_harvester")
tracer = get_tracer("event_harvester.cli.commands.eval_extraction")


async def eval_extraction_cmd(args, cfg) -> int:
    """Run both models over identical batches and print the comparison."""
    from event_harvester.training.eval_extraction import resolve_key, run_ab

    with tracer.start_as_current_span("cli.eval_extraction") as span:
        span.set_attribute("eval.baseline", args.baseline)
        span.set_attribute("eval.candidate", args.candidate)
        span.set_attribute("eval.batches", args.batches)

        key = resolve_key(args.api_key)
        candidate_key = resolve_key(args.candidate_api_key, "EH_EVAL_CANDIDATE_API_KEY")
        # Fall back to the configured key only when no endpoint override is in
        # play: a proxy's virtual key and a provider key are different things,
        # and silently sending the wrong one produces a 401 that reads like a
        # broken eval rather than a missing flag.
        if not key and not args.api_base:
            key = cfg.llm.api_key or None

        if args.api_base and not key:
            print(f"\n  {YELLOW}--api-base is set but no key was given. Pass "
                  f"--api-key or set EH_EVAL_API_KEY.{RESET}\n")
            return 2

        try:
            run_ab(
                args.baseline,
                args.candidate,
                batches=args.batches,
                batch_size=args.batch_size,
                days_back=cfg.days_back,
                api_base=args.api_base,
                api_key=key,
                candidate_api_base=args.candidate_api_base,
                candidate_api_key=candidate_key,
                save=args.save,
                cache_path=args.cache,
            )
        except RuntimeError as e:
            print(f"\n  {YELLOW}{e}{RESET}\n")
            return 1
        return 0
