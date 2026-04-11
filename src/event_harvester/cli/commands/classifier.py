"""Classifier command — train and evaluate the message classifier."""

import json
import logging
from pathlib import Path

from event_harvester.cli.parse_helpers import resolve_platforms

logger = logging.getLogger("event_harvester")

W = 64


async def classifier_cmd(args, cfg) -> int:
    sub = args.classifier_command
    if sub == "train":
        return await _classifier_train(args, cfg)
    if sub == "eval":
        return _classifier_eval(args, cfg)
    logger.error("Unknown classifier subcommand: %s", sub)
    return 1


async def _classifier_train(args, cfg) -> int:
    """Label messages (or load existing labels) and train the classifier."""
    from event_harvester.classifier import train as train_classifier

    if args.days is not None:
        cfg.days_back = args.days

    # Either load pre-existing labels or harvest+label
    if args.in_labels:
        try:
            labeled = json.loads(Path(args.in_labels).read_text(encoding="utf-8"))
            print(f"Loaded {len(labeled)} labeled messages from {args.in_labels}")
        except Exception as e:
            logger.error("Failed to load labels: %s", e)
            return 1
    else:
        from event_harvester.harvest import harvest_messages
        from event_harvester.label import label_messages

        platform_kwargs = resolve_platforms(args.only, args.skip)
        all_messages = await harvest_messages(cfg, **platform_kwargs)
        if not all_messages:
            print("No messages found to label.")
            return 0

        print(f"\n{'=' * W}")
        print("[ Labeling messages with LLM ]")
        print(f"{'=' * W}\n")
        labeled = label_messages(all_messages, cfg.llm)

    if args.out_labels:
        Path(args.out_labels).write_text(
            json.dumps(labeled, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"Labels saved -> {args.out_labels}\n")

    # Train
    msgs_for_train = [{k: v for k, v in m.items() if k != "label"} for m in labeled]
    labels_for_train = [m["label"] for m in labeled]

    print(f"\n{'=' * W}")
    print("[ Training classifier ]")
    print(f"{'=' * W}\n")
    train_classifier(msgs_for_train, labels_for_train)
    return 0


def _classifier_eval(args, cfg) -> int:
    """Evaluate classifier accuracy against labeled ground truth."""
    from event_harvester.eval_classifier import run_eval

    run_eval(args.labels, save_eval=args.out_samples)
    return 0
