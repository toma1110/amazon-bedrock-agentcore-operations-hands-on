"""S07 Runtime entrypoint: bounded diagnostics using the established agent."""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from botocore.config import Config
from strands import Agent, tool
from strands.models import BedrockModel
from opentelemetry import trace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "s02"))
sys.path.insert(0, str(HERE.parent / "s03"))
sys.path.insert(0, str(HERE.parent / "s06"))
import agent_app  # noqa: E402
import gateway_tools  # noqa: E402
import runtime_observability  # noqa: E402
from fault_injection import (  # noqa: E402
    ExternalStubError, ToolExecutionError, ToolInputError,
    call_checked_tool, call_local_api_stub,
)

PORT = 8080
MAX_BODY = 16_384
MAX_PROMPT = 2_000
MAX_INVOCATIONS = 8
MAX_OUTPUT_TOKENS = 256
MAX_TURNS = 2

_agent: Any | None = None
_gateway_client: Any | None = None
_requests = 0
_request_guard = runtime_observability.ModelRequestGuard(max_attempts=MAX_TURNS)
S04_GATEWAY_ID = "seilgfwckz"
S04_GATEWAY_HOST = "udemy-agentcore-s04-gateway-seilgfwckz.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com"
S07_ALLOWED_MODEL_IDS = {agent_app.MODEL_ID, "amazon.nova-lite-v1:0-s07-invalid"}
S07_ALLOWED_REGIONS = {agent_app.DEFAULT_REGION, "ap-northeast-99"}


def _emit(event: dict[str, Any]) -> None:
    print(json.dumps(event, ensure_ascii=False, separators=(",", ":")), flush=True)


@tool
def inspect_synthetic_runbook(service: str, topic: str, inject: str = "none") -> str:
    """Read one fixed sample runbook entry, with optional bounded error injection.

    Args:
        service: Exact key catalog-api or report-worker.
        topic: Exact key 5xx or throttle.
        inject: none or internal; internal reproduces a generic implementation error.
    """
    try:
        if inject not in {"none", "internal"}:
            raise ToolInputError("inject must be none or internal")
        value = call_checked_tool(
            {"service": service, "topic": topic}, agent_app.find_runbook,
            inject_internal_error=inject == "internal",
        )
        _emit({"event": "s07_tool", "name": "inspect_synthetic_runbook", "status": "ok"})
        return json.dumps({"ok": True, "result": value}, ensure_ascii=False)
    except (ToolInputError, ToolExecutionError) as exc:
        _emit({"event": "s07_tool", "name": "inspect_synthetic_runbook", "status": "error",
               "error_type": type(exc).__name__})
        return json.dumps({"ok": False, "error_type": type(exc).__name__, "message": str(exc)}, ensure_ascii=False)


@tool
def call_local_status_api_stub(scenario: str, timeout_ms: int = 250) -> str:
    """Call a localhost-only sample API that can return success, 429, 5xx, or timeout.

    Args:
        scenario: success, 429, 5xx, or delay.
        timeout_ms: Timeout from 1 to 500 milliseconds.
    """
    try:
        result = call_local_api_stub(scenario, timeout_ms=timeout_ms)
        _emit({"event": "s07_tool", "name": "call_local_status_api_stub", "status": "ok",
               "attempts": result["attempts"], "destination": "localhost"})
        return json.dumps({"ok": True, "result": result})
    except (ToolInputError, ExternalStubError, TimeoutError, ToolExecutionError) as exc:
        info = {"ok": False, "error_type": type(exc).__name__, "message": str(exc)}
        if isinstance(exc, ExternalStubError):
            info.update({"status": exc.status, "attempts": exc.attempts})
        _emit({"event": "s07_tool", "name": "call_local_status_api_stub", "status": "error",
               "error_type": type(exc).__name__, "attempts": getattr(exc, "attempts", None)})
        return json.dumps(info)


def _gateway_reader() -> Any:
    global _gateway_client
    url = os.environ.get("S04_GATEWAY_URL", "")
    expected = os.environ.get("S04_GATEWAY_TOOL_NAME", "")
    if not url or not expected:
        raise RuntimeError("S04 Gateway settings are required")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != S04_GATEWAY_HOST or parsed.path != "/mcp"
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("S04 Gateway URL must match the fixed Tokyo gateway endpoint")
    if expected != "udemy-agentcore-s04-cw-target___read_synthetic_cloudwatch":
        raise ValueError("S04 Gateway tool name must match the fixed synthetic reader")
    if _gateway_client is None:
        _gateway_client = gateway_tools.create_gateway_client(url, agent_app.DEFAULT_REGION)
        _gateway_client.__enter__()
        names = [getattr(item, "tool_name", None) for item in _gateway_client.list_tools_sync()]
        if names != [expected]:
            _gateway_client.__exit__(None, None, None)
            _gateway_client = None
            raise RuntimeError("Gateway tool list did not match expected single tool")
    return gateway_tools.create_reader_tool(_gateway_client, expected)


