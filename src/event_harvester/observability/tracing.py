"""OpenTelemetry tracing infrastructure (soft-dependency).

Provides ``init_tracing``, ``get_tracer``, and ``current_span_event`` for
the rest of the codebase. If ``opentelemetry`` isn't importable, every
public function becomes a no-op so the pipeline still runs.

Idempotency: ``init_tracing`` may be called more than once. The first
call installs the ``TracerProvider``; later calls only attach new
exporters that weren't already attached.
"""

from __future__ import annotations

import contextlib
import logging
import os

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
