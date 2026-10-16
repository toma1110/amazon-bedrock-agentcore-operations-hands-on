"""S06-only OpenTelemetry stdout spans and bounded latency metric helpers."""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.config import Config
from opentelemetry import trace
from opentelemetry.trace import Span
from strands.telemetry import StrandsTelemetry

METRIC_NAMESPACE = "Course/S06"
METRIC_NAME = "AgentInvocationLatencyMs"
METRIC_DIMENSIONS = [{"Name": "Section", "Value": "S06"}]

_telemetry: StrandsTelemetry | None = None
_metric_client: Any | None = None

MAX_MODEL_REQUEST_ATTEMPTS = 2
MAX_MODEL_REQUEST_BYTES = 32_768

_SAFE_PROVIDER_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")


def _safe_provider_label(value: Any) -> str | None:
    """Return a short provider identifier, never arbitrary error text."""
    if isinstance(value, str) and _SAFE_PROVIDER_LABEL.fullmatch(value):
        return value
    return None


def _safe_http_status(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 100 <= value <= 599:
        return value
    if isinstance(value, str) and value.isdecimal() and len(value) == 3:
        status = int(value)
        if 100 <= status <= 599:
            return status
    return None


def safe_error_fields(exc: BaseException) -> dict[str, str | int]:
    """Extract only bounded provider error identifiers and numeric statuses.

    Botocore ClientError (including EventStreamError) retains a parsed
    ``response`` mapping. Provider messages, response bodies, headers, request
    IDs, and arbitrary exception text can contain learner or credential data,
    so none are copied to the Runtime log.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return {}
    error = response.get("Error")
    if not isinstance(error, dict):
        return {}

    fields: dict[str, str | int] = {}
    error_code = _safe_provider_label(error.get("Code"))
    if error_code is not None:
        fields["error_code"] = error_code

    metadata = response.get("ResponseMetadata")
    if isinstance(metadata, dict):
        http_status = _safe_http_status(metadata.get("HTTPStatusCode"))
        if http_status is not None:
            fields["http_status"] = http_status
        headers = metadata.get("HTTPHeaders")
        if isinstance(headers, dict):
            # Botocore's parsed ConverseStream error frame retains the event
            # discriminator only as this typed header. Never serialize headers.
            provider_event_type = _safe_provider_label(headers.get(":exception-type"))
            if provider_event_type is not None:
                fields["provider_event_type"] = provider_event_type

    original_status = next((
        _safe_http_status(error.get(key))
        for key in ("originalStatusCode", "OriginalStatusCode", "original_status_code")
        if _safe_http_status(error.get(key)) is not None
    ), None)
    if original_status is not None:
        fields["original_status_code"] = original_status
    return fields


class ModelRequestLimitExceeded(RuntimeError):
    """Raised before a model request is sent when an S06 guard is reached."""


class ModelRequestGuard:
    """Bound and count outbound Bedrock requests for one S06 Runtime invocation."""

    def __init__(
        self,
        *,
        max_attempts: int = MAX_MODEL_REQUEST_ATTEMPTS,
        max_request_bytes: int = MAX_MODEL_REQUEST_BYTES,
    ) -> None:
        self.max_attempts = max_attempts
        self.max_request_bytes = max_request_bytes
        self._lock = threading.Lock()
        self._attempts = 0
        self._blocked = 0
        self._request_bytes: list[int] = []

    def begin_invocation(self) -> None:
        with self._lock:
            self._attempts = 0
            self._blocked = 0
            self._request_bytes = []

    def before_send(self, request: Any, **_: Any) -> None:
        body = request.body
        if isinstance(body, str):
            body_size = len(body.encode("utf-8"))
        elif isinstance(body, (bytes, bytearray, memoryview)):
            body_size = len(body)
        else:
            # S06 uses JSON Converse bodies; reject unmeasurable bodies rather
            # than send them without the byte guard.
            with self._lock:
                self._blocked += 1
            raise ModelRequestLimitExceeded("S06 blocked an unmeasurable model request body")

        with self._lock:
            if self._attempts >= self.max_attempts:
                self._blocked += 1
                raise ModelRequestLimitExceeded("S06 model request attempt limit reached")
            if body_size > self.max_request_bytes:
                self._blocked += 1
                raise ModelRequestLimitExceeded("S06 model request byte limit reached")
            self._attempts += 1
            self._request_bytes.append(body_size)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "model_request_attempts": self._attempts,
                "model_request_blocked": self._blocked,
                "model_request_bytes": list(self._request_bytes),
            }


def new_model_request_guard() -> ModelRequestGuard:
    return ModelRequestGuard()


def _safe_span_json(span: Any) -> str:
    """Serialize only span identity, parentage, timing, and safe operation labels.

    Strands spans can contain prompts, model output, and tool payloads. Those
    attributes are intentionally excluded from runtime stdout.
    """
    context = span.context
    parent = span.parent
    attrs = span.attributes or {}
    safe_attrs = {
        key: attrs[key]
        for key in (
            "gen_ai.operation.name",
            "gen_ai.request.model",
            "gen_ai.usage.input_tokens",
            "gen_ai.usage.output_tokens",
            "tool.name",
        )
        if key in attrs and isinstance(attrs[key], (str, int, float, bool))
    }
    document = {
        "event": "otel_span",
        "trace_id": f"{context.trace_id:032x}",
        "span_id": f"{context.span_id:016x}",
        "parent_span_id": f"{parent.span_id:016x}" if parent else None,
        "name": span.name,
        "start_time_unix_nano": span.start_time,
        "end_time_unix_nano": span.end_time,
        "duration_ms": round((span.end_time - span.start_time) / 1_000_000, 3),
        "status": span.status.status_code.name,
        "attributes": safe_attrs,
    }
    return json.dumps(document, ensure_ascii=False, separators=(",", ":")) + os.linesep


def initialize_tracing(tracer_provider: Any | None = None) -> None:
    """Install Strands' real model/tool span instrumentation once per process."""
    global _telemetry
    if _telemetry is None:
        _telemetry = StrandsTelemetry(tracer_provider=tracer_provider)
        _telemetry.setup_console_exporter(formatter=_safe_span_json)


def start_request_span(invocation_id: str) -> Span:
    """Create the parent span that joins Runtime request logs to Strands spans."""
    initialize_tracing()
    span = trace.get_tracer("course.s06.runtime").start_span("runtime.request")
    span.set_attribute("course.section", "s06")
    span.set_attribute("invocation.id", invocation_id)
    return span


def trace_id_for(span: Span) -> str:
    return f"{span.get_span_context().trace_id:032x}"


def record_latency_metric(duration_ms: float, timestamp: datetime) -> None:
    """Publish one bounded-cardinality point; never put request IDs in dimensions."""
    global _metric_client
    if _metric_client is None:
        _metric_client = boto3.client(
            "cloudwatch",
            region_name="ap-northeast-1",
            config=Config(retries={"mode": "standard", "total_max_attempts": 1}),
        )
    _metric_client.put_metric_data(
        Namespace=METRIC_NAMESPACE,
        MetricData=[{
            "MetricName": METRIC_NAME,
            "Dimensions": METRIC_DIMENSIONS,
            "Timestamp": timestamp.astimezone(timezone.utc),
            "Value": max(0.0, float(duration_ms)),
            "Unit": "Milliseconds",
        }],
    )


def reset_for_tests() -> None:
    """Reset process-level handles so unit tests stay isolated."""
    global _telemetry, _metric_client
    _telemetry = None
    _metric_client = None
