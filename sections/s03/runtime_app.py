"""HTTP entrypoint for the S03 AgentCore Runtime direct-code deployment."""

from __future__ import annotations

import json
import importlib
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys
from typing import Any, Callable

PORT = 8080
MAX_REQUEST_BYTES = 16_384
MAX_PROMPT_CHARS = 2_000

# In the deployed ZIP, agent_app.py and runbooks.json sit beside this file.
# During local development, reuse the completed S02 implementation directly.
_here = Path(__file__).resolve().parent
_s02_dir = _here.parent / "s02"
if not (_here / "agent_app.py").exists() and _s02_dir.exists():
    sys.path.insert(0, str(_s02_dir))
agent_app = importlib.import_module("agent_app")

# AgentCore Runtime gives each active session its own execution environment.
# Keep one Agent in that environment so follow-up invocations in the same
# session can use the Agent's in-process conversation history.
_agent: Any | None = None
_gateway_client: Any | None = None
_agent_lock = threading.Lock()
_invocation_number = 0
_s06_request_count = 0
_s06_halted = False
_s06_last_request_at: float | None = None
_s06_model_guard: Any | None = None
_ready = False
_agent_limits = {
    "turns": 4,
    "output_tokens": 2_048,
    "total_tokens": 6_000,
}
_s06_agent_limits = {
    "turns": 2,
    # These are Strands event-loop stop limits. They are not provider billing
    # caps; per-response max_tokens and the outbound request guard are separate.
    "output_tokens": 512,
    "total_tokens": 2_048,
}
_s06_max_tokens_per_model_request = 512


def _s06_observability() -> Any | None:
    """Load S06-only instrumentation only in an explicitly configured runtime."""
    if os.environ.get("S06_TRACE_ENABLED", "false").lower() != "true":
        return None
    return importlib.import_module("runtime_observability")


def _s06_streaming_override() -> bool | None:
    """Return the opt-in S06 streaming override; reject ambiguous values."""
    if os.environ.get("S06_TRACE_ENABLED", "false").lower() != "true":
        return None
    value = os.environ.get("S06_MODEL_STREAMING")
    if value is None or value == "true":
        return None
    if value == "false":
        return False
    raise ValueError("S06_MODEL_STREAMING must be exactly 'true' or 'false'")


def _get_agent() -> Any:
    global _agent, _gateway_client, _s06_model_guard
    if _agent is None:
        s06_enabled = os.environ.get("S06_TRACE_ENABLED", "false").lower() == "true"
        streaming_override = _s06_streaming_override()
        model_options: dict[str, Any] = {}
        if s06_enabled:
            observability = _s06_observability()
            _s06_model_guard = observability.new_model_request_guard()
            model_options = {
                "s06_model_request_guard": _s06_model_guard,
                "s06_max_tokens": _s06_max_tokens_per_model_request,
            }
        gateway_url = os.environ.get("S04_GATEWAY_URL", "")
        if gateway_url:
            from gateway_tools import create_gateway_client, create_reader_tool

            expected_tool = os.environ.get("S04_GATEWAY_TOOL_NAME", "")
            if not expected_tool:
                raise ValueError("S04_GATEWAY_TOOL_NAME is required when S04 Gateway is enabled")
            _gateway_client = create_gateway_client(gateway_url, agent_app.DEFAULT_REGION)
            _gateway_client.__enter__()
            available_tools = _gateway_client.list_tools_sync()
            # Strands' MCPClient.list_tools_sync() returns MCPAgentTool wrappers,
            # whose public agent-facing name is `tool_name` (the underlying
            # MCP Tool name is available at `mcp_tool.name`).
            names = [getattr(item, "tool_name", None) for item in available_tools]
            if names != [expected_tool]:
                _gateway_client.__exit__(None, None, None)
                _gateway_client = None
                raise RuntimeError("Gateway tool list did not match the single expected S04 tool")
            system_prompt = """あなたは教材用のAWS運用調査アシスタントです。
質問のserviceと時刻範囲を確認し、不明点を推測せず質問してください。
read_synthetic_cloudwatchを使う場合は、合成データであることを明示してください。
返されたログやメトリクスの範囲を超えて原因・結果を断定しないでください。
実際の顧客環境や実システムを調査したと説明してはいけません。
"""
            _agent = agent_app._make_agent(
                agent_app.DEFAULT_REGION,
                tools=[create_reader_tool(_gateway_client, expected_tool)],
                system_prompt=system_prompt,
                **model_options,
            )
        else:
            _agent = agent_app._make_agent(agent_app.DEFAULT_REGION, **model_options)
        if s06_enabled and streaming_override is False:
            _agent.model.update_config(streaming=False)
    return _agent


