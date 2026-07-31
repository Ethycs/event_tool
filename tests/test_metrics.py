"""Tests for ``event_harvester.observability.metrics`` and the span-status half
of ``tracing``.

Follows the house pattern from ``test_tracing.py``: attach an in-memory
exporter/reader to the process-global provider rather than standing up a
collector. These measure *effects* — that a metric point exists with the right
attributes, that a span really carries ERROR status — not that a function was
called.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from event_harvester.observability import metrics as metrics_mod
from event_harvester.observability import tracing as tracing_mod


@pytest.fixture(scope="module")
def reader():
    """One in-memory reader, installed once for the whole module.

    OTel refuses to replace a MeterProvider once set ("Overriding of current
    MeterProvider is not allowed") — it logs a warning and keeps the original,
    so a per-test provider would leave every reader after the first orphaned
    and silently empty. Hence module scope, and hence the assertions below
    filter by attribute rather than assuming a metric was untouched: readings
    are cumulative across the tests in this file.
    """
    from opentelemetry import metrics as otel_metrics
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    rdr = InMemoryMetricReader()
    otel_metrics.set_meter_provider(MeterProvider(metric_readers=[rdr]))
    metrics_mod._INSTRUMENTS.clear()
    metrics_mod._PROVIDER_INSTALLED = True
    return rdr


def _points(rdr, name: str):
    out = []
    data = rdr.get_metrics_data()
    if data is None:
        return out
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name != name:
                    continue
                out.extend(m.data.data_points)
    return out


class TestInstrumentsRecord:
    def test_a_counter_lands_with_its_attributes(self, reader):
        metrics_mod.count("test.counter", 2, verb="click", ok=False)
        points = _points(reader, "test.counter")
        assert len(points) == 1
        assert points[0].value == 2
        assert dict(points[0].attributes) == {"verb": "click", "ok": False}

    def test_a_histogram_records_a_distribution(self, reader):
        for value in (10, 20, 30):
            metrics_mod.record("test.hist", value, unit="ms", verb="scroll")
        point = _points(reader, "test.hist")[0]
        assert point.count == 3
        assert point.sum == 60

    def test_an_up_down_counter_comes_back_down(self, reader):
        """A plain counter could never say how many are open *right now*."""
        metrics_mod.add_to_gauge("test.open", 1)
        metrics_mod.add_to_gauge("test.open", 1)
        metrics_mod.add_to_gauge("test.open", -1)
        assert _points(reader, "test.open")[0].value == 1

    def test_none_attributes_are_dropped(self, reader):
        """Callers pass optional dimensions unconditionally; None is not a label."""
        metrics_mod.count("test.optional", source="luma", reason=None)
        assert dict(_points(reader, "test.optional")[0].attributes) == {"source": "luma"}

    def test_instruments_are_cached_not_rebuilt(self, reader):
        metrics_mod.count("test.cached")
        first = metrics_mod._INSTRUMENTS["counter:test.cached"]
        metrics_mod.count("test.cached")
        assert metrics_mod._INSTRUMENTS["counter:test.cached"] is first


class TestMetricsNeverBreakTheCaller:
    """Instrumentation must never be able to take down what it measures."""

    def test_recording_without_a_provider_is_a_no_op(self):
        with patch.object(metrics_mod, "get_meter", return_value=None):
            metrics_mod.count("test.noprovider")
            metrics_mod.record("test.noprovider.h", 1.0)
            metrics_mod.add_to_gauge("test.noprovider.g", 1)
        assert isinstance(metrics_mod._INSTRUMENTS["counter:test.noprovider"],
                          metrics_mod._NoOpInstrument)

    def test_a_broken_instrument_does_not_raise(self):
        boom = type("Boom", (), {"add": lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))})()
        metrics_mod._INSTRUMENTS["counter:test.boom"] = boom
        metrics_mod.count("test.boom")  # must not raise
        metrics_mod._INSTRUMENTS.pop("counter:test.boom", None)

    def test_shutdown_is_safe_when_metrics_never_started(self):
        with patch.object(metrics_mod, "_PROVIDER_INSTALLED", False):
            metrics_mod.shutdown_metrics()

    def test_otel_absent_makes_everything_inert(self):
        with patch.object(metrics_mod, "_OTEL_OK", False):
            assert metrics_mod.get_meter() is None
            metrics_mod.init_metrics(debug=True)
            metrics_mod.shutdown_metrics()


class TestSpanStatus:
    """Recording an exception is not the same as marking the span failed.

    A span that raised but whose status stays UNSET reads as successful to every
    backend — error rates and alerting key off status, not off the presence of
    an exception event. That was the gap in the package-wide pass.
    """

    def _exporter(self):
        from opentelemetry import trace
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        tracing_mod.init_tracing(debug=False)
        exporter = InMemorySpanExporter()
        trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))
        return exporter

    def test_a_raising_function_produces_an_error_span(self):
        exporter = self._exporter()

        @tracing_mod.traced("test.raises", tracer_name="test.status")
        def boom():
            raise ValueError("nope")

        with pytest.raises(ValueError):
            boom()

        span = next(s for s in exporter.get_finished_spans() if s.name == "test.raises")
        assert span.status.status_code.name == "ERROR"
        assert span.attributes.get("error.type") == "ValueError"
        assert "nope" in span.attributes.get("error.message", "")

    def test_a_handled_failure_can_mark_the_span_without_raising(self):
        """The failure mode this codebase actually has: it returns, it does not raise."""
        exporter = self._exporter()

        @tracing_mod.traced("test.handled", tracer_name="test.status")
        def returns_false():
            tracing_mod.fail_current_span("target not found", error_type="step_fill")
            return False

        assert returns_false() is False
        span = next(s for s in exporter.get_finished_spans() if s.name == "test.handled")
        assert span.status.status_code.name == "ERROR"
        assert span.attributes.get("error.type") == "step_fill"

    def test_a_successful_span_is_not_marked_error(self):
        exporter = self._exporter()

        @tracing_mod.traced("test.fine", tracer_name="test.status")
        def fine():
            return 1

        fine()
        span = next(s for s in exporter.get_finished_spans() if s.name == "test.fine")
        assert span.status.status_code.name != "ERROR"

    def test_fail_current_span_without_a_span_is_a_no_op(self):
        tracing_mod.fail_current_span("nothing is listening")


class TestRouteTelemetry:
    """The end-to-end shape: one span per step, and a failed route says so."""

    def _exporter(self):
        from opentelemetry import trace
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        tracing_mod.init_tracing(debug=False)
        exporter = InMemorySpanExporter()
        trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))
        return exporter

    def test_each_step_gets_its_own_span(self, reader):
        """Without this a route is one flat span and only its total time is visible."""
        from event_harvester.sources import web_steps

        exporter = self._exporter()

        class P:
            def goto(self, url, **kw): pass
            def wait_for_timeout(self, ms): pass
            def evaluate(self, *a, **kw): return ""

        class U:
            def dismiss_cookies(self, page): return True
            def close_modal(self, page): return False

        with patch.object(web_steps.W, "extract_links", return_value=[{"url": "https://e/1"}]):
            web_steps.replay(U(), P(), [
                {"verb": "goto", "url": "https://example.com"},
                {"verb": "dismiss"},
                {"verb": "extract", "what": "links"},
            ], source="demo")

        step_spans = [s for s in exporter.get_finished_spans() if s.name.startswith("web.step.")]
        assert [s.name for s in step_spans] == [
            "web.step.goto", "web.step.dismiss", "web.step.extract",
        ]
        assert step_spans[0].attributes["step.index"] == 0
        assert step_spans[2].attributes["step.links_total"] == 1

        executed = _points(reader, "web.steps.executed")
        verbs = {p.attributes["verb"] for p in executed}
        assert {"goto", "dismiss", "extract"} <= verbs
        assert all(p.attributes["source"] == "demo" for p in executed)

    def test_a_broken_route_marks_both_the_step_and_the_route(self, reader):
        from event_harvester.sources import web_steps

        exporter = self._exporter()

        class P:
            def wait_for_timeout(self, ms): pass
            def evaluate(self, *a, **kw): return ""

        class U:
            pass

        with patch.object(web_steps.W, "find_search_box", return_value=None):
            web_steps.replay(U(), P(), [{"verb": "fill", "target": "search", "text": "AI"}],
                             source="demo", diagnose_on_failure=False)

        by_name = {s.name: s for s in exporter.get_finished_spans()}
        assert by_name["web.step.fill"].status.status_code.name == "ERROR"
        assert by_name["web.route.replay"].status.status_code.name == "ERROR"
        assert by_name["web.route.replay"].attributes["route.failed_verb"] == "fill"

        # Yield is the signal replay-rot is eventually detected from.
        failed_yields = [p for p in _points(reader, "web.route.yield")
                         if p.attributes.get("ok") is False]
        assert failed_yields and failed_yields[0].sum == 0

    def test_the_target_kind_is_recorded_because_selectors_rot_first(self, reader):
        from event_harvester.sources import web_steps

        exporter = self._exporter()

        class P:
            def wait_for_timeout(self, ms): pass
            def click(self, sel): pass

        with patch.object(web_steps.W, "click", return_value=True):
            web_steps.replay(object(), P(),
                             [{"verb": "click", "target": "selector:.next"}], source="demo")

        span = next(s for s in exporter.get_finished_spans() if s.name == "web.step.click")
        assert span.attributes["step.target_kind"] == "selector"
