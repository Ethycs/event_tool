"""Record web navigation events during Playwright page sessions as JSONL.

One file per (source, run-id). Pure data recording — no screenshots,
no LLM calls. Feeds debugging output so the user can see what each web
source actually did during a run.

JSONL schema (one object per line)::

    {"ts": "...", "step": 1, "kind": "open", "url": "...", "parent": null}
    {"ts": "...", "step": 2, "kind": "framenavigated", "url": "...", "parent": 1}
    {"ts": "...", "step": 3, "kind": "popup", "url": "...", "parent": 2}
    {"ts": "...", "step": 4, "kind": "xhr", "url": "...", "parent": 1, "label": "..."}
    {"ts": "...", "step": 5, "kind": "close", "url": null, "parent": 3}

``parent`` references the ``step`` that triggered the event (null for
the initial open). ``kind`` is one of ``open`` | ``framenavigated`` |
``popup`` | ``xhr`` | ``close`` | ``error``. ``label`` is omitted when
None.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """UTC ISO-8601 with milliseconds and a ``Z`` suffix."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class NavigationTracer:
    """Append-only JSONL recorder for one (source, run-id) pair."""

    def __init__(
        self,
        source_name: str,
        run_id: str,
        out_dir: Path = Path("data/.web_traces"),
    ) -> None:
        self.source_name = source_name
        self.run_id = run_id
        out_dir = Path(out_dir)
        self.out_dir: Path = out_dir.resolve()
        source_dir = out_dir / source_name
        source_dir.mkdir(parents=True, exist_ok=True)
        self.path: Path = (source_dir / f"{run_id}.jsonl").resolve()
        # Touch the file so it exists even before any record is written.
        self.path.touch(exist_ok=True)
        self._step = 0
        self._closed = False

    def next_step(self) -> int:
        """Increment and return the next monotonic step id."""
        self._step += 1
        return self._step

    def record(
        self,
        kind: str,
        url: str | None = None,
        parent: int | None = None,
        label: str | None = None,
        image: str | None = None,
    ) -> int:
        """Write one JSONL record. Returns the assigned step id.

        Silently skips chrome-extension URLs (Chromium + UC-extension
        internals are noise). On write failure, logs a warning and
        continues.
        """
        if url and url.startswith("chrome-extension://"):
            # Return current step id without incrementing or writing.
            return self._step

        step = self.next_step()
        self._write_record(step, kind, url=url, parent=parent, label=label, image=image)
        return step

    def _write_record(
        self,
        step: int,
        kind: str,
        url: str | None = None,
        parent: int | None = None,
        label: str | None = None,
        image: str | None = None,
    ) -> None:
        """Write a JSONL row using an already-claimed step id.

        Internal helper — callers must have already incremented the
        step counter (or chosen to reuse one).
        """
        record: dict[str, Any] = {
            "ts": _now_iso(),
            "step": step,
            "kind": kind,
            "url": url,
            "parent": parent,
        }
        if label is not None:
            record["label"] = label
        if image is not None:
            record["image"] = image

        line = json.dumps(record, ensure_ascii=False) + "\n"
        wrote = False
        try:
            # Open-append-fsync-close per record so partial JSONL
            # survives crashes.
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                try:
                    import os
                    os.fsync(f.fileno())
                except OSError:
                    # fsync can fail on some filesystems; the flush
                    # already pushed bytes to the OS buffer.
                    pass
            wrote = True
        except OSError as e:
            logger.warning(
                "NavigationTracer failed to write to %s: %s", self.path, e
            )

        # After a successful JSONL write, also attach a span event to the
        # currently-active OTel span (if any). Lazy import keeps
        # navigation_trace independently importable even if the tracing
        # module fails to import. Tracing is purely additive: nav records
        # still appear in JSONL even with OTel off.
        if wrote:
            try:
                from event_harvester.tracing import current_span_event
                current_span_event(
                    kind,
                    {
                        "url": url or "",
                        "step": step or 0,
                        "parent": parent if parent is not None else "",
                    },
                )
            except Exception:
                pass  # tracing module unavailable; fail silent

    def record_with_screenshot(
        self,
        kind: str,
        url: str | None,
        page: Any,
        capture: Any,
        parent: int | None = None,
        label: str | None = None,
    ) -> int:
        """Claim a step, snap a screenshot, write the JSONL row with image ref.

        ``capture`` is duck-typed — it only needs a ``.snap(page, step)``
        method returning a ``Path | None``. If ``capture`` is ``None``,
        falls back to a plain :meth:`record` (supports the
        trace-without-screenshots case). If ``capture.snap()`` returns
        ``None`` (failure), still writes the trace row but without an
        ``image`` field.

        Returns the step id.
        """
        if capture is None:
            return self.record(kind, url=url, parent=parent, label=label)

        if url and url.startswith("chrome-extension://"):
            # Mirror record()'s filter: don't burn a step, don't snap.
            return self._step

        step = self.next_step()
        image_rel: str | None = None
        try:
            image_path = capture.snap(page, step)
        except Exception as e:  # noqa: BLE001 — capture is duck-typed
            logger.warning("ScreenshotCapture.snap raised: %s", e)
            image_path = None

        if image_path is not None:
            image_path = Path(image_path)
            # JSONL stores the path relative to out_dir so the value is
            # e.g. "<source>/<run-id>/<step>.png" — unambiguous when a
            # UI viewer aggregates multiple sources.
            try:
                image_rel = str(image_path.relative_to(self.out_dir))
            except ValueError:
                image_rel = str(image_path)

        self._write_record(
            step, kind, url=url, parent=parent, label=label, image=image_rel
        )
        return step

    def close(self) -> None:
        """Idempotent close. Reserved for future cleanup."""
        self._closed = True


def attach(tracer: NavigationTracer, page: Any, capture: Any = None) -> None:
    """Register ``framenavigated`` / ``popup`` / ``close`` listeners on ``page``.

    The ``page`` argument is duck-typed — only ``page.on(event_name,
    callback)`` is required. Chrome-extension URLs are filtered inside
    the handlers too (defense in depth).

    When ``capture`` is non-None, the framenavigated and popup handlers
    write a screenshot reference via :meth:`record_with_screenshot`
    instead of a plain :meth:`record`. ``close`` never snaps — the page
    is gone by the time the listener fires.
    """
    def _on_framenavigated(frame: Any) -> None:
        url = getattr(frame, "url", None)
        if url and url.startswith("chrome-extension://"):
            return
        if capture is not None:
            tracer.record_with_screenshot(
                "framenavigated", url=url, page=page, capture=capture,
            )
        else:
            tracer.record("framenavigated", url=url)

    def _on_popup(popup: Any) -> None:
        url = getattr(popup, "url", None)
        if url and url.startswith("chrome-extension://"):
            return
        if capture is not None:
            # Snap the popup itself, not the main page — the popup is
            # the newly-opened tab whose URL we're recording.
            tracer.record_with_screenshot(
                "popup", url=url, page=popup, capture=capture,
            )
        else:
            tracer.record("popup", url=url)

    def _on_close() -> None:
        tracer.record("close", url=None)

    page.on("framenavigated", _on_framenavigated)
    page.on("popup", _on_popup)
    page.on("close", _on_close)
