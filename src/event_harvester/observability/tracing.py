"""OpenTelemetry tracing infrastructure (soft-dependency).

Provides ``init_tracing``, ``get_tracer``, ``traced``, ``set_span_attrs``,
and ``current_span_event`` for the rest of the codebase. If
``opentelemetry`` isn't importable, every public function becomes a no-op so
the pipeline still runs.

Idempotency: ``init_tracing`` may be called more than once. The first
call installs the ``TracerProvider``; later calls only attach new
exporters that weren't already attached.

**Instrumenting new code**: reach for :func:`traced` rather than opening a
span by hand. The manual
``tracer.start_as_current_span(...) / set_attribute / record_exception``
block is ~8 lines of identical boilerplate per function, and the codebase
had already drifted into two spellings of it. The decorator handles sync,
async, and generator functions, and swallows nothing.

**What is instrumented**: every meaningful operation boundary — a stage, an
I/O call, an LLM request, a page interaction, a CLI or MCP entry point.
Deliberately *not* per-element helpers called inside tight loops
(``score_message``, ``_pattern_to_element``); a span per message would cost
more than it explains and would bury the spans that matter.
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Any

logger = logging.getLogger("event_harvester.observability.tracing")

_OTEL_OK = False
_PROVIDER_INSTALLED = False
_ATTACHED_EXPORTER_TYPES: set[str] = set()

try:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
        SimpleSpanProcessor,
    )
    from opentelemetry.trace import Status, StatusCode

    _OTEL_OK = True
except Exception as e:  # pragma: no cover - exercised only without otel installed
    logger.warning("opentelemetry not available; tracing is a no-op: %s", e)
    _OTEL_OK = False


class _NoOpSpan:
    """Minimal Span stand-in used when OTel isn't installed."""

    def set_attribute(self, *a, **kw) -> None:
        pass

    def add_event(self, *a, **kw) -> None:
        pass

    def record_exception(self, *a, **kw) -> None:
        pass

    def set_status(self, *a, **kw) -> None:
        pass

    def is_recording(self) -> bool:
        return False

    def __enter__(self) -> "_NoOpSpan":
        return self

    def __exit__(self, *a) -> None:
        return None


class _NoOpTracer:
    """Tracer stand-in whose ``start_as_current_span`` yields a no-op span."""

    @contextlib.contextmanager
    def start_as_current_span(self, name: str, *a, **kw):
        yield _NoOpSpan()


_NOOP_TRACER = _NoOpTracer()


def init_tracing(debug: bool = False, otlp_endpoint: str | None = None) -> None:
    """Install the global TracerProvider and attach exporters.

    - First call installs a ``TracerProvider`` if OTel is available.
    - ``debug=True`` attaches a ``ConsoleSpanExporter`` on a
      ``SimpleSpanProcessor``.
    - ``otlp_endpoint`` attaches an ``OTLPSpanExporter`` (gRPC, insecure)
      on a ``BatchSpanProcessor``.
    - Subsequent calls only add exporters that aren't already attached.

    Any failure is logged at WARNING and swallowed — the pipeline must
    keep running.
    """
    global _PROVIDER_INSTALLED

    if not _OTEL_OK:
        return

    try:
        if not _PROVIDER_INSTALLED:
            resource = Resource.create({"service.name": "event_harvester"})
            provider = TracerProvider(resource=resource)
            trace.set_tracer_provider(provider)
            _PROVIDER_INSTALLED = True
        else:
            provider = trace.get_tracer_provider()

        if debug and "console" not in _ATTACHED_EXPORTER_TYPES:
            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
            _ATTACHED_EXPORTER_TYPES.add("console")

        if otlp_endpoint and "otlp" not in _ATTACHED_EXPORTER_TYPES:
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                    OTLPSpanExporter,
                )

                exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
                provider.add_span_processor(BatchSpanProcessor(exporter))
                _ATTACHED_EXPORTER_TYPES.add("otlp")
            except Exception as e:
                logger.warning("OTLP exporter unavailable (%s); skipping.", e)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("init_tracing failed: %s", e)


def get_tracer(name: str = "event_harvester"):
    """Return a tracer (real OTel tracer if available, else a no-op)."""
    if not _OTEL_OK:
        return _NOOP_TRACER
    try:
        return trace.get_tracer(name)
    except Exception:  # pragma: no cover - defensive
        return _NOOP_TRACER


def set_span_attrs(**attrs: Any) -> None:
    """Set attributes on the current span, if one is recording.

    Lets a function body annotate the span its decorator opened without
    plumbing the span object through. ``None`` values are skipped so callers
    can pass optional fields unconditionally.
    """
    if not _OTEL_OK:
        return
    try:
        span = trace.get_current_span()
        if span is None:
            return
        is_rec = getattr(span, "is_recording", None)
        if callable(is_rec) and not is_rec():
            return
        for k, v in attrs.items():
            if v is not None:
                span.set_attribute(k, v)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("set_span_attrs failed: %s", e)


