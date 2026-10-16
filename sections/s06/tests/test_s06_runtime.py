from __future__ import annotations

import http.client
import json
import sys
from http.server import HTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

from opentelemetry.sdk.trace import TracerProvider
from botocore.exceptions import EventStreamError
from botocore.parsers import RestJSONParser
from botocore.session import get_session

REPOSITORY = Path(__file__).resolve().parents[3]
S02_DIR = REPOSITORY / "sections" / "s02"
S03_DIR = REPOSITORY / "sections" / "s03"
S06_DIR = REPOSITORY / "sections" / "s06"
sys.path[:0] = [str(S02_DIR), str(S03_DIR), str(S06_DIR)]

import runtime_app
import runtime_observability


def test_s06_invocation_correlates_trace_log_and_metric_without_content(monkeypatch, capsys):
    provider = TracerProvider()
    metrics = []
    request_spans = []

    def start_request_span(invocation_id):
        span = provider.get_tracer("s06.test").start_span("runtime.request")
        span.set_attribute("invocation.id", invocation_id)
        request_spans.append(span)
        return span

    observability = SimpleNamespace(
        start_request_span=start_request_span,
        trace_id_for=lambda span: f"{span.get_span_context().trace_id:032x}",
        record_latency_metric=lambda duration, timestamp: metrics.append((duration, timestamp)),
    )
    monkeypatch.setenv("S06_TRACE_ENABLED", "true")
    monkeypatch.setenv("S06_METRICS_ENABLED", "true")
    monkeypatch.setattr(runtime_app, "_s06_observability", lambda: observability)
    monkeypatch.setattr(runtime_app.agent_app, "_tool_trace", [])
    monkeypatch.setattr(runtime_app, "_s06_request_count", 0)
    monkeypatch.setattr(runtime_app, "_s06_halted", False)
    monkeypatch.setattr(runtime_app, "_s06_last_request_at", None)
    model_calls = []

    class FakeAgent:
        def __call__(self, prompt, *, limits):
            assert limits == {"turns": 2, "output_tokens": 512, "total_tokens": 2_048}
            model_calls.append(prompt)
            runtime_app.agent_app._tool_trace.append({"name": "read_synthetic_cloudwatch"})
            return "synthetic answer"

    monkeypatch.setattr(runtime_app, "_agent", FakeAgent())
    monkeypatch.setattr(runtime_app, "_ready", True)
    server = HTTPServer(("127.0.0.1", 0), runtime_app.RuntimeHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
    try:
        connection.request(
            "POST",
            "/invocations",
            body=json.dumps({"prompt": "synthetic sample question"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        result = connection.getresponse()
        body = json.loads(result.read())
        output = capsys.readouterr().out
        log_event = json.loads(next(line for line in output.splitlines() if '"event": "agent_invocation"' in line))

        assert result.status == 200
        assert body["invocation_id"] == log_event["invocation_id"]
        assert body["trace_id"] == log_event["trace_id"]
        assert log_event["s06_trace"] is True
        assert log_event["latency_ms"] >= 0
        assert len(metrics) == 1
        assert metrics[0][0] == log_event["latency_ms"]
        assert len(model_calls) == 1
        assert "synthetic sample question" not in json.dumps(log_event)
        assert "synthetic answer" not in output

        # A malformed application payload is still a Runtime invocation, but
        # validation rejects it before the model or Gateway can be called.
        monkeypatch.setattr(runtime_app, "_s06_last_request_at", None)
        connection.request(
            "POST",
            "/invocations",
            body=b'{"request":"catalog-api"}',
            headers={"Content-Type": "application/json"},
        )
        invalid = connection.getresponse()
        invalid_body = json.loads(invalid.read())
        invalid_output = capsys.readouterr().out
        invalid_event = json.loads(next(
            line for line in invalid_output.splitlines()
            if '"event": "agent_invocation"' in line and '"status": 400' in line
        ))
        assert invalid.status == 400
        assert invalid_body["error_type"] == "RequestValidationError"
        assert invalid_event["status"] == 400
        assert invalid_event["error_type"] == "RequestValidationError"
        assert invalid_event["trace_id"]
        assert invalid_event["latency_ms"] >= 0
        assert request_spans[-1].status.status_code.name == "ERROR"
        assert len(model_calls) == 1
        assert len(metrics) == 2

        monkeypatch.setattr(runtime_app, "_s06_request_count", 3)
        connection.request(
            "POST",
            "/invocations",
            body=json.dumps({"prompt": "this request must be rejected"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        over_budget = connection.getresponse()
        assert over_budget.status == 429
        assert "no model request was made" in json.loads(over_budget.read())["error"]
        assert len(model_calls) == 1
        assert len(metrics) == 2
    finally:
        connection.close()
        server.shutdown()
        server.server_close()


def test_s06_stops_process_after_uncertain_model_failure(monkeypatch, capsys):
    attempts = []
    guard = SimpleNamespace(
        begin_invocation=lambda: None,
        snapshot=lambda: {
            "model_request_attempts": len(attempts),
            "model_request_blocked": 0,
            "model_request_bytes": [128] * len(attempts),
        },
    )
    observability = SimpleNamespace(
        MAX_MODEL_REQUEST_BYTES=32_768,
        start_request_span=lambda invocation_id: TracerProvider().get_tracer("s06.failure").start_span("runtime.request"),
        trace_id_for=lambda span: f"{span.get_span_context().trace_id:032x}",
        record_latency_metric=lambda duration, timestamp: None,
    )
    monkeypatch.setenv("S06_TRACE_ENABLED", "true")
    monkeypatch.setenv("S06_METRICS_ENABLED", "false")
    monkeypatch.setattr(runtime_app, "_s06_observability", lambda: observability)
    monkeypatch.setattr(runtime_app, "_s06_model_guard", guard)
    monkeypatch.setattr(runtime_app, "_s06_request_count", 0)
    monkeypatch.setattr(runtime_app, "_s06_halted", False)
    monkeypatch.setattr(runtime_app, "_s06_last_request_at", None)
    monkeypatch.setattr(runtime_app.agent_app, "_tool_trace", [])

    class FailingAgent:
        def __call__(self, prompt, *, limits):
            attempts.append(prompt)
            raise TimeoutError("provider response was not observed")

    monkeypatch.setattr(runtime_app, "_agent", FailingAgent())
    monkeypatch.setattr(runtime_app, "_ready", True)
    server = HTTPServer(("127.0.0.1", 0), runtime_app.RuntimeHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
    try:
        connection.request(
            "POST", "/invocations", body=json.dumps({"prompt": "synthetic test"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        first = connection.getresponse()
        assert first.status == 500
        first.read()
        assert len(attempts) == 1
        log_event = json.loads(next(
            line for line in capsys.readouterr().out.splitlines()
            if '"event": "agent_invocation"' in line
        ))
        assert log_event["model_request_attempts"] == 1
        assert log_event["usage_observed"] is False
        assert log_event["usage"] is None

        connection.request(
            "POST", "/invocations", body=json.dumps({"prompt": "do not retry"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        blocked = connection.getresponse()
        assert blocked.status == 429
        assert "stopped after a failed invocation" in json.loads(blocked.read())["error"]
        assert len(attempts) == 1
    finally:
        connection.close()
        server.shutdown()
        server.server_close()


def test_s06_logs_safe_eventstream_fields_without_exposing_error_message(monkeypatch, capsys):
    guard = SimpleNamespace(
        begin_invocation=lambda: None,
        snapshot=lambda: {
            "model_request_attempts": 1,
            "model_request_blocked": 0,
            "model_request_bytes": [512],
        },
    )
    observability = SimpleNamespace(
        MAX_MODEL_REQUEST_BYTES=32_768,
        start_request_span=lambda invocation_id: TracerProvider().get_tracer("s06.eventstream").start_span("runtime.request"),
        trace_id_for=lambda span: f"{span.get_span_context().trace_id:032x}",
        record_latency_metric=lambda duration, timestamp: None,
        safe_error_fields=runtime_observability.safe_error_fields,
    )
    monkeypatch.setenv("S06_TRACE_ENABLED", "true")
    monkeypatch.setenv("S06_METRICS_ENABLED", "false")
    monkeypatch.setattr(runtime_app, "_s06_observability", lambda: observability)
    monkeypatch.setattr(runtime_app, "_s06_model_guard", guard)
    monkeypatch.setattr(runtime_app, "_s06_request_count", 0)
    monkeypatch.setattr(runtime_app, "_s06_halted", False)
    monkeypatch.setattr(runtime_app, "_s06_last_request_at", None)
    monkeypatch.setattr(runtime_app.agent_app, "_tool_trace", [])

    service = get_session().get_service_model("bedrock-runtime")
    parsed = RestJSONParser().parse({
        "status_code": 400,
        "headers": {
            ":exception-type": "modelStreamErrorException",
            "content-type": "application/json",
            "authorization": "PRIVATE CREDENTIAL",
            "x-private-header": "PRIVATE HEADER VALUE",
        },
        "body": json.dumps({
            "originalStatusCode": 424,
            "message": "PRIVATE BODY MESSAGE",
        }).encode("utf-8"),
    }, service.operation_model("ConverseStream").output_shape)
    parsed_error = EventStreamError(parsed, "ConverseStream")

    class FailingAgent:
        def __call__(self, prompt, *, limits):
            raise parsed_error

    monkeypatch.setattr(runtime_app, "_agent", FailingAgent())
    monkeypatch.setattr(runtime_app, "_ready", True)
    server = HTTPServer(("127.0.0.1", 0), runtime_app.RuntimeHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
    try:
        connection.request(
            "POST", "/invocations", body=json.dumps({"prompt": "private question"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        output = capsys.readouterr().out
        log_event = json.loads(next(
            line for line in output.splitlines() if '"event": "agent_invocation"' in line
        ))

        assert response.status == 500
        assert json.loads(body)["error"] == "agent invocation failed; inspect the Runtime log"
        assert log_event["error_type"] == "EventStreamError"
        assert log_event["error_code"] == "400"
        assert log_event["provider_event_type"] == "modelStreamErrorException"
        assert log_event["http_status"] == 400
        assert "original_status_code" not in log_event
        assert log_event["tool_call_count"] == 0
        assert log_event["usage_observed"] is False
        assert log_event["usage"] is None
        assert all(secret not in output + body for secret in (
            "PRIVATE", "424", "authorization", "x-private-header", "content-type",
        ))
        assert "private question" not in output + body
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
