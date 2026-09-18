"""OpenTelemetry GenAI / agent span helpers.

Attribute names follow the OTel GenAI semantic conventions (Development):
https://github.com/open-telemetry/semantic-conventions-genai
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, Tracer

SERVICE_NAME = "agent-trace-eval"
AGENT_NAME = "fastpay-exception-triage"
AGENT_VERSION = "0.1.0"
DATA_SOURCE_ID = "fastpay-playbooks"


class JsonlSpanExporter(SpanExporter):
    """Writes finished spans as JSON lines for the waterfall renderer and CI artifacts."""

    def __init__(self, path: str, sink: list[dict[str, Any]] | None = None) -> None:
        self.path = path
        self.sink = sink if sink is not None else []
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # Truncate on each eval process so the artifact is one run.
        open(path, "w", encoding="utf-8").close()

    def export(self, spans: list[ReadableSpan]) -> SpanExportResult:
        rows = [readable_span_to_dict(s) for s in spans]
        self.sink.extend(rows)
        with open(self.path, "a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, default=str) + "\n")
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None


def readable_span_to_dict(span: ReadableSpan) -> dict[str, Any]:
    ctx = span.get_span_context()
    parent = span.parent
    attrs = {}
    for key, value in (span.attributes or {}).items():
        attrs[key] = _attr_value(value)
    return {
        "name": span.name,
        "trace_id": format(ctx.trace_id, "032x"),
        "span_id": format(ctx.span_id, "016x"),
        "parent_span_id": format(parent.span_id, "016x") if parent else None,
        "start_time_unix_nano": span.start_time,
        "end_time_unix_nano": span.end_time,
        "kind": span.kind.name if span.kind else None,
        "status": span.status.status_code.name if span.status else "UNSET",
        "attributes": attrs,
    }


def _attr_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_attr_value(v) for v in value]
    return str(value)


def setup_tracer(
    *,
    jsonl_path: str | None = None,
    sink: list[dict[str, Any]] | None = None,
) -> tuple[Tracer, JsonlSpanExporter | None]:
    """Install a process-wide tracer provider. Additional calls only add exporters."""
    resource = Resource.create(
        {
            "service.name": SERVICE_NAME,
            "service.version": AGENT_VERSION,
            "gen_ai.system": "agent_trace_eval",
        }
    )
    current = trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        provider = current
    else:
        provider = TracerProvider(resource=resource)
        trace.set_tracer_provider(provider)

    exporter: JsonlSpanExporter | None = None
    if jsonl_path:
        exporter = JsonlSpanExporter(jsonl_path, sink=sink)
        provider.add_span_processor(SimpleSpanProcessor(exporter))

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint and not getattr(provider, "_ate_otlp_attached", False):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        setattr(provider, "_ate_otlp_attached", True)

    return trace.get_tracer("agent_trace_eval"), exporter


def get_tracer() -> Tracer:
    return trace.get_tracer("agent_trace_eval")


def set_json_attr(span: Span, key: str, value: Any) -> None:
    if value is None:
        return
    if isinstance(value, (dict, list)):
        span.set_attribute(key, json.dumps(value, default=str))
    else:
        span.set_attribute(key, value)


@contextmanager
def genai_span(
    name: str,
    *,
    operation: str,
    kind: SpanKind,
    attributes: dict[str, Any] | None = None,
) -> Iterator[Span]:
    tracer = get_tracer()
    attrs: dict[str, Any] = {"gen_ai.operation.name": operation}
    if attributes:
        for key, value in attributes.items():
            if isinstance(value, (dict, list)):
                attrs[key] = json.dumps(value, default=str)
            elif value is not None:
                attrs[key] = value
    with tracer.start_as_current_span(name, kind=kind, attributes=attrs) as span:
        try:
            yield span
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.set_attribute("error.type", type(exc).__name__)
            raise
