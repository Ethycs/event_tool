"""Per-task log tagging via contextvars.

Lets concurrent harvest tasks (Discord, Telegram, etc.) attach a short
label to every log record they emit, so the UI log panel can prefix
lines like ``[discord] foo bar`` even when sources run in parallel.

The contextvar propagates naturally:
  * across ``await`` boundaries (asyncio copies the context),
  * into ``asyncio.to_thread`` calls (the executor thread inherits
    the copied context).
"""

from __future__ import annotations

import contextlib
import contextvars

task_tag: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "task_tag", default=None,
)


@contextlib.contextmanager
def tag_task(name: str):
    """Set ``task_tag`` to ``name`` for the duration of the ``with`` block."""
    token = task_tag.set(name)
    try:
        yield
    finally:
        task_tag.reset(token)
