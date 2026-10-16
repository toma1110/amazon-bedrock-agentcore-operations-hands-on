from __future__ import annotations

import json
import boto3
import http.server
import sys
import threading
from botocore.exceptions import ClientError, EventStreamError
from botocore.parsers import RestJSONParser
from botocore.session import get_session
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

S06_DIR = Path(__file__).resolve().parents[1]
S02_DIR = S06_DIR.parent / "s02"
sys.path.insert(0, str(S02_DIR))
sys.path.insert(0, str(S06_DIR))

import agent_app
import runtime_observability as observability
import runtime_app


def test_real_strands_model_and_tool_spans_share_runtime_trace_without_content(capsys, monkeypatch):
    from strands import tool

    requests = []
    responses = [
        {
            "output": {"message": {"role": "assistant", "content": [{"toolUse": {
                "toolUseId": "sample-call-1",
                "name": "read_synthetic_cloudwatch",
                "input": {"service": "catalog-api"},
            }}]}},
            "stopReason": "tool_use",
            "usage": {"inputTokens": 24, "outputTokens": 9, "totalTokens": 33},
            "metrics": {"latencyMs": 2},
        },
        {
            "output": {"message": {"role": "assistant", "content": [{"text": "synthetic final answer"}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 31, "outputTokens": 12, "totalTokens": 43},
            "metrics": {"latencyMs": 2},
        },
    ]

    class ConverseHandler(http.server.BaseHTTPRequestHandler):
        request_count = 0

        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
            type(self).request_count += 1
            requests.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0")))))
            response = json.dumps(responses.pop(0)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format, *args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), ConverseHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    base_session = boto3.Session(
        aws_access_key_id="test-access-key",
        aws_secret_access_key="test-secret-key",
        region_name="ap-northeast-1",
    )

    class LocalSession:
        region_name = "ap-northeast-1"

        def client(self, service_name, **kwargs):
            kwargs["endpoint_url"] = endpoint
            return base_session.client(service_name, **kwargs)

    @tool
    def read_synthetic_cloudwatch(service: str) -> str:
        """Read one fixed fake sample record."""
        assert service == "catalog-api"
        return "PRIVATE SYNTHETIC TOOL RESULT"

    observability.reset_for_tests()
    observability.initialize_tracing()
    span_exporter = InMemorySpanExporter()
    observability._telemetry.tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    guard = observability.ModelRequestGuard()
    make_agent = agent_app._make_agent
    monkeypatch.setattr(
        runtime_app.agent_app,
        "_make_agent",
        lambda region, **kwargs: make_agent(
            region, tools=[read_synthetic_cloudwatch], boto_session=LocalSession(), **kwargs
        ),
    )
    monkeypatch.setattr(runtime_app, "_agent", None)
    monkeypatch.setattr(runtime_app, "_gateway_client", None)
    monkeypatch.setattr(runtime_app, "_s06_model_guard", None)
    monkeypatch.setenv("S06_TRACE_ENABLED", "true")
    monkeypatch.setenv("S06_MODEL_STREAMING", "false")
    agent = runtime_app._get_agent()
    guard = runtime_app._s06_model_guard
    parent = observability.start_request_span("local-runtime-request")
    try:
        guard.begin_invocation()
        with trace.use_span(parent, end_on_exit=True):
            result = agent(
                "Use the catalog-api sample only.",
                limits={"turns": 2, "output_tokens": 512, "total_tokens": 2_048},
            )
        assert result.message["content"][0]["text"] == "synthetic final answer"
        assert ConverseHandler.request_count == 2
        assert [request["inferenceConfig"]["maxTokens"] for request in requests] == [512, 512]
        assert guard.snapshot()["model_request_attempts"] == 2
        spans = span_exporter.get_finished_spans()
        assert spans
        trace_ids = {f"{span.context.trace_id:032x}" for span in spans}
        assert trace_ids == {observability.trace_id_for(parent)}
        assert any(span.name == "chat" for span in spans)
        assert any(span.name.startswith("execute_tool read_synthetic_cloudwatch") for span in spans)
        span_ids = {f"{span.context.span_id:016x}" for span in spans}
        assert any(f"{span.parent.span_id:016x}" in span_ids for span in spans if span.parent)
        exported = "\n".join(observability._safe_span_json(span) for span in spans)
        assert "PRIVATE SYNTHETIC TOOL RESULT" not in exported
        assert "synthetic final answer" not in exported
        assert "Use the catalog-api sample only." not in exported
    finally:
        server.shutdown()
        server.server_close()


def test_span_formatter_preserves_parentage_and_timing_but_drops_content():
    parent = SimpleNamespace(span_id=0x12)
    span = SimpleNamespace(
        context=SimpleNamespace(trace_id=0xAB, span_id=0x34),
        parent=parent,
        attributes={
            "tool.name": "read_synthetic_cloudwatch",
            "gen_ai.usage.input_tokens": 19,
            "gen_ai.prompt": "private prompt content",
            "gen_ai.completion": "private answer content",
            "http.request.header.authorization": "secret",
        },
        name="tool.read_synthetic_cloudwatch",
        start_time=1_000_000,
        end_time=5_500_000,
        status=SimpleNamespace(status_code=SimpleNamespace(name="OK")),
    )

    event = json.loads(observability._safe_span_json(span))

    assert event["event"] == "otel_span"
    assert event["trace_id"] == f"{0xAB:032x}"
    assert event["span_id"] == f"{0x34:016x}"
    assert event["parent_span_id"] == f"{0x12:016x}"
    assert event["duration_ms"] == 4.5
    assert event["attributes"] == {
        "tool.name": "read_synthetic_cloudwatch",
        "gen_ai.usage.input_tokens": 19,
    }
    assert "private prompt content" not in json.dumps(event)
    assert "private answer content" not in json.dumps(event)
    assert "secret" not in json.dumps(event)


def test_metric_publisher_uses_one_fixed_low_cardinality_series(monkeypatch):
    calls = []
    fake_client = SimpleNamespace(put_metric_data=lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(observability, "_metric_client", None)
    monkeypatch.setattr(observability.boto3, "client", lambda *args, **kwargs: fake_client)

    observability.record_latency_metric(12.5, datetime(2026, 10, 17, tzinfo=timezone.utc))

    assert len(calls) == 1
    call = calls[0]
    assert call["Namespace"] == "Course/S06"
    datum = call["MetricData"][0]
    assert datum["MetricName"] == "AgentInvocationLatencyMs"
    assert datum["Dimensions"] == [{"Name": "Section", "Value": "S06"}]
    assert datum["Value"] == 12.5
    assert datum["Unit"] == "Milliseconds"
    assert all("trace" not in key.lower() and "invocation" not in key.lower() for key in datum["Dimensions"][0])


def test_s06_bedrock_client_and_strands_retry_are_single_attempts():
    class ThrottleHandler(http.server.BaseHTTPRequestHandler):
        request_count = 0

        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
            type(self).request_count += 1
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = json.dumps({"message": "synthetic throttle", "__type": "ThrottlingException"}).encode()
            self.send_response(429)
            self.send_header("Content-Type", "application/x-amz-json-1.1")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("x-amzn-errortype", "ThrottlingException")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), ThrottleHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    base_session = boto3.Session(
        aws_access_key_id="test-access-key",
        aws_secret_access_key="test-secret-key",
        region_name="ap-northeast-1",
    )

    class LocalSession:
        region_name = "ap-northeast-1"

        def client(self, service_name, **kwargs):
            kwargs["endpoint_url"] = endpoint
            return base_session.client(service_name, **kwargs)

    guard = observability.ModelRequestGuard()
    try:
        agent = agent_app._make_agent(
            "ap-northeast-1",
            s06_model_request_guard=guard,
            s06_max_tokens=512,
            boto_session=LocalSession(),
        )
        assert agent.model.client.meta.config.retries["total_max_attempts"] == 1
        assert agent.model.config["max_tokens"] == 512
        assert agent._retry_strategy._max_attempts == 1

        guard.begin_invocation()
        try:
            agent.model.client.converse(
                modelId=agent_app.MODEL_ID,
                messages=[{"role": "user", "content": [{"text": "synthetic test"}]}],
                inferenceConfig={"maxTokens": 512, "temperature": 0.0},
            )
            raise AssertionError("synthetic throttle should be returned as an error")
        except ClientError:
            pass

        assert ThrottleHandler.request_count == 1
        snapshot = guard.snapshot()
        assert snapshot["model_request_attempts"] == 1
        assert snapshot["model_request_blocked"] == 0
        assert len(snapshot["model_request_bytes"]) == 1
        assert snapshot["model_request_bytes"][0] <= observability.MAX_MODEL_REQUEST_BYTES
    finally:
        server.shutdown()
        server.server_close()


def test_safe_error_fields_extracts_modeled_status_from_full_strands_client_response():
    class ModelErrorHandler(http.server.BaseHTTPRequestHandler):
        request_count = 0

        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
            type(self).request_count += 1
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = json.dumps({
                "message": "PRIVATE BODY MESSAGE",
                "originalStatusCode": 500,
                "resourceName": "PRIVATE RESOURCE NAME",
            }).encode()
            self.send_response(424)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("x-amzn-errortype", "ModelErrorException")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), ModelErrorHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    base_session = boto3.Session(
        aws_access_key_id="test-access-key",
        aws_secret_access_key="test-secret-key",
        region_name="ap-northeast-1",
    )

    class LocalSession:
        region_name = "ap-northeast-1"

        def client(self, service_name, **kwargs):
            kwargs["endpoint_url"] = endpoint
            return base_session.client(service_name, **kwargs)

    guard = observability.ModelRequestGuard()
    try:
        agent = agent_app._make_agent(
            "ap-northeast-1",
            s06_model_request_guard=guard,
            s06_max_tokens=512,
            boto_session=LocalSession(),
        )
        agent.model.update_config(streaming=False)
        guard.begin_invocation()
        caught = None
        try:
            agent("synthetic failure test", limits={"turns": 2, "output_tokens": 512, "total_tokens": 2_048})
        except ClientError as exc:
            caught = exc

        assert caught is not None
        assert ModelErrorHandler.request_count == 1
        assert caught.response["originalStatusCode"] == 500
        assert caught.response["resourceName"] == "PRIVATE RESOURCE NAME"
        fields = observability.safe_error_fields(caught)
        assert fields == {
            "error_code": "ModelErrorException",
            "http_status": 424,
            "original_status_code": 500,
        }
        assert all(secret not in json.dumps(fields) for secret in (
            "PRIVATE", "resourceName", "message",
        ))
    finally:
        server.shutdown()
        server.server_close()


def test_safe_error_fields_matches_locked_botocore_eventstream_parser_shape():
    service = get_session().get_service_model("bedrock-runtime")
    output_shape = service.operation_model("ConverseStream").output_shape
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
    }, output_shape)
    error = EventStreamError(parsed, "ConverseStream")

    assert parsed["Error"] == {"Message": "PRIVATE BODY MESSAGE", "Code": "400"}
    assert parsed["ResponseMetadata"]["HTTPStatusCode"] == 400
    assert parsed["ResponseMetadata"]["HTTPHeaders"][":exception-type"] == "modelStreamErrorException"
    assert "originalStatusCode" not in parsed["Error"]

    fields = observability.safe_error_fields(error)

    assert fields == {
        "error_code": "400",
        "provider_event_type": "modelStreamErrorException",
        "http_status": 400,
    }
    assert "original_status_code" not in fields
    assert all(secret not in json.dumps(fields) for secret in (
        "PRIVATE", "424", "authorization", "x-private-header", "content-type",
    ))