def handle_invocation(payload: bytes, invoke: Callable[[str], Any]) -> tuple[int, dict[str, Any]]:
    """Validate a Runtime JSON request and return a JSON-compatible result."""
    if len(payload) > MAX_REQUEST_BYTES:
        return 413, {"error": "request body is too large"}
    try:
        body = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 400, {
            "error": "request body must be UTF-8 JSON",
            "error_type": "RequestValidationError",
        }
    if not isinstance(body, dict) or not isinstance(body.get("prompt"), str):
        return 400, {
            "error": "JSON body must contain a string prompt",
            "error_type": "RequestValidationError",
        }
    prompt = body["prompt"].strip()
    if not prompt or len(prompt) > MAX_PROMPT_CHARS:
        return 400, {
            "error": "prompt must contain 1 to 2000 characters",
            "error_type": "RequestValidationError",
        }
    try:
        result = invoke(prompt)
    except Exception as exc:
        # Keep provider error details out of the response. The Runtime log has
        # the request-level failure context for the operator to investigate.
        return 500, {
            "error": "agent invocation failed; inspect the Runtime log",
            "error_type": type(exc).__name__,
        }
    return 200, {"result": agent_app._display_answer(result)}


class RuntimeHandler(BaseHTTPRequestHandler):
    server_version = "S03Runtime/1.0"

    def _write_json(self, status: int, document: dict[str, Any]) -> None:
        body = json.dumps(document, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/ping":
            if not _ready:
                self._write_json(503, {"status": "Initializing"})
                return
            self._write_json(200, {"status": "Healthy"})
            return
        self._write_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/invocations":
            self._write_json(404, {"error": "not found"})
            return
        try:
            _s06_streaming_override()
        except ValueError:
            self._write_json(500, {
                "error": "invalid S06 model streaming configuration",
                "error_type": "ConfigurationError",
            })
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            self._write_json(400, {"error": "invalid Content-Length"})
            return
        if length < 0:
            self._write_json(400, {"error": "invalid Content-Length"})
            return
        if length > MAX_REQUEST_BYTES:
            self._write_json(413, {"error": "request body is too large"})
            return
        payload = self.rfile.read(length)
        global _invocation_number, _s06_request_count, _s06_halted, _s06_last_request_at
        with _agent_lock:
            s06_enabled = os.environ.get("S06_TRACE_ENABLED", "false").lower() == "true"
            now = time.monotonic()
            if s06_enabled and _s06_halted:
                self._write_json(429, {"error": "S06 stopped after a failed invocation; inspect before any new request"})
                return
            if s06_enabled and _s06_request_count >= 3:
                self._write_json(429, {"error": "S06 request limit reached; no model request was made"})
                return
            if s06_enabled and _s06_last_request_at is not None and now - _s06_last_request_at < 60:
                self._write_json(429, {"error": "S06 permits at most one request per minute; no model request was made"})
                return
            if s06_enabled:
                # Count attempts, including malformed requests and failures, so
                # retries cannot silently exceed the hands-on's local budget.
                _s06_request_count += 1
                _s06_last_request_at = now
            _invocation_number += 1
            invocation_id = str(uuid.uuid4())
            invocation_usage: dict[str, int] = {}
            invocation_error_fields: dict[str, str | int] = {}
            agent_app._tool_trace.clear()
            def invoke(prompt: str) -> Any:
                agent = _get_agent()
                if _gateway_client is not None:
                    from gateway_tools import reset_remote_call_budget

                    reset_remote_call_budget(limit=2 if s06_enabled else None)
                limits = (
                    _s06_agent_limits
                    if os.environ.get("S06_TRACE_ENABLED", "false").lower() == "true"
                    else _agent_limits
                )
                try:
                    result = agent(prompt, limits=limits)
                except Exception as exc:
                    # S06 records only safe provider identifiers/statuses. The
                    # exception text and parsed response body are never logged.
                    if observability is not None:
                        safe_error_fields = getattr(observability, "safe_error_fields", None)
                        if callable(safe_error_fields):
                            invocation_error_fields.update(safe_error_fields(exc))
                    raise
                if s06_enabled:
                    metrics = getattr(result, "metrics", None)
                    usage = getattr(metrics, "accumulated_usage", None)
                    if isinstance(usage, dict):
                        for source, target in (
                            ("inputTokens", "input_tokens"),
                            ("outputTokens", "output_tokens"),
                            ("totalTokens", "total_tokens"),
                        ):
                            value = usage.get(source)
                            if isinstance(value, int) and value >= 0:
                                invocation_usage[target] = value
                return result

            started_at = datetime.now(timezone.utc)
            started = time.perf_counter()
            observability = _s06_observability()
            if s06_enabled and _s06_model_guard is not None:
                _s06_model_guard.begin_invocation()
            if observability is None:
                status, response = handle_invocation(payload, invoke)
                trace_id = None
            else:
                from opentelemetry import trace
                from opentelemetry.trace import Status, StatusCode

                request_span = observability.start_request_span(invocation_id)
                with trace.use_span(request_span, end_on_exit=True):
                    status, response = handle_invocation(payload, invoke)
                    request_span.set_attribute("http.response.status_code", status)
                    if status >= 400:
                        request_span.set_status(Status(StatusCode.ERROR))
                    trace_id = observability.trace_id_for(request_span)
            latency_ms = round((time.perf_counter() - started) * 1_000, 3)
            if observability is not None and os.environ.get("S06_METRICS_ENABLED", "false").lower() == "true":
                try:
                    observability.record_latency_metric(latency_ms, started_at)
                except Exception as exc:
                    # Telemetry failure must not convert a successfully served
                    # request into an application failure or leak provider text.
                    print(json.dumps({
                        "event": "s06_metric_publish_error",
                        "invocation_id": invocation_id,
                        "error_type": type(exc).__name__,
                    }, ensure_ascii=False))
            # S02's trace producer records arguments/result, but not the tool
            # name. S03 uses one tool, so derive the name from that callable.
            trace = [
                {**item, "name": item.get("name") or agent_app.lookup_runbook.__name__}
                for item in agent_app._tool_trace
            ]
            if s06_enabled and status >= 500:
                # Provider errors/timeouts can have uncertain billing. Stop
                # this process rather than automatically trying again.
                _s06_halted = True
            response["invocation_id"] = invocation_id
            if trace_id is not None:
                response["trace_id"] = trace_id
            if status == 200:
                response["trace"] = trace
            # Never log the learner's prompt, model answer, or tool arguments.
            log_event = {
                "event": "agent_invocation",
                "invocation_id": invocation_id,
                "invocation_number": _invocation_number,
                "status": status,
                "tool_call_count": len(trace),
                "tool_names": [item.get("name", "unknown") for item in trace],
                "error_type": response.get("error_type"),
            }
            if observability is not None:
                model_guard = (
                    _s06_model_guard.snapshot()
                    if _s06_model_guard is not None
                    else {"model_request_attempts": 0, "model_request_blocked": 0, "model_request_bytes": []}
                )
                log_event.update({
                    "started_at": started_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    "latency_ms": latency_ms,
                    "trace_id": trace_id,
                    "s06_trace": True,
                    **model_guard,
                    **invocation_error_fields,
                    "model_request_max_bytes": getattr(observability, "MAX_MODEL_REQUEST_BYTES", None),
                    "usage_observed": bool(invocation_usage),
                    "usage": invocation_usage or None,
                })
            print(json.dumps(log_event, ensure_ascii=False))
        self._write_json(status, response)

    def log_message(self, format: str, *args: Any) -> None:
        # Do not copy request paths, prompts, or provider output to CloudWatch.
        print(f"Runtime HTTP {self.command} status={args[1] if len(args) > 1 else 'unknown'}")


def main() -> None:
    global _ready
    # Readiness means the static HTTP application is loaded. Do not create the
    # Bedrock client here: Botocore may resolve runtime credentials while the
    # SDK client is constructed, before AgentCore V2 takes its snapshot.
    if os.environ.get("S06_TRACE_ENABLED", "false").lower() == "true":
        _s06_observability().initialize_tracing()
    _ready = True
    server = HTTPServer(("0.0.0.0", PORT), RuntimeHandler)
    print(f"AgentCore Runtime HTTP server listening on port {PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
