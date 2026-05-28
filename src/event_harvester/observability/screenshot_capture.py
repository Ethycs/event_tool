"""Per-step PNG screenshot capture + animated-GIF encoding for web traces.

Phase 2 of the web-nav debugging feature. Phase 1 records navigation events
as JSONL at ``data/.web_traces/<source>/<run-id>.jsonl``. This module adds
PNG capture per navigation step plus animated-GIF encoding for scroll
sequences in feed mode.

The :class:`ScreenshotCapture` instance is duck-typed against the
``page`` argument — only ``page.screenshot(path=str, **kwargs)`` and
optionally ``page.wait_for_timeout(int)`` are required, so this module
does not import Playwright.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ScreenshotCapture:
    """Write per-step PNG screenshots and assemble GIFs from them.

    ``out_dir`` is the per-run directory, e.g.
    ``data/.web_traces/luma/20260429T183211_a3f2/``. It is auto-created
    if missing.

    SPAs that fire dozens of ``framenavigated`` events during a single
    user action would otherwise stack 30s screenshot timeouts and stall
    the pipeline for minutes; ``max_snaps`` and the consecutive-failure
    circuit breaker keep that bounded.
    """

    DEFAULT_TIMEOUT_MS = 5000
    DEFAULT_MAX_SNAPS = 25
    CONSECUTIVE_FAILURE_LIMIT = 3

    def __init__(
        self,
        out_dir: Path,
        *,
        max_snaps: int = DEFAULT_MAX_SNAPS,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> None:
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.max_snaps = max_snaps
        self.timeout_ms = timeout_ms
        self._snap_count = 0
        self._consecutive_failures = 0
        self._disabled = False

    def snap(self, page: Any, step: int, settle_ms: int = 200) -> Path | None:
        """Take a screenshot for ``step`` after a short settle delay.

        Returns the path on success, ``None`` on cap-reached, circuit-broken,
        or any failure (logged at warning level). Screenshot failures must
        never crash the pipeline.
        """
        if self._disabled:
            return None
        if self._snap_count >= self.max_snaps:
            if not self._disabled:
                logger.info(
                    "screenshot cap reached for %s (%d snaps); skipping further captures",
                    self.out_dir.name, self.max_snaps,
                )
                self._disabled = True
            return None

        self._snap_count += 1
        target = self.out_dir / f"{step}.png"
        try:
            self._settle(page, settle_ms)
            page.screenshot(path=str(target), timeout=self.timeout_ms)
        except Exception as e:  # noqa: BLE001 — page is duck-typed
            logger.warning("screenshot snap step=%d failed: %s", step, e)
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.CONSECUTIVE_FAILURE_LIMIT:
                logger.warning(
                    "screenshot disabled for %s after %d consecutive failures",
                    self.out_dir.name, self._consecutive_failures,
                )
                self._disabled = True
            return None
        self._consecutive_failures = 0
        return target

    @staticmethod
    def _settle(page: Any, settle_ms: int) -> None:
        """Sleep ``settle_ms`` ms, preferring Playwright's wait_for_timeout."""
        wait = getattr(page, "wait_for_timeout", None)
        if callable(wait):
            wait(settle_ms)
        else:
            time.sleep(settle_ms / 1000)

    def gif_from_pngs(
        self, png_paths: list[Path], output: Path, fps: int = 2
    ) -> Path | None:
        """Assemble an animated GIF from a sequence of PNGs via Pillow.

        Returns ``output`` on success, ``None`` on empty input, missing
        Pillow, or any encoding failure. Parent dirs of ``output`` are
        auto-created.
        """
        if not png_paths:
            logger.debug("gif_from_pngs: empty input, skipping")
            return None

        try:
            from PIL import Image
        except ImportError:
            logger.warning(
                "Pillow not installed; install via 'pixi add --pypi pillow' "
                "to enable GIF capture"
            )
            return None

        output = Path(output)
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            first = Image.open(png_paths[0])
            rest = [Image.open(p) for p in png_paths[1:]]
            duration_ms = int(1000 / fps) if fps > 0 else 500
            first.save(
                output,
                format="GIF",
                save_all=True,
                append_images=rest,
                duration=duration_ms,
                loop=0,
                optimize=False,
            )
        except Exception as e:  # noqa: BLE001 — Pillow + IO failure modes
            logger.warning("gif_from_pngs failed for %s: %s", output, e)
            return None
        return output

    def close(self) -> None:
        """No-op reserved for future cleanup. Idempotent."""
        # Intentionally empty; safe to call repeatedly.
        return None