def test_safe_error_fields_omits_unbounded_or_non_numeric_values():
    error = ClientError({
        "Error": {
            "Code": "malicious code with spaces",
            "EventType": "PRIVATE text with spaces",
            "OriginalStatusCode": "not a status",
            "Message": "PRIVATE PROMPT",
        },
        "ResponseMetadata": {
            "HTTPStatusCode": True,
            "HTTPHeaders": {
                "authorization": "PRIVATE CREDENTIAL",
                ":exception-type": "PRIVATE event type with spaces",
            },
        },
    }, "Converse")

    assert observability.safe_error_fields(error) == {}

    top_level_invalid = ClientError({
        "originalStatusCode": True,
        "OriginalStatusCode": 600,
        "original_status_code": "not a status",
        "Error": {
            "Code": "ModelErrorException",
            "originalStatusCode": "5000",
            "Message": "PRIVATE PROMPT",
        },
        "ResponseMetadata": {"HTTPStatusCode": 424},
    }, "Converse")
    assert observability.safe_error_fields(top_level_invalid) == {
        "error_code": "ModelErrorException",
        "http_status": 424,
    }

    top_level_preferred = ClientError({
        "originalStatusCode": 500,
        "Error": {"Code": "ModelErrorException", "originalStatusCode": 424},
        "ResponseMetadata": {"HTTPStatusCode": 424},
    }, "Converse")
    assert observability.safe_error_fields(top_level_preferred)["original_status_code"] == 500

    error_fallback = ClientError({
        "Error": {"Code": "ModelErrorException", "originalStatusCode": 424},
        "ResponseMetadata": {"HTTPStatusCode": 424},
    }, "Converse")
    assert observability.safe_error_fields(error_fallback)["original_status_code"] == 424
