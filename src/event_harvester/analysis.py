"""OpenRouter client — structured task extraction from messages."""

import json
import logging

from openai import AsyncOpenAI

from event_harvester.config import OpenRouterConfig

logger = logging.getLogger("event_harvester.analysis")

TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "One-paragraph digest of the messages",
        },
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short, actionable task title (max 80 chars)",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Context: which chat/message prompted this task",
                    },
                    "priority": {
                        "type": "integer",
                        "enum": [0, 1, 3, 5],
                        "description": "0=none, 1=low, 3=medium, 5=high",
                    },
                    "due_in_days": {
                        "type": ["integer", "null"],
                        "description": "Days from today until due, or null if open-ended",
                    },
                },
                "required": ["title", "notes", "priority", "due_in_days"],
            },
        },
    },
    "required": ["summary", "tasks"],
}

PRIORITY_LABEL = {0: "none", 1: "low", 3: "medium", 5: "high"}


def build_prompt(messages: list[dict], days_back: int) -> str:
    """Build the analysis prompt from collected messages."""
    grouped: dict[str, list[dict]] = {}
    for msg in messages:
        key = f"{msg['platform'].capitalize()} / {msg['channel']}"
        grouped.setdefault(key, []).append(msg)

    lines = [
        f"Review {len(messages)} messages from the last {days_back} day(s) "
        f"across {len(grouped)} chat(s).\n",
        "Extract ONLY genuine action items — things someone actually needs to do, "
        "follow up on, or respond to. Ignore casual chat, announcements with no action, "
        "and already-resolved discussions. Be selective: 3 good tasks beats 10 weak ones.\n",
        "─── Messages ───\n",
    ]
    for chat, msgs in sorted(grouped.items()):
        lines.append(f"### {chat}  ({len(msgs)} messages)")
        for m in msgs[-60:]:
            ts = m["timestamp"][:16].replace("T", " ")
            content = m["content"][:400].replace("\n", " ")
            lines.append(f"  [{ts}] {m['author']}: {content}")
        lines.append("")
    return "\n".join(lines)


async def analyse_and_extract_tasks(
    messages: list[dict],
    days_back: int,
    cfg: OpenRouterConfig,
) -> tuple[str, list[dict]]:
    """Send messages to OpenRouter and return (summary, tasks)."""
    if not cfg.is_configured:
        logger.warning("OPENROUTER_API_KEY not set — skipping analysis.")
        return "", []

    client = AsyncOpenAI(
        api_key=cfg.api_key,
        base_url="https://openrouter.ai/api/v1",
        default_headers={"X-Title": "event_harvester"},
    )

    logger.info("Sending %d messages to %s ...", len(messages), cfg.model)

    resp = await client.chat.completions.create(
        model=cfg.model,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "task_extraction",
                "strict": True,
                "schema": TASK_SCHEMA,
            },
        },
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a personal assistant that reads messaging activity "
                    "and extracts genuine, actionable tasks. Return valid JSON only."
                ),
            },
            {"role": "user", "content": build_prompt(messages, days_back)},
        ],
        max_tokens=2048,
    )

    raw = resp.choices[0].message.content
    try:
        data = json.loads(raw)
        return data.get("summary", ""), data.get("tasks", [])
    except json.JSONDecodeError as e:
        logger.error("Failed to parse OpenRouter JSON response: %s", e)
        return raw, []
