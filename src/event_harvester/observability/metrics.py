"""OpenTelemetry metrics infrastructure (soft-dependency).

The other half of `tracing.py`. Spans answer "what happened in this run";
metrics answer "what is happening across runs" — which is the question every
operational decision in this project actually turns on. *Did the meetup route
stop yielding?* *Are we paying for more vision calls than we used to?* *Which
step verb fails most?* A trace can only answer those by being read one at a
time.

Same contract as `tracing.py`, deliberately: if `opentelemetry` is missing or
the SDK fails to start, every instrument becomes a no-op and the pipeline runs
unchanged. Instrumentation must never be able to take down the thing it
measures.

Instruments are created lazily and cached by name, so a module can call
`counter("web.steps")` at call time without import-order coupling to
`init_metrics`. Recording into an instrument built before the provider was
installed is a no-op rather than an error, which is the failure mode that
matters here — `init_metrics` runs in `cli/dispatch.py` *after* module import.

Attribute discipline mirrors the tracing side: attach the few dimensions that
get grouped by (verb, source, ok, kind) and nothing unbounded. A metric keyed
by URL is a cardinality bomb, and the source list is 29 entries and growing.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("event_harvester.observability.metrics")

_OTEL_OK = False
_PROVIDER_INSTALLED = False
_ATTACHED_READER_TYPES: set[str] = set()
_INSTRUMENTS: dict[str, Any] = {}

try:
    from opentelemetry import metrics as _otel_metrics
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import (
        ConsoleMetricExporter,
        PeriodicExportingMetricReader,
    )
    from opentelemetry.sdk.resources import Resource

    _OTEL_OK = True
except Exception as e:  # pragma: no cover - exercised only without otel installed
    logger.warning("opentelemetry metrics not available; metrics are a no-op: %s", e)
    _OTEL_OK = False


class _NoOpInstrument:
    """Stands in for any instrument when OTel is absent or failed to start."""

    def add(self, *a, **kw) -> None:
        pass

    def record(self, *a, **kw) -> None:
        pass


_NOOP = _NoOpInstrument()


def init_metrics(debug: bool = False, otlp_endpoint: str | None = None,
                 console_interval_ms: int = 60000) -> None:
    """Install the global MeterProvider and attach readers.

    Mirrors `tracing.init_tracing`, including its idempotency: the first call
    installs the provider, later calls only attach readers that are not already
    attached. Any failure is logged at WARNING and swallowed.

    Unlike spans, metrics are *pull-on-a-timer* — a `PeriodicExportingMetricReader`
    spawns a background thread and flushes on an interval. For a CLI process that
    exits in seconds, that thread would often never fire, so the console reader
    uses a long interval and relies on the shutdown flush instead of pretending
    to stream.
    """
    global _PROVIDER_INSTALLED

    if not _OTEL_OK:
        return

    readers = []
    try:
        if debug and "console" not in _ATTACHED_READER_TYPES:
            readers.append(PeriodicExportingMetricReader(
                ConsoleMetricExporter(),
                export_interval_millis=console_interval_ms,
            ))
            _ATTACHED_READER_TYPES.add("console")

        if otlp_endpoint and "otlp" not in _ATTACHED_READER_TYPES:
            try:
                from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
                    OTLPMetricExporter,
                )

                readers.append(PeriodicExportingMetricReader(
                    OTLPMetricExporter(endpoint=otlp_endpoint, insecure=True),
                ))
                _ATTACHED_READER_TYPES.add("otlp")
            except Exception as e:
                logger.warning("OTLP metric exporter unavailable (%s); skipping.", e)

        if not readers:
            return

        if _PROVIDER_INSTALLED:
            # The SDK binds readers at provider construction, so a second
            # provider would orphan the instruments already handed out. Say so
            # rather than silently producing a meter nothing reads.
            logger.warning(
                "init_metrics: a MeterProvider is already installed; "
                "readers %s were not attached", sorted(_ATTACHED_READER_TYPES),
            )
            return

        resource = Resource.create({"service.name": "event_harvester"})
        _otel_metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=readers))
        _PROVIDER_INSTALLED = True
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("init_metrics failed: %s", e)


def get_meter(name: str = "event_harvester"):
    if not _OTEL_OK:
        return None
    try:
        return _otel_metrics.get_meter(name)
    except Exception:  # pragma: no cover - defensive
        return None


def _instrument(kind: str, name: str, unit: str, description: str):
    """Create-or-fetch one instrument, cached by (kind, name)."""
    key = f"{kind}:{name}"
    cached = _INSTRUMENTS.get(key)
    if cached is not None:
        return cached

    meter = get_meter()
    if meter is None:
        _INSTRUMENTS[key] = _NOOP
        return _NOOP

    try:
        factory = {
            "counter": meter.create_counter,
            "histogram": meter.create_histogram,
            "updown": meter.create_up_down_counter,
        }[kind]
        made = factory(name, unit=unit, description=description)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("could not create %s %s: %s", kind, name, e)
        made = _NOOP
    _INSTRUMENTS[key] = made
    return made


def count(name: str, value: int = 1, *, unit: str = "1", description: str = "",
          **attrs: Any) -> None:
    """Increment a counter. Silently does nothing when metrics are off."""
    try:
        _instrument("counter", name, unit, description).add(
            value, {k: v for k, v in attrs.items() if v is not None},
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("count(%s) failed: %s", name, e)


def record(name: str, value: float, *, unit: str = "1", description: str = "",
           **attrs: Any) -> None:
    """Record a histogram value (durations, yields, counts-per-operation)."""
    try:
        _instrument("histogram", name, unit, description).record(
            value, {k: v for k, v in attrs.items() if v is not None},
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("record(%s) failed: %s", name, e)


def add_to_gauge(name: str, delta: int, *, unit: str = "1", description: str = "",
                 **attrs: Any) -> None:
    """Move an up-down counter — for things that go up and come back down.

    Open explore sessions are the motivating case: a plain counter would only
    ever tell you how many were opened, never how many are still holding a
    browser open right now.
    """
    try:
        _instrument("updown", name, unit, description).add(
            delta, {k: v for k, v in attrs.items() if v is not None},
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("add_to_gauge(%s) failed: %s", name, e)


def shutdown_metrics(timeout_ms: int = 5000) -> None:
    """Flush and stop the provider. Safe to call when metrics never started.

    A short-lived CLI would otherwise exit before the periodic reader's first
    interval and export nothing at all.
    """
    if not _OTEL_OK or not _PROVIDER_INSTALLED:
        return
    try:
        provider = _otel_metrics.get_meter_provider()
        force_flush = getattr(provider, "force_flush", None)
        if callable(force_flush):
            force_flush(timeout_millis=timeout_ms)
        shutdown = getattr(provider, "shutdown", None)
        if callable(shutdown):
            shutdown()
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("shutdown_metrics failed: %s", e)


def _autoinit_from_env() -> None:
    """Convenience hook mirroring ``tracing._autoinit_from_env``."""
    debug = os.environ.get("OTEL_DEBUG_CONSOLE", "").lower() in {"1", "true", "yes"}
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if debug or endpoint:
        init_metrics(debug=debug, otlp_endpoint=endpoint)
