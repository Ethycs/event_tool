"""Auto-label messages as actionable/noise using litellm.

This module sends messages to an LLM in batches and parses back
A (actionable) or N (noise) labels for each message.
"""

import logging

from event_harvester.config import LLMConfig
from event_harvester.llm import _complete_litellm

logger = logging.getLogger("event_harvester.label")

_LABEL_SYSTEM_PROMPT = """\
You are classifying messages as EVENT or NOISE.

EVENT (E): a real-world event someone could physically attend or join virtually. \
Meetups, hackathons, happy hours, conferences, workshops, parties, interviews, \
scheduled calls/meetings with a specific date/time, webinars, career fairs, \
furmeets, bar nights, conventions, game nights, volunteer sessions.

NOISE (N): everything else. Group chat conversations, opinions, jokes, memes, \
newsletters, marketing, notifications, someone else's plans, general discussion, \
questions not about events, status updates, tech talk, political commentary, \
reactions, bot messages, news links.

A message is EVENT only if it contains or announces a specific gathering with \
a date, time, or location. "Let's meet up sometime" is NOISE. \
"Drinks at 8pm Friday at The Bar" is EVENT.

Output in INI format:

[Message.1]
label = E

[Message.2]
label = N

[Message.3]
label = E

Only output the INI sections, nothing else."""


def _build_label_prompt(messages: list[dict]) -> str:
    """Build a numbered list of messages for the LLM to label."""
    lines = []
    for i, m in enumerate(messages, 1):
        platform = m.get("platform", "?")
        author = m.get("author", "?")
        content = m.get("content", "")[:400].replace("\n", " ")
        lines.append(f"{i}. [{platform}] @{author}: {content}")
    return "\n".join(lines)


def label_messages(
    messages: list[dict],
    cfg: LLMConfig,
    batch_size: int = 20,
) -> list[dict]:
    """Label messages as actionable (1) or noise (0) using litellm.

    Args:
        messages: list of message dicts
        cfg: LLMConfig for the LLM backend
        batch_size: how many messages to send per LLM call

    Returns:
        List of message dicts with an added "label" field (1 or 0).
    """
    labeled: list[dict] = []
    total_batches = (len(messages) + batch_size - 1) // batch_size

    for i in range(0, len(messages), batch_size):
        batch = messages[i : i + batch_size]
        batch_num = i // batch_size + 1
        logger.info("Labeling batch %d/%d (%d messages)", batch_num, total_batches, len(batch))

        prompt = _build_label_prompt(batch)

        try:
            # Always use litellm for labeling — it's a one-time bulk operation
            # where speed and accuracy matter more than cost.
            raw = _complete_litellm(
                messages=[
                    {"role": "system", "content": _LABEL_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                cfg=cfg,
                max_tokens=512,
                response_format=None,
            )
        except Exception as e:
            logger.error("Labeling failed on batch %d: %s", batch_num, e)
            # Default to actionable for failed batches (safe fallback)
            for m in batch:
                labeled.append({**m, "label": 1})
            continue

        # Parse the response
        from event_harvester.analysis import _parse_llm_ini
        sections = _parse_llm_ini(raw)
        label_map: dict[int, int] = {}
        for section_name, fields in sections.items():
            # Extract number from "Message.1", "Message.2", etc.
            parts = section_name.split(".")
            if len(parts) >= 2:
                try:
                    idx = int(parts[-1]) - 1  # 1-indexed to 0-indexed
                    letter = fields.get("label", "E").strip().upper()
                    label_map[idx] = 1 if letter == "E" else 0
                except ValueError:
                    pass

        for j, m in enumerate(batch):
            label = label_map.get(j, 1)  # default to actionable if missing
            labeled.append({**m, "label": label})

    n_actionable = sum(1 for m in labeled if m["label"] == 1)
    n_noise = len(labeled) - n_actionable
    logger.info(
        "Labeling complete: %d actionable, %d noise out of %d total.",
        n_actionable, n_noise, len(labeled),
    )

    return labeled
