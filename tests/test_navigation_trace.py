"""Tests for ``event_harvester.navigation_trace``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from event_harvester.navigation_trace import NavigationTracer, attach


def _read_lines(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_creates_directory_and_file_on_init(tmp_path: Path) -> None:
    tracer = NavigationTracer("luma", "abc", out_dir=tmp_path)
    expected = tmp_path / "luma" / "abc.jsonl"
    assert expected.exists()
    assert tracer.path == expected.resolve()


def test_records_monotonic_steps(tmp_path: Path) -> None:
    tracer = NavigationTracer("luma", "run1", out_dir=tmp_path)
    s1 = tracer.record("open", url="https://lu.ma/discover")
    s2 = tracer.record("framenavigated", url="https://lu.ma/discover/tech", parent=s1)
    s3 = tracer.record("popup", url="https://lu.ma/event/abc", parent=s2)

    assert (s1, s2, s3) == (1, 2, 3)

    rows = _read_lines(tracer.path)
    assert len(rows) == 3
    assert [r["step"] for r in rows] == [1, 2, 3]
    assert [r["kind"] for r in rows] == ["open", "framenavigated", "popup"]


def test_chrome_extension_filtered(tmp_path: Path) -> None:
    tracer = NavigationTracer("luma", "run1", out_dir=tmp_path)
    tracer.record("framenavigated", url="chrome-extension://abc/x.html")
    rows = _read_lines(tracer.path)
    assert rows == []


def test_parent_linkage_preserved(tmp_path: Path) -> None:
    tracer = NavigationTracer("luma", "run1", out_dir=tmp_path)
    tracer.record("open", url="https://lu.ma/discover")
    tracer.record("xhr", url="https://lu.ma/_api/timeline", parent=1, label="feed-scroll")

    rows = _read_lines(tracer.path)
    assert rows[1]["parent"] == 1
    assert rows[1]["label"] == "feed-scroll"


def test_close_is_idempotent(tmp_path: Path) -> None:
    tracer = NavigationTracer("luma", "run1", out_dir=tmp_path)
    tracer.close()
    tracer.close()  # should not raise


class _FakePage:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}

    def on(self, event: str, callback) -> None:  # noqa: ANN001 — duck-typed
        self.handlers[event] = callback


class _FakeFrame:
    def __init__(self, url: str) -> None:
        self.url = url


def test_attach_wires_page_listeners(tmp_path: Path) -> None:
    tracer = NavigationTracer("luma", "run1", out_dir=tmp_path)
    page = _FakePage()
    attach(tracer, page)

    assert set(page.handlers.keys()) == {"framenavigated", "popup", "close"}

    # Invoke the framenavigated handler.
    page.handlers["framenavigated"](_FakeFrame("https://example.com"))

    rows = _read_lines(tracer.path)
    assert len(rows) == 1
    assert rows[0]["kind"] == "framenavigated"
    assert rows[0]["url"] == "https://example.com"


def test_attach_popup_handler_records_popup(tmp_path: Path) -> None:
    tracer = NavigationTracer("luma", "run1", out_dir=tmp_path)
    page = _FakePage()
    attach(tracer, page)

    page.handlers["popup"](_FakeFrame("https://example.com/popup"))

    rows = _read_lines(tracer.path)
    assert any(r["kind"] == "popup" and r["url"] == "https://example.com/popup" for r in rows)


def test_omits_label_when_none(tmp_path: Path) -> None:
    tracer = NavigationTracer("luma", "run1", out_dir=tmp_path)
    tracer.record("open", url="https://lu.ma/discover")

    rows = _read_lines(tracer.path)
    assert len(rows) == 1
    assert "label" not in rows[0]


def test_attach_filters_chrome_extension_frame(tmp_path: Path) -> None:
    tracer = NavigationTracer("luma", "run1", out_dir=tmp_path)
    page = _FakePage()
    attach(tracer, page)

    page.handlers["framenavigated"](_FakeFrame("chrome-extension://abc/x.html"))
    assert _read_lines(tracer.path) == []


# --- image-reference extensions ------------------------------------------------


class _FakeCapture:
    """Duck-typed stand-in for ScreenshotCapture."""

    def __init__(self, result_factory) -> None:  # noqa: ANN001 — test helper
        self._result_factory = result_factory
        self.calls: list[tuple[object, int]] = []

    def snap(self, page, step: int):  # noqa: ANN001 — duck-typed
        self.calls.append((page, step))
        return self._result_factory(page, step)


def test_record_writes_image_field_when_provided(tmp_path: Path) -> None:
    tracer = NavigationTracer("luma", "run1", out_dir=tmp_path)
    tracer.record("framenavigated", url="https://lu.ma/x", image="luma/run/1.png")

    rows = _read_lines(tracer.path)
    assert len(rows) == 1
    assert rows[0]["image"] == "luma/run/1.png"


def test_record_omits_image_when_none(tmp_path: Path) -> None:
    tracer = NavigationTracer("luma", "run1", out_dir=tmp_path)
    tracer.record("open", url="https://lu.ma/x")

    rows = _read_lines(tracer.path)
    assert len(rows) == 1
    assert "image" not in rows[0]


def test_record_with_screenshot_writes_image_path(tmp_path: Path) -> None:
    tracer = NavigationTracer("luma", "run1", out_dir=tmp_path)
    # Screenshot landed at tmp_path/luma/run1/1.png — inside out_dir.
    shot_dir = tmp_path / "luma" / "run1"
    shot_dir.mkdir(parents=True, exist_ok=True)

    def _factory(page, step):  # noqa: ANN001 — test helper
        p = shot_dir / f"{step}.png"
        p.write_bytes(b"")
        return p

    fake_capture = _FakeCapture(_factory)
    fake_page = object()

    step = tracer.record_with_screenshot(
        "framenavigated", "https://x", fake_page, fake_capture
    )

    assert step == 1
    rows = _read_lines(tracer.path)
    assert len(rows) == 1
    # Relative path is computed against tracer.out_dir (which is tmp_path).
    expected = str((tmp_path / "luma" / "run1" / "1.png").resolve().relative_to(tmp_path.resolve()))
    assert rows[0]["image"] == expected
    assert fake_capture.calls == [(fake_page, 1)]


def test_record_with_screenshot_handles_snap_failure(tmp_path: Path) -> None:
    tracer = NavigationTracer("luma", "run1", out_dir=tmp_path)
    fake_capture = _FakeCapture(lambda page, step: None)
    fake_page = object()

    step = tracer.record_with_screenshot(
        "framenavigated", "https://x", fake_page, fake_capture
    )

    assert step == 1
    rows = _read_lines(tracer.path)
    assert len(rows) == 1
    assert "image" not in rows[0]


def test_record_with_screenshot_falls_back_when_capture_none(tmp_path: Path) -> None:
    tracer = NavigationTracer("luma", "run1", out_dir=tmp_path)
    fake_page = object()

    step = tracer.record_with_screenshot("open", "https://x", fake_page, capture=None)

    assert step == 1
    rows = _read_lines(tracer.path)
    assert len(rows) == 1
    assert "image" not in rows[0]
    assert rows[0]["kind"] == "open"


def test_attach_with_capture_records_screenshots(tmp_path: Path) -> None:
    """attach(tracer, page, capture=cap) routes framenavigated through
    record_with_screenshot so each row has an ``image`` field."""
    tracer = NavigationTracer("luma", "run1", out_dir=tmp_path)
    page = _FakePage()
    shot_dir = tmp_path / "luma" / "run1"
    shot_dir.mkdir(parents=True, exist_ok=True)

    def _factory(p, step):  # noqa: ANN001 — test helper
        img = shot_dir / f"{step}.png"
        img.write_bytes(b"")
        return img

    capture = _FakeCapture(_factory)
    attach(tracer, page, capture=capture)

    page.handlers["framenavigated"](_FakeFrame("https://example.com/a"))
    page.handlers["framenavigated"](_FakeFrame("https://example.com/b"))

    rows = _read_lines(tracer.path)
    assert len(rows) == 2
    assert all(r["kind"] == "framenavigated" for r in rows)
    # capture.snap was invoked for each framenavigated event
    assert len(capture.calls) == 2
    # Image paths are relative to tracer.out_dir
    for row, (_, step) in zip(rows, capture.calls):
        expected = str(
            (tmp_path / "luma" / "run1" / f"{step}.png").resolve()
            .relative_to(tmp_path.resolve())
        )
        assert row["image"] == expected


def test_attach_popup_uses_capture_when_provided(tmp_path: Path) -> None:
    """The popup handler should also write an image when capture is set,
    snapping the popup page (not the parent)."""
    tracer = NavigationTracer("luma", "run1", out_dir=tmp_path)
    page = _FakePage()
    shot_dir = tmp_path / "luma" / "run1"
    shot_dir.mkdir(parents=True, exist_ok=True)

    snapped_pages: list = []

    def _factory(p, step):  # noqa: ANN001 — test helper
        snapped_pages.append(p)
        img = shot_dir / f"{step}.png"
        img.write_bytes(b"")
        return img

    capture = _FakeCapture(_factory)
    attach(tracer, page, capture=capture)

    popup = _FakeFrame("https://example.com/popup")
    page.handlers["popup"](popup)

    rows = _read_lines(tracer.path)
    assert len(rows) == 1
    assert rows[0]["kind"] == "popup"
    assert "image" in rows[0]
    assert snapped_pages == [popup]


def test_step_counter_not_double_incremented(tmp_path: Path) -> None:
    tracer = NavigationTracer("luma", "run1", out_dir=tmp_path)
    shot_dir = tmp_path / "luma" / "run1"
    shot_dir.mkdir(parents=True, exist_ok=True)

    def _factory(page, step):  # noqa: ANN001 — test helper
        p = shot_dir / f"{step}.png"
        p.write_bytes(b"")
        return p

    fake_capture = _FakeCapture(_factory)
    fake_page = object()

    s1 = tracer.record_with_screenshot("open", "https://a", fake_page, fake_capture)
    s2 = tracer.record_with_screenshot(
        "framenavigated", "https://b", fake_page, fake_capture, parent=s1
    )

    assert (s1, s2) == (1, 2)
    rows = _read_lines(tracer.path)
    assert [r["step"] for r in rows] == [1, 2]


# --- OTel span-event integration ---------------------------------------------


def test_record_emits_span_event_when_span_active(tmp_path: Path) -> None:
    """When an OTel span is current, NavigationTracer.record adds a span event
    matching the JSONL row's kind/url/step/parent."""
    try:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )
    except ImportError:
        pytest.skip("opentelemetry not available")

    # Ensure our tracing module has a provider installed. It uses the
    # global trace.get_tracer_provider(); init_tracing() makes that real.
    from event_harvester import tracing as tracing_mod
    tracing_mod.init_tracing(debug=False)

    from opentelemetry import trace
    provider = trace.get_tracer_provider()

    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    test_tracer = trace.get_tracer("test.nav_record")

    tracer = NavigationTracer("luma", "spanrun", out_dir=tmp_path)
    with test_tracer.start_as_current_span("outer"):
        tracer.record(
            kind="framenavigated",
            url="https://example.com",
            parent=None,
        )

    spans = exporter.get_finished_spans()
    outer = [s for s in spans if s.name == "outer"]
    assert outer, "expected an 'outer' span to be recorded"
    events = list(outer[-1].events)
    nav_events = [e for e in events if e.name == "framenavigated"]
    assert nav_events, f"expected a framenavigated event, got: {[e.name for e in events]}"
    evt = nav_events[0]
    assert evt.attributes.get("url") == "https://example.com"
    assert evt.attributes.get("step") == 1


def test_record_silent_when_no_span_active(tmp_path: Path) -> None:
    """Without an active span, record() must still write JSONL and not raise."""
    tracer = NavigationTracer("luma", "nospan", out_dir=tmp_path)
    # No `with start_as_current_span(...)` around this call — just record.
    tracer.record(kind="open", url="https://example.com")
    rows = _read_lines(tracer.path)
    assert len(rows) == 1
    assert rows[0]["kind"] == "open"
