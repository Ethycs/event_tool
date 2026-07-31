"""App deep links for an event.

Lives here rather than in `report.py` because it is not a Markdown concern —
it was only ever there because Markdown was the first output to need it. With
Markdown deprecated (2026-07-30) the HTML report would otherwise import from
the module it replaces.
"""

from __future__ import annotations

from urllib.parse import quote


def ticktick_deep_link(
    title: str,
    start_date: str | None,
    all_day: bool,
    content: str,
) -> str:
    """Build a ticktick:// x-callback-url deep link."""
    params = {
        "title": title,
        "content": content,
    }

    if start_date:
        # Normalize to the format TickTick expects: 2026-03-21T14:00:00.000+0000
        date_str = start_date
        if len(date_str) == 10:  # YYYY-MM-DD only
            date_str += "T00:00:00.000+0000"
        elif "T" in date_str and "." not in date_str:
            date_str += ":00.000+0000" if date_str.count(":") == 1 else ".000+0000"
        params["startDate"] = date_str
        params["allDay"] = "true" if all_day else "false"

    query = "&".join(f"{k}={quote(v, safe='')}" for k, v in params.items())
    return f"ticktick://x-callback-url/v1/add_task?{query}"
