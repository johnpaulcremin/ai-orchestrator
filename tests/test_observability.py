from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from app import observability


def test_otel_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert observability.otel_enabled() is False


def test_otel_enabled_when_endpoint_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    assert observability.otel_enabled() is True


def test_enrich_span_is_safe_without_active_span() -> None:
    # No recording span in context: must not raise.
    observability.enrich_span(**{"ai.model": "gpt-5"})


def test_enrich_span_sets_attributes_on_active_span() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("root"):
        observability.enrich_span(
            **{"ai.model": "claude-sonnet-5", "ai.mode_used": "smart", "ai.skip": None}
        )

    span = exporter.get_finished_spans()[0]
    assert span.attributes.get("ai.model") == "claude-sonnet-5"
    assert span.attributes.get("ai.mode_used") == "smart"
    # None-valued attributes are dropped.
    assert "ai.skip" not in span.attributes
