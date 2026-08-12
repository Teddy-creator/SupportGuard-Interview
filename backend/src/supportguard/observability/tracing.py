from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from opentelemetry import propagate, trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind

from supportguard.config import Settings


def configure_tracing(*, service: str, settings: Settings) -> None:
    if not settings.otel_exporter_otlp_endpoint:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    exporter = OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)


def inject_trace_context(payload: dict[str, Any]) -> None:
    propagate.inject(payload)


def extracted_context(traceparent: str | None) -> Context:
    carrier: Mapping[str, str] = {"traceparent": traceparent} if traceparent else {}
    return propagate.extract(carrier)


def tracer() -> trace.Tracer:
    return trace.get_tracer("supportguard")


HTTP_SERVER = SpanKind.SERVER
PRODUCER = SpanKind.PRODUCER
CONSUMER = SpanKind.CONSUMER
