"""Tests for the OpenTelemetry tracing wiring.

Tests are written to run inside the ``dev`` pixi env where
``opentelemetry`` is installed. They use the SDK's
``InMemorySpanExporter`` to capture spans without standing up an
OTLP endpoint.
"""

from __future__ import annotations

import importlib

import pytest

from event_harvester.observability import tracing as tracing_mod


@pytest.fixture(autouse=True)
def reset_tracing_state():
    """Reset the module-level "already installed" flag between tests.

    The TracerProvider is process-global so we can't truly uninstall it,
    but we *can* reset our tracking flag so ``init_tracing`` will try to
    install again — which is the codepath we want to test for
    idempotency.
    """
    # Snapshot
    prev_installed = tracing_mod._PROVIDER_INSTALLED
    prev_attached = set(tracing_mod._ATTACHED_EXPORTER_TYPES)
    yield
    # Restore so other test modules aren't affected
    tracing_mod._PROVIDER_INSTALLED = prev_installed
    tracing_mod._ATTACHED_EXPORTER_TYPES.clear()
    tracing_mod._ATTACHED_EXPORTER_TYPES.update(prev_attached)


def test_init_tracing_idempotent():
    """init_tracing() with no args should be safe to call multiple times.

    The TracerProvider only gets installed once; subsequent calls are
    no-ops (no exception, no double-install).
    """
    tracing_mod.init_tracing()
    tracing_mod.init_tracing()
    tracing_mod.init_tracing(debug=False)
    # After three calls, we should have a working tracer.
    tracer = tracing_mod.get_tracer("test")
    assert tracer is not None
    # And the provider-installed flag should be sticky.
    assert tracing_mod._PROVIDER_INSTALLED is True


def test_span_captured_via_in_memory_exporter():
    """Manually attach an InMemorySpanExporter and verify a span
    started via our tracer is captured with the right name + attrs."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    tracing_mod.init_tracing(debug=False)
    provider = trace.get_tracer_provider()

    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    tracer = tracing_mod.get_tracer("test.capture")
    with tracer.start_as_current_span("test") as span:
        span.set_attribute("foo", "bar")
        span.set_attribute("count", 42)

    finished = exporter.get_finished_spans()
    assert len(finished) == 1
    s = finished[0]
    assert s.name == "test"
    assert s.attributes.get("foo") == "bar"
    assert s.attributes.get("count") == 42


def test_current_span_event_with_active_span():
    """current_span_event adds an event to the current span; with no
    current span it should silently no-op."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    tracing_mod.init_tracing(debug=False)
    provider = trace.get_tracer_provider()

    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    tracer = tracing_mod.get_tracer("test.events")

    # With a current span — event should attach.
    with tracer.start_as_current_span("outer"):
        tracing_mod.current_span_event("evt", {"k": "v"})

    finished = exporter.get_finished_spans()
    assert len(finished) >= 1
    last = finished[-1]
    assert last.name == "outer"
    assert len(last.events) == 1
    assert last.events[0].name == "evt"
    assert last.events[0].attributes.get("k") == "v"

    # Without a current span — should not raise.
    tracing_mod.current_span_event("orphan", {"k": "v"})


def test_fallback_when_otel_unavailable(monkeypatch):
    """Simulate the no-otel case by flipping the module's _OTEL_OK flag.

    Public API must remain callable without raising.
    """
    monkeypatch.setattr(tracing_mod, "_OTEL_OK", False)

    # init_tracing should be a no-op (no exception).
    tracing_mod.init_tracing(debug=True, otlp_endpoint="localhost:4317")

    # get_tracer should return the no-op tracer.
    tracer = tracing_mod.get_tracer("fallback")
    # The no-op tracer's start_as_current_span is a context manager
    # yielding a no-op span. None of these should raise.
    with tracer.start_as_current_span("noop") as span:
        span.set_attribute("k", "v")
        span.add_event("evt")
        span.record_exception(RuntimeError("boom"))
        assert span.is_recording() is False

    # current_span_event is a no-op.
    tracing_mod.current_span_event("evt", {"k": "v"})


def test_module_imports_cleanly():
    """The tracing module must always be importable — even on cold
    starts in environments where OTel might be missing. We just
    re-import it here as a smoke check."""
    importlib.reload(tracing_mod)
    assert hasattr(tracing_mod, "init_tracing")
    assert hasattr(tracing_mod, "get_tracer")
    assert hasattr(tracing_mod, "current_span_event")
