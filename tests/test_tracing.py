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
    assert hasattr(tracing_mod, "traced")
    assert hasattr(tracing_mod, "set_span_attrs")


# ── @traced ───────────────────────────────────────────────────────────
#
# `traced` decorates ~60 functions across the package, including every MCP
# tool and every L1 web verb, so a defect in it is a defect everywhere. The
# properties that matter are: it returns what the function returns, it does
# not swallow exceptions, and it preserves the signature metadata FastMCP
# introspects to build tool schemas.


@pytest.fixture
def spans():
    """Capture spans emitted during a test."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    tracing_mod.init_tracing(debug=False)
    exporter = InMemorySpanExporter()
    trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


class TestTracedDecorator:
    def test_sync_returns_value_and_emits_span(self, spans):
        @tracing_mod.traced("t.sync", tracer_name="test.traced")
        def add(a, b):
            tracing_mod.set_span_attrs(**{"t.sum": a + b})
            return a + b

        assert add(2, 3) == 5
        s = [x for x in spans.get_finished_spans() if x.name == "t.sync"]
        assert len(s) == 1
        assert s[0].attributes.get("t.sum") == 5

    async def test_async_is_awaited_not_returned_as_coroutine(self, spans):
        """The async branch must await the function, not hand back a coroutine."""

        @tracing_mod.traced("t.async", tracer_name="test.traced")
        async def fetch(x):
            return x * 2

        assert await fetch(21) == 42
        assert any(x.name == "t.async" for x in spans.get_finished_spans())

    def test_generator_yields_all_items(self, spans):
        @tracing_mod.traced("t.gen", tracer_name="test.traced")
        def counter(n):
            yield from range(n)

        assert list(counter(4)) == [0, 1, 2, 3]
        assert any(x.name == "t.gen" for x in spans.get_finished_spans())

    def test_exception_propagates_and_is_recorded(self, spans):
        """Instrumentation must never change control flow."""

        @tracing_mod.traced("t.boom", tracer_name="test.traced")
        def boom():
            raise ValueError("kaboom")

        with pytest.raises(ValueError, match="kaboom"):
            boom()

        s = [x for x in spans.get_finished_spans() if x.name == "t.boom"]
        assert len(s) == 1
        assert s[0].attributes.get("error.message") == "kaboom"

    async def test_async_exception_propagates(self, spans):
        @tracing_mod.traced("t.aboom", tracer_name="test.traced")
        async def boom():
            raise RuntimeError("async kaboom")

        with pytest.raises(RuntimeError, match="async kaboom"):
            await boom()
        assert any(x.name == "t.aboom" for x in spans.get_finished_spans())

    def test_static_attributes_are_attached(self, spans):
        @tracing_mod.traced("t.static", tracer_name="test.traced", **{"t.kind": "demo"})
        def f():
            return None

        f()
        s = [x for x in spans.get_finished_spans() if x.name == "t.static"]
        assert s[0].attributes.get("t.kind") == "demo"

    def test_signature_metadata_survives(self):
        """FastMCP builds tool schemas by introspecting the decorated function.

        If `traced` dropped __annotations__ or replaced the signature with
        (*args, **kwargs), every MCP tool would register with an empty schema
        and clients would lose all parameter names.
        """
        import inspect

        @tracing_mod.traced("t.sig", tracer_name="test.traced")
        def tool(index: int, dry_run: bool = False) -> dict:
            """Docstring survives too."""
            return {}

        assert tool.__name__ == "tool"
        assert tool.__doc__ == "Docstring survives too."
        # This module sets `from __future__ import annotations`, so raw
        # __annotations__ values are strings (PEP 563). Resolve them the way
        # a schema builder does rather than comparing to the type object.
        assert inspect.get_annotations(tool, eval_str=True)["index"] is int
        params = inspect.signature(tool).parameters
        assert list(params) == ["index", "dry_run"]
        assert params["dry_run"].default is False

    def test_default_span_name_from_qualname(self, spans):
        @tracing_mod.traced(tracer_name="test.traced")
        def some_function():
            return 1

        some_function()
        names = [x.name for x in spans.get_finished_spans()]
        assert any(n.endswith("some_function") for n in names)

    def test_works_when_otel_unavailable(self, monkeypatch):
        """With no OTel installed the decorator must still be transparent."""
        monkeypatch.setattr(tracing_mod, "_OTEL_OK", False)

        @tracing_mod.traced("t.noop", tracer_name="test.traced")
        def f(x):
            tracing_mod.set_span_attrs(**{"ignored": x})
            return x + 1

        assert f(1) == 2

    def test_set_span_attrs_skips_none(self, spans):
        """None-valued attributes are dropped so callers can pass optionals."""

        @tracing_mod.traced("t.none", tracer_name="test.traced")
        def f():
            tracing_mod.set_span_attrs(**{"t.present": "yes", "t.absent": None})

        f()
        s = [x for x in spans.get_finished_spans() if x.name == "t.none"]
        assert s[0].attributes.get("t.present") == "yes"
        assert "t.absent" not in s[0].attributes
