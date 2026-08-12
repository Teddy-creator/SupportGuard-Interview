from __future__ import annotations

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from supportguard.observability.tracing import extracted_context, inject_trace_context


def test_w3c_context_survives_outbox_carrier_round_trip() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    carrier: dict[str, str] = {}
    with tracer.start_as_current_span("command") as parent:
        inject_trace_context(carrier)
        parent_trace_id = parent.get_span_context().trace_id
    context = extracted_context(carrier["traceparent"])
    with tracer.start_as_current_span("worker", context=context) as child:
        assert child.get_span_context().trace_id == parent_trace_id

    spans = exporter.get_finished_spans()
    assert [span.name for span in spans] == ["command", "worker"]