def _make_agent() -> Any:
    model_id = os.environ.get("S07_MODEL_ID", agent_app.MODEL_ID)
    region = os.environ.get("S07_BEDROCK_REGION", agent_app.DEFAULT_REGION)
    if model_id not in S07_ALLOWED_MODEL_IDS:
        raise ValueError("S07_MODEL_ID must be the fixed model or the fixed invalid-model test value")
    if region == "ap-northeast-99":
        raise ValueError("S07_BEDROCK_REGION is intentionally invalid; blocked before creating an AWS client")
    if region not in S07_ALLOWED_REGIONS:
        raise ValueError("S07_BEDROCK_REGION must be ap-northeast-1 or the fixed invalid-region test value")
    options: dict[str, Any] = {"model_id": model_id, "region_name": region,
        "boto_client_config": Config(retries={"mode": "standard", "total_max_attempts": 1}),
        "max_tokens": MAX_OUTPUT_TOKENS, "temperature": 0, "streaming": False}
    model = BedrockModel(**options)
    model.client.meta.events.register("before-send.bedrock-runtime.*", _request_guard.before_send)
    gateway_reader = _gateway_reader()
    return Agent(model=model,
        tools=[inspect_synthetic_runbook, gateway_reader, call_local_status_api_stub],
        system_prompt=("You are a read-only course operations assistant. Use only the fixed synthetic "
            "runbook, fixed synthetic CloudWatch Gateway tool, and localhost-only API stub. Never modify AWS. "
            "Separate observed errors, unknowns, and recovery action. Keep the answer concise."),
        callback_handler=None, max_parallel_tools=1, retry_strategy=None)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/ping":
            self.send_error(404)
            return
        payload = {"status": "healthy", "ready": _agent is not None}
        self._send(200, payload)

    def do_POST(self) -> None:  # noqa: N802
        global _requests
        if self.path != "/invocations":
            self.send_error(404)
            return
        request_id = str(uuid.uuid4())
        start = time.monotonic()
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY:
                raise ValueError("request body must be between 1 and 16384 bytes")
            body = json.loads(self.rfile.read(length))
            prompt = body.get("prompt") if isinstance(body, dict) else None
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT:
                raise ValueError("prompt must be a non-empty string of at most 2000 characters")
            if _requests >= MAX_INVOCATIONS:
                self._send(429, {"error": "session invocation limit reached"})
                return
            _requests += 1
            _request_guard.begin_invocation()
            span = runtime_observability.start_request_span(request_id)
            with trace.use_span(span, end_on_exit=True):
                result = _get_agent()(prompt)
                output = agent_app._display_answer(result)
            self._send(200, {"response": output, "request_id": request_id})
            _emit({"event": "s07_invocation", "request_id": request_id, "status": "ok",
                   "trace_id": runtime_observability.trace_id_for(span),
                   "model_requests": _request_guard.snapshot()["model_request_attempts"],
                   "duration_ms": round((time.monotonic() - start) * 1_000, 1), "time": datetime.now(timezone.utc).isoformat()})
        except Exception as exc:
            error_fields = runtime_observability.safe_error_fields(exc)
            self._send(400 if isinstance(exc, (ValueError, json.JSONDecodeError)) else 502,
                       {"error": type(exc).__name__, "request_id": request_id})
            _emit({"event": "s07_invocation", "request_id": request_id, "status": "error",
                   "trace_id": runtime_observability.trace_id_for(span) if "span" in locals() else None,
                   "error_type": type(exc).__name__, **error_fields,
                   "time": datetime.now(timezone.utc).isoformat()})

    def _send(self, status: int, value: dict[str, Any]) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            # The caller may disconnect before an intentionally delayed stub reply.
            return


def _get_agent() -> Any:
    global _agent
    if _agent is None:
        _agent = _make_agent()
    return _agent


if __name__ == "__main__":
    _agent = _make_agent()
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
