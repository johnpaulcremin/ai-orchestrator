from __future__ import annotations

import os
from typing import Any

from opentelemetry import trace

_configured = False


def otel_enabled() -> bool:
    """Tracing is opt-in: only active when an OTLP endpoint is configured."""
    return bool((os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip())


def setup_tracing(app: Any) -> bool:
    """
    Configure OpenTelemetry tracing and instrument the FastAPI app.

    No-op (returns False) unless OTEL_EXPORTER_OTLP_ENDPOINT is set, so the app
    carries zero tracing overhead by default. Point the endpoint at any OTLP/HTTP
    collector (SigNoz, Grafana Tempo, Jaeger, an OTel Collector, ...).
    """
    global _configured
    if _configured or not otel_enabled():
        return False

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    service_name = os.getenv("OTEL_SERVICE_NAME", "ai-orchestrator")
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app)
    _configured = True
    return True


def enrich_span(**attributes: Any) -> None:
    """
    Attach attributes to the active span (the FastAPI request span during a
    request). Safe no-op when tracing is disabled or there is no active span.
    """
    span = trace.get_current_span()
    for key, value in attributes.items():
        if value is not None:
            span.set_attribute(key, value)
