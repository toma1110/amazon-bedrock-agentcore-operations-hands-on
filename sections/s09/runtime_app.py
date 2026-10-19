"""HTTP entry point for the dedicated S09 read-only investigation Runtime."""

from __future__ import annotations

import json
import hashlib
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "s03"))
sys.path.insert(0, str(HERE.parent / "s02"))
from investigation_agent import display_result, investigation_prompt, make_agent, result_block_types  # noqa: E402
from gateway_tools import create_gateway_client, reset_remote_call_budget  # noqa: E402

PORT = 8080
MAX_BODY = 16_384
MAX_PROMPT = 2_000
MAX_INVOCATIONS = 2
MAX_MODEL_REQUESTS = 3
MAX_OUTPUT_TOKENS = 3_072
MAX_TOTAL_TOKENS = 12_288
MAX_WINDOW = timedelta(minutes=60)
MAX_AGE = timedelta(days=7)
SERVICE = "catalog-api"
_gateway_client: Any | None = None
_gateway_tool_name = ""
_requests = 0
_model_requests = 0


def _emit(event: dict[str, Any]) -> None:
    event.setdefault("timestamp_utc", datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"))
    print(json.dumps(event, ensure_ascii=False, separators=(",", ":")), flush=True)


def _clear_tool_trace() -> None:
    import agent_app

    agent_app._tool_trace.clear()


def _snapshot_tool_trace() -> list[dict[str, Any]]:
    import agent_app

    return list(agent_app._tool_trace)


def _gateway_counts(tool_trace: list[dict[str, Any]]) -> dict[str, int]:
    """Separate confirmed attempts, responses, failures, and ambiguous sends."""
    entries = [entry for entry in tool_trace if entry.get("name") == "read_synthetic_cloudwatch"]
    return {
        "gateway_attempts": sum(entry.get("wire_status") in {"confirmed", "unknown"} for entry in entries),
        "gateway_completed": sum(entry.get("status") == "completed" for entry in entries),
        "gateway_failed": sum(entry.get("status") == "failed" for entry in entries),
        "gateway_unknown": sum(entry.get("wire_status") == "unknown" for entry in entries),
        "gateway_not_sent": sum(entry.get("wire_status") == "not_sent" for entry in entries),
    }


def _missing_data_types(tool_trace: list[dict[str, Any]]) -> list[str]:
    observed = {
        entry.get("arguments", {}).get("data_type")
        for entry in tool_trace
        if entry.get("name") == "read_synthetic_cloudwatch"
        and entry.get("status") == "completed"
        and isinstance(entry.get("arguments"), dict)
    }
    return sorted({"logs", "metrics"} - observed)


def _complete_result(response_text: str, stop_reason: str, missing_types: list[str]) -> bool:
    """Accept only a final model turn backed by both required reads."""
    return bool(response_text.strip()) and stop_reason == "end_turn" and not missing_types


def _bounded_trace(tool_trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep request/response evidence bounded without claiming a partial result is complete."""
    bounded: list[dict[str, Any]] = []
    for entry in tool_trace:
        result = entry.get("result")
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        if len(encoded) > 12_288:
            bounded.append({
                "name": entry.get("name"),
                "arguments": entry.get("arguments"),
                "forwarded_arguments": entry.get("forwarded_arguments"),
                "status": entry.get("status"),
                "wire_status": entry.get("wire_status"),
                "error_type": entry.get("error_type"),
                "result": {
                    "truncated": True,
                    "original_bytes": len(encoded),
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                    "preview": encoded[:4_096].decode("utf-8", errors="replace"),
                },
            })
        else:
            bounded.append(entry)
    return bounded


def _before_model_request(**_: Any) -> None:
    global _model_requests
    _model_requests += 1
    if _model_requests > MAX_MODEL_REQUESTS:
        raise RuntimeError("S09 model request limit reached; no further request sent")


def _connect_gateway() -> None:
    global _gateway_client, _gateway_tool_name
    endpoint = os.environ.get("S09_GATEWAY_URL", "")
    _gateway_tool_name = os.environ.get("S09_GATEWAY_TOOL_NAME", "")
    if not endpoint or not _gateway_tool_name:
        raise ValueError("S09_GATEWAY_URL and S09_GATEWAY_TOOL_NAME are required")
    _gateway_client = create_gateway_client(endpoint, "ap-northeast-1")
    _gateway_client.__enter__()
    tools = _gateway_client.list_tools_sync()
    names = {getattr(item, "mcp_tool", None).name for item in tools if getattr(item, "mcp_tool", None)}
    if _gateway_tool_name not in names or len(names) != 1:
        _gateway_client.__exit__(None, None, None)
        _gateway_client = None
        raise ValueError("S09 Gateway must expose exactly the expected read-only tool")


def _make_agent(scope: dict[str, str]) -> Any:
    if _gateway_client is None:
        raise RuntimeError("S09 Gateway client is not ready")
    agent = make_agent(_gateway_client, _gateway_tool_name, scope)
    agent.model.client.meta.events.register("before-send.bedrock-runtime.*", _before_model_request)
    return agent


def _utc(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO-8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must include a UTC timezone")
    return parsed.astimezone(timezone.utc)


def validate_scope(body: object, *, now: datetime | None = None) -> dict[str, str]:
    if not isinstance(body, dict):
        raise ValueError("request must be a JSON object")
    required = {"prompt", "service", "start_time", "end_time"}
    if not required.issubset(body) or any(body.get(key) in (None, "") for key in ("service", "start_time", "end_time")):
        raise LookupError("対象サービスとUTCの開始・終了時刻を指定してください。情報がそろうまでログやメトリクスは検索しません。")
    if set(body) != required:
        raise ValueError("request must contain exactly prompt, service, start_time, and end_time")
    prompt, service = body["prompt"], body["service"]
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT:
        raise ValueError("prompt must be a non-empty string of at most 2000 characters")
    if service != SERVICE:
        raise ValueError("service is outside the fixed S04 synthetic dataset")
    start, end = _utc(body["start_time"], "start_time"), _utc(body["end_time"], "end_time")
    current = now or datetime.now(timezone.utc)
    if start >= end or end > current or start < current - MAX_AGE or end - start > MAX_WINDOW:
        raise ValueError("requested UTC window must be past, within 7 days, and no longer than 60 minutes")
    return {"prompt": prompt, "service": service, "start_time": start.isoformat().replace("+00:00", "Z"), "end_time": end.isoformat().replace("+00:00", "Z")}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        _emit({"event": "s09_http", "method": self.command, "status": args[1] if len(args) > 1 else "unknown"})

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/ping":
            self.send_error(404)
            return
        self._send(200, {"status": "healthy", "ready": _gateway_client is not None})

    def do_POST(self) -> None:  # noqa: N802
        global _requests, _model_requests
        if self.path != "/invocations":
            self.send_error(404)
            return
        request_id, started = str(uuid.uuid4()), time.monotonic()
        started_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        self._active_request_id = request_id
        _model_requests = 0
        _clear_tool_trace()
        validated_scope = False

        def finish_diagnostic(**fields: Any) -> dict[str, Any]:
            now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            trace = _bounded_trace(_snapshot_tool_trace())
            return {
                "request_id": request_id,
                "started_at_utc": started_at,
                "completed_at_utc": now,
                "elapsed_ms": round((time.monotonic() - started) * 1_000),
                "model_requests": _model_requests,
                "usage": None,
                "stop_reason": "unavailable",
                "response_block_types": [],
                **_gateway_counts(trace),
                "missing_data_types": _missing_data_types(trace),
                "tool_trace": trace,
                **fields,
            }

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY:
                raise ValueError("request body must be between 1 and 16384 bytes")
            body = json.loads(self.rfile.read(length))
            try:
                scope = validate_scope(body)
            except LookupError as exc:
                diagnostic = finish_diagnostic(status="needs_input", missing_data_types=["logs", "metrics"])
                _emit({"event": "s09_invocation", **diagnostic, "outer_http_status": 200})
                self._send(200, {"status": "needs_input", "response": str(exc), **diagnostic})
                return
            validated_scope = True
            if _requests >= MAX_INVOCATIONS:
                diagnostic = finish_diagnostic(status="error", error_code="invocation_limit_reached")
                _emit({"event": "s09_invocation", **diagnostic, "outer_http_status": 200})
                self._send(200, {"status": "error", "error_code": "invocation_limit_reached", **diagnostic})
                return
            _requests += 1
            reset_remote_call_budget(2)
            agent = _make_agent(scope)
            result = agent(
                investigation_prompt(**scope),
                limits={"turns": MAX_MODEL_REQUESTS, "output_tokens": MAX_OUTPUT_TOKENS, "total_tokens": MAX_TOTAL_TOKENS},
            )
            usage = result.metrics.accumulated_usage
            raw_tool_trace = _snapshot_tool_trace()
            tool_trace = _bounded_trace(raw_tool_trace)
            stop_reason = getattr(result, "stop_reason", "unknown")
            block_types = result_block_types(result)
            response_text = display_result(result).strip()
            diagnostic = {
                "request_id": request_id,
                "stop_reason": stop_reason,
                "response_block_types": block_types,
                "model_requests": _model_requests,
                **_gateway_counts(raw_tool_trace),
                "missing_data_types": _missing_data_types(raw_tool_trace),
                "tool_trace": tool_trace,
                "usage": {"input_tokens": usage.get("inputTokens"), "output_tokens": usage.get("outputTokens"), "total_tokens": usage.get("totalTokens")},
            }
            if not _complete_result(response_text, stop_reason, diagnostic["missing_data_types"]):
                diagnostic.update({
                    "status": "error",
                    "error_code": "incomplete_result",
                    "error": "S09 did not return text backed by both required CloudWatch reads",
                    "started_at_utc": started_at,
                    "completed_at_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    "elapsed_ms": round((time.monotonic() - started) * 1_000),
                })
                _emit({"event": "s09_invocation", **diagnostic, "outer_http_status": 200})
                self._send(200, diagnostic)
                return
            response = {
                "status": "success",
                "response": response_text, "request_id": request_id,
                "elapsed_ms": round((time.monotonic() - started) * 1_000),
                "started_at_utc": started_at,
                "completed_at_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "stop_reason": stop_reason,
                "response_block_types": block_types,
                "model_requests": _model_requests,
                "gateway_attempts": diagnostic["gateway_attempts"],
                "gateway_completed": diagnostic["gateway_completed"],
                "gateway_failed": diagnostic["gateway_failed"],
                "gateway_unknown": diagnostic["gateway_unknown"],
                "gateway_not_sent": diagnostic["gateway_not_sent"],
                "missing_data_types": diagnostic["missing_data_types"],
                "tool_trace": tool_trace,
                "usage": diagnostic["usage"],
            }
            response_delivered = self._send(200, response)
            _emit({"event": "s09_invocation", "status": "ok" if response_delivered else "response_delivery_failed", "request_id": request_id,
                   "started_at_utc": response["started_at_utc"], "completed_at_utc": response["completed_at_utc"],
                   "elapsed_ms": response["elapsed_ms"], "model_requests": _model_requests,
                   "outer_http_status": 200,
                   "response_delivered": response_delivered,
                   "stop_reason": stop_reason, "response_block_types": block_types,
                   "gateway_attempts": response["gateway_attempts"],
                   "gateway_completed": response["gateway_completed"],
                   "gateway_failed": response["gateway_failed"],
                   "gateway_unknown": response["gateway_unknown"],
                   "gateway_not_sent": response["gateway_not_sent"], "tool_trace": tool_trace,
                   "usage": response["usage"]})
        except (ValueError, json.JSONDecodeError) as exc:
            http_status = 200 if validated_scope else 400
            fields = {"status": "error", "error_code": type(exc).__name__}
            if not validated_scope:
                fields["message"] = str(exc)
            diagnostic = finish_diagnostic(**fields)
            _emit({"event": "s09_invocation", **diagnostic, "outer_http_status": http_status})
            self._send(http_status, diagnostic)
        except Exception as exc:
            diagnostic = finish_diagnostic(status="error", error_code="runtime_error", error_type=type(exc).__name__)
            # Once the request is accepted, return a structured application
            # failure instead of a transient HTTP 5xx that the managed runtime
            # may replay. Callers must inspect `status`; HTTP 200 is not success.
            _emit({"event": "s09_invocation", **diagnostic, "outer_http_status": 200})
            self._send(200, diagnostic)

    def _send(self, status: int, payload: dict[str, Any]) -> bool:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return True
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError) as exc:
            _emit({
                "event": "s09_http_response_write_failed",
                "request_id": getattr(self, "_active_request_id", None),
                "intended_http_status": status,
                "error_type": type(exc).__name__,
            })
            return False


def main() -> None:
    _connect_gateway()
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
