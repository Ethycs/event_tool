"""Markdown report generator with TickTick deep links.

**DEPRECATED 2026-07-30.** Superseded by `outputs/html_report.py`, which is
self-contained, mobile-first, and forwardable — this report renders as a wall
of text on the device where it is actually read. `generate_report` still works
and emits a `DeprecationWarning`; `--report FILE.md` still selects it.

This does **not** deprecate `outputs/obsidian.py`. That writes Markdown into an
Obsidian vault, where Markdown is the native format rather than a rendering
choice.
"""

import warnings
from datetime import datetime, timezone

from event_harvester.observability.tracing import traced

# Re-exported for back-compat; the canonical home is outputs/deeplinks.py, so
# the replacement does not have to import from the module it replaces.
from event_harvester.outputs.deeplinks import ticktick_deep_link  # noqa: F401

_TRACER = "event_harvester.outputs.report"


@traced("report.generate", tracer_name=_TRACER)
def generate_report(
    validated_events: list[dict],
    raw_events: list[dict],
    links: list[dict],
    source_counts: dict[str, int],
    total_messages: int,
    output_path: str = "events_report.md",
) -> str:
    """Generate a markdown report with TickTick deep links.

    Uses validated_events if available, otherwise falls back to raw_events.
    Returns the output file path.

    .. deprecated:: 2026-07-30
       Use :func:`event_harvester.outputs.html_report.generate_html_report`.
    """
    warnings.warn(
        "Markdown reports are deprecated; use outputs.html_report."
        "generate_html_report (self-contained, mobile-first, forwardable). "
        "Pass --report FILE.md to keep using this.",
        DeprecationWarning,
        stacklevel=2,
    )
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    sources = " + ".join(
        name.capitalize()
        for name, count in source_counts.items()
        if count > 0
    )

    lines = [
        "# Event Harvester - Top Events",
        "",
        f"Generated: {now}",
        f"Sources: {sources} ({total_messages} messages)",
        "",
        "---",
        "",
    ]

    # Decide which events to render
    events = validated_events if validated_events else raw_events

    if events:
        lines.append("## Events & Scheduling")
        lines.append("")

        for i, ev in enumerate(events, 1):
            _append_event(lines, i, ev)

    if links:
        lines.append("## Top Links")
        lines.append("")
        for link in links[:15]:
            score = link.get("score", 0)
            url = link.get("url", "")
            author = link.get("author", "")
            lines.append(f"- **{score}** [{url}]({url}) — @{author}")
        lines.append("")

    if not events and not links:
        lines.append("No events or links found.")
        lines.append("")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return output_path


def _append_event(lines: list[str], index: int, ev: dict) -> None:
    """Append one event block to the markdown lines."""
    # Handle both validated (LLM-cleaned) and raw event formats
    title = ev.get("title") or ev.get("content", "Untitled")[:80]
    if len(title) > 80:
        title = title[:77] + "..."

    score = ev.get("score", 0)
    author = ev.get("author", "")
    channel = ev.get("source") or ev.get("channel", "")
    timestamp = ev.get("timestamp", "")
    details = ev.get("details", "")
    content_snippet = details or ev.get("content", "") or ev.get("original", "")
    if len(content_snippet) > 200:
        content_snippet = content_snippet[:197] + "..."

    # Tags
    tags = []
    if ev.get("pinned"):
        tags.append("`PIN`")
    if ev.get("scheduling"):
        tags.append("`SCHED`")
    tag_str = " ".join(tags)

    # Date info
    resolved_date = ev.get("date") or ev.get("best_date") or ""
    all_day = ev.get("all_day", False)
    dates = ev.get("dates", [])
    times = ev.get("times", [])

    lines.append(f"### {index}. {title}")
    lines.append("")

    score_line = f"- **Score**: {score}"
    if tag_str:
        score_line += f" {tag_str}"
    lines.append(score_line)

    lines.append(f"- **From**: @{author} in `{channel}`")
    lines.append(f"- **Posted**: {timestamp}")

    if dates:
        lines.append(f"- **Dates**: {', '.join(str(d) for d in dates)}")
    if times:
        lines.append(f"- **Times**: {', '.join(str(t) for t in times)}")

    if resolved_date:
        date_display = resolved_date
        if all_day:
            date_display += " (all day)"
        lines.append(f"- **Resolved date**: {date_display}")

    lines.append("")
    lines.append(f"> {content_snippet}")
    lines.append("")

    # TickTick deep link
    link_content = f"From @{author} in {channel}"
    deep_link = ticktick_deep_link(title, resolved_date, all_day, link_content)
    lines.append(f"[Add to TickTick]({deep_link})")
    lines.append("")
    lines.append("---")
    lines.append("")
