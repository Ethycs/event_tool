"""Tests for ``event_harvester.screenshot_capture``."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from PIL import Image

from event_harvester.screenshot_capture import ScreenshotCapture


def _write_png(path: Path, color: tuple[int, int, int] = (255, 0, 0)) -> None:
    """Write a 1x1 PNG of ``color`` to ``path``."""
    img = Image.new("RGB", (1, 1), color)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG")


class FakePage:
    """Duck-typed stand-in for a Playwright Page."""

    def __init__(self, *, with_wait: bool = True, fail: bool = False) -> None:
        self.fail = fail
        self.wait_calls: list[int] = []
        self.screenshot_calls: list[str] = []
        if with_wait:
            self.wait_for_timeout = self._wait_for_timeout  # type: ignore[assignment]

    def _wait_for_timeout(self, ms: int) -> None:
        self.wait_calls.append(ms)

    def screenshot(self, path: str, **kwargs: object) -> None:
        self.screenshot_calls.append(path)
        if self.fail:
            raise RuntimeError("simulated screenshot failure")
        _write_png(Path(path))


def test_init_creates_directory(tmp_path: Path) -> None:
    out = tmp_path / "run_x"
    assert not out.exists()
    cap = ScreenshotCapture(out)
    assert out.is_dir()
    assert cap.out_dir == out


def test_snap_writes_png_and_returns_path(tmp_path: Path) -> None:
    cap = ScreenshotCapture(tmp_path)
    page = FakePage()
    result = cap.snap(page, step=1)
    assert result == tmp_path / "1.png"
    assert result.exists()
    # Round-trip the PNG to verify it's a real image.
    with Image.open(result) as img:
        assert img.size == (1, 1)


def test_snap_handles_screenshot_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cap = ScreenshotCapture(tmp_path)
    page = FakePage(fail=True)
    with caplog.at_level(logging.WARNING, logger="event_harvester.screenshot_capture"):
        result = cap.snap(page, step=7)
    assert result is None
    assert any("step=7" in rec.message for rec in caplog.records)


def test_snap_uses_settle_when_available(tmp_path: Path) -> None:
    cap = ScreenshotCapture(tmp_path)
    page = FakePage()
    cap.snap(page, step=1, settle_ms=123)
    assert page.wait_calls == [123]


def test_snap_falls_back_to_sleep_when_no_settle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap = ScreenshotCapture(tmp_path)
    page = FakePage(with_wait=False)
    sleeps: list[float] = []
    monkeypatch.setattr(
        "event_harvester.screenshot_capture.time.sleep",
        lambda s: sleeps.append(s),
    )
    result = cap.snap(page, step=2, settle_ms=250)
    assert result == tmp_path / "2.png"
    assert sleeps == [0.25]


def test_gif_from_pngs_writes_animated_gif(tmp_path: Path) -> None:
    cap = ScreenshotCapture(tmp_path)
    paths = []
    for i, color in enumerate([(255, 0, 0), (0, 255, 0), (0, 0, 255)], start=1):
        p = tmp_path / f"{i}.png"
        _write_png(p, color)
        paths.append(p)
    output = tmp_path / "out.gif"
    result = cap.gif_from_pngs(paths, output, fps=2)
    assert result == output
    assert output.exists()
    with Image.open(output) as gif:
        assert getattr(gif, "is_animated", False) is True
        assert gif.n_frames == 3


def test_gif_from_pngs_empty_input_returns_none(tmp_path: Path) -> None:
    cap = ScreenshotCapture(tmp_path)
    assert cap.gif_from_pngs([], tmp_path / "empty.gif") is None


def test_close_is_idempotent(tmp_path: Path) -> None:
    cap = ScreenshotCapture(tmp_path)
    cap.close()
    cap.close()


def test_snap_passes_timeout_kwarg(tmp_path: Path) -> None:
    cap = ScreenshotCapture(tmp_path, timeout_ms=1234)
    captured: list[dict] = []

    class TimeoutAwarePage(FakePage):
        def screenshot(self, path: str, **kwargs: object) -> None:
            captured.append(kwargs)
            _write_png(Path(path))

    cap.snap(TimeoutAwarePage(), step=1)
    assert captured == [{"timeout": 1234}]


def test_snap_max_cap_skips_further_captures(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cap = ScreenshotCapture(tmp_path, max_snaps=2)
    page = FakePage()
    assert cap.snap(page, step=1) is not None
    assert cap.snap(page, step=2) is not None
    with caplog.at_level(logging.INFO, logger="event_harvester.screenshot_capture"):
        assert cap.snap(page, step=3) is None
    assert cap.snap(page, step=4) is None
    # Page.screenshot was only called for the first two, not the skipped ones.
    assert len(page.screenshot_calls) == 2
    assert any("cap reached" in rec.message for rec in caplog.records)


def test_snap_disables_after_consecutive_failures(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cap = ScreenshotCapture(tmp_path)
    page = FakePage(fail=True)
    with caplog.at_level(logging.WARNING, logger="event_harvester.screenshot_capture"):
        for i in range(1, ScreenshotCapture.CONSECUTIVE_FAILURE_LIMIT + 1):
            assert cap.snap(page, step=i) is None
        # After the limit, further calls short-circuit without invoking page.screenshot.
        before = len(page.screenshot_calls)
        assert cap.snap(page, step=99) is None
        assert len(page.screenshot_calls) == before
    assert any("disabled" in rec.message for rec in caplog.records)


def test_snap_resets_failure_streak_on_success(tmp_path: Path) -> None:
    cap = ScreenshotCapture(tmp_path)
    failing = FakePage(fail=True)
    good = FakePage(fail=False)
    # Two failures, then a success, then two more failures: should NOT trip the circuit.
    cap.snap(failing, step=1)
    cap.snap(failing, step=2)
    assert cap.snap(good, step=3) is not None
    cap.snap(failing, step=4)
    cap.snap(failing, step=5)
    # Disabled only after CONSECUTIVE_FAILURE_LIMIT failures in a row, not cumulative.
    assert cap._disabled is False