def _mark_error(span, exc: BaseException) -> None:
    """Record an exception on ``span`` **and** set the span's status to ERROR.

    Recording alone is not enough. A span that raised but whose status stays
    ``UNSET`` reads as successful to every backend — error rates, alerts and
    the red highlighting in a trace viewer all key off status, not off the
    presence of an exception event. This was the gap in the package-wide
    instrumentation pass: exceptions were recorded everywhere and no span was
    ever marked failed.
    """
    try:
        span.set_attribute("error.message", str(exc))
        span.set_attribute("error.type", type(exc).__name__)
        span.record_exception(exc)
        if _OTEL_OK:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("_mark_error failed: %s", e)


def fail_current_span(message: str, *, error_type: str = "handled") -> None:
    """Mark the current span as failed **without** an exception.

    For the failures this codebase handles rather than raises — a step that
    could not resolve its target, a route that stopped early, a token that
    would not refresh. Those return a value and keep going, so nothing would
    otherwise mark the span, and a trace of an unsuccessful run would look
    entirely green.
    """
    if not _OTEL_OK:
        return
    try:
        span = trace.get_current_span()
        if span is None:
            return
        is_rec = getattr(span, "is_recording", None)
        if callable(is_rec) and not is_rec():
            return
        span.set_attribute("error.message", message)
        span.set_attribute("error.type", error_type)
        span.set_status(Status(StatusCode.ERROR, message))
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("fail_current_span failed: %s", e)


def traced(
    name: str | None = None,
    *,
    tracer_name: str = "event_harvester",
    **static_attrs: Any,
):
    """Decorator: run the wrapped callable inside a span.

    Handles sync functions, async functions, and generators, so call sites
    don't have to care which they are. Exceptions are recorded on the span
    and re-raised — instrumentation must never change control flow.

    ``name`` defaults to ``"<module-tail>.<qualname>"``. Static attributes
    are attached at entry; use :func:`set_span_attrs` from inside the body
    for anything computed.

        @traced("llm.chat", tracer_name="event_harvester.llm")
        def chat_completion(messages, cfg):
            set_span_attrs(**{"llm.model": cfg.model})
            ...
    """
    import asyncio
    import functools
    import inspect

    def decorate(fn):
        span_name = name or f"{fn.__module__.rsplit('.', 1)[-1]}.{fn.__qualname__}"

        def _enter(tr):
            cm = tr.start_as_current_span(span_name)
            return cm

        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*a, **kw):
                tr = get_tracer(tracer_name)
                with _enter(tr) as span:
                    for k, v in static_attrs.items():
                        if v is not None:
                            span.set_attribute(k, v)
                    try:
                        return await fn(*a, **kw)
                    except Exception as e:
                        _mark_error(span, e)
                        raise

            return async_wrapper

        if inspect.isgeneratorfunction(fn):

            @functools.wraps(fn)
            def gen_wrapper(*a, **kw):
                tr = get_tracer(tracer_name)
                with _enter(tr) as span:
                    for k, v in static_attrs.items():
                        if v is not None:
                            span.set_attribute(k, v)
                    try:
                        yield from fn(*a, **kw)
                    except Exception as e:
                        _mark_error(span, e)
                        raise

            return gen_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*a, **kw):
            tr = get_tracer(tracer_name)
            with _enter(tr) as span:
                for k, v in static_attrs.items():
                    if v is not None:
                        span.set_attribute(k, v)
                try:
                    return fn(*a, **kw)
                except Exception as e:
                    _mark_error(span, e)
                    raise

        return sync_wrapper

    return decorate


def current_span_event(name: str, attributes: dict | None = None) -> None:
    """Add an event to the current span, if one is recording."""
    if not _OTEL_OK:
        return
    try:
        span = trace.get_current_span()
        if span is None:
            return
        # Some no-op span impls expose is_recording(); guard for it.
        is_rec = getattr(span, "is_recording", None)
        if callable(is_rec) and not is_rec():
            return
        span.add_event(name, attributes or {})
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("current_span_event(%s) failed: %s", name, e)


def _autoinit_from_env() -> None:
    """Optional convenience hook (unused by default).

    Callers may invoke this to auto-init from ``OTEL_*`` env vars without
    plumbing CLI flags. Kept private to avoid surprise side-effects on
    import.
    """
    debug = os.environ.get("OTEL_DEBUG_CONSOLE", "").lower() in {"1", "true", "yes"}
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if debug or endpoint:
        init_tracing(debug=debug, otlp_endpoint=endpoint)
