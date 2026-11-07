"""HTTP entry point for the dedicated S09 read-only investigation Runtime."""

from __future__ import annotations

import json
import hashlib
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "s02"))
from investigation_agent import display_result, investigation_prompt, make_agent, result_block_types  # noqa: E402

PORT = 8080
MAX_BODY = 16_384
MAX_PROMPT = 2_000
MAX_INVOCATIONS = 3
MAX_MODEL_REQUESTS = 2
MAX_OUTPUT_TOKENS = 3_072
MAX_TOTAL_TOKENS = 12_288
MAX_WINDOW = timedelta(minutes=90)
MAX_AGE = timedelta(days=3)
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
    entries = [entry for entry in tool_trace if entry.get("name") == "read_sample_app_logs"]
    return {
        "log_read_attempts": sum(entry.get("wire_status") in {"confirmed", "unknown"} for entry in entries),
        "log_read_completed": sum(entry.get("status") == "completed" for entry in entries),
        "log_read_failed": sum(entry.get("status") == "failed" for entry in entries),
        "log_read_unknown": sum(entry.get("wire_status") == "unknown" for entry in entries),
        "log_read_not_sent": sum(entry.get("wire_status") == "not_sent" for entry in entries),
    }


def _missing_data_types(tool_trace: list[dict[str, Any]]) -> list[str]:
    reads = [entry for entry in tool_trace if entry.get("name") == "read_sample_app_logs"]
    complete = (len(reads) == 1 and reads[0].get("status") == "completed"
                and isinstance(reads[0].get("result"), dict)
                and reads[0]["result"].get("ok") is True)
    return [] if complete else ["logs"]


def _reader_result(tool_trace: list[dict[str, Any]]) -> dict[str, Any] | None:
    reads = [entry for entry in tool_trace if entry.get("name") == "read_sample_app_logs"]
    if (len(reads) == 1 and reads[0].get("status") == "completed"
            and isinstance(reads[0].get("result"), dict)
            and reads[0]["result"].get("ok") is True):
        return reads[0]["result"]
    return None


def _report_value(value: Any) -> str:
    return "未確認" if value is None else str(value)


def _more_results_label(value: Any) -> str:
    if value is True:
        return "あり"
    if value is False:
        return "なし"
    return "未確認"


def _learner_report(reader_result: dict[str, Any]) -> str:
    """Project authoritative reader facts into Japanese learner-facing text."""
    lines = [
        "## 確定データ（reader_resultから生成。モデル生成文ではありません）",
        f"取得元: {_report_value(reader_result.get('source'))}",
        f"UTC時間窓: {_report_value(reader_result.get('start_time'))} ～ {_report_value(reader_result.get('end_time'))}",
        f"取得上限: {_report_value(reader_result.get('result_limit'))}件 / 読取ページ数: {_report_value(reader_result.get('pages_read'))}",
        f"ログが見つかった件数 / 対象件数: {_report_value(reader_result.get('matched_request_count'))} / {_report_value(reader_result.get('expected_request_count'))}",
        f"上限後も結果がある可能性: {_more_results_label(reader_result.get('more_results_possible'))}",
        "",
        "### requestごとの照合",
    ]
    matches = reader_result.get("request_matches")
    if isinstance(matches, list) and matches:
        for item in matches:
            if not isinstance(item, dict):
                continue
            log = item.get("log") if isinstance(item.get("log"), dict) else None
            label = _report_value(item.get("label"))
            request_id = _report_value(item.get("request_id"))
            http_status = _report_value(item.get("http_status"))
            if log is None:
                lines.append(f"- {label}: request ID `{request_id}`、HTTP status {http_status}。ログは未取得です。")
            else:
                logged_status = log.get("status")
                comparison = ("一致" if logged_status == item.get("http_status")
                              else "不一致") if logged_status is not None and item.get("http_status") is not None else "未確認"
                lines.append(
                    f"- {label}: request ID `{request_id}`、HTTP status {http_status}、"
                    f"ログstatus {_report_value(logged_status)}（HTTPとの照合: {comparison}）、"
                    f"durationMs {_report_value(log.get('durationMs'))}。"
                )
    else:
        lines.append("- requestごとの照合情報: 未確認")
    lines.extend(["", "### 集計"])
    status_counts = reader_result.get("log_status_counts")
    if isinstance(status_counts, dict) and status_counts:
        for status, count in status_counts.items():
            suffix = "件" if count is not None else ""
            lines.append(f"- ログstatus {status}: {_report_value(count)}{suffix}")
    else:
        lines.append("- ログstatus別件数: 未確認")
    durations = reader_result.get("durationMs")
    if isinstance(durations, dict):
        lines.append(
            "- durationMs: "
            f"件数 {_report_value(durations.get('count'))}、最小 {_report_value(durations.get('min'))}、"
            f"最大 {_report_value(durations.get('max'))}、平均 {_report_value(durations.get('mean'))}"
        )
    else:
        lines.append("- durationMs集計: 未確認")
    missing_ids = reader_result.get("missing_request_ids")
    if isinstance(missing_ids, list):
        lines.append("- 未取得ID: " + (", ".join(f"`{request_id}`" for request_id in missing_ids) if missing_ids else "なし"))
    else:
        lines.append("- 未取得ID: 未確認")
    lines.append(
        "- 未取得の記録は、この検索で確認できなかったことを示します。"
        "エラーがなかったことや、障害原因の有無を確定するものではありません。"
    )
    if reader_result.get("untrusted_input_observed") is True:
        lines.append("- 不信頼入力フィールドを含むログを観測しました。値は不信頼データとして扱います。")
    return "\n".join(lines)


def _complete_result(response_text: str, stop_reason: str, missing_types: list[str]) -> bool:
    """Accept a final model turn backed by a completed, scoped log read."""
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


def _configured_group() -> str:
    group = os.environ.get("S09_SAMPLE_LOG_GROUP", "")
    if not re.fullmatch(r"/aws/lambda/[A-Za-z][A-Za-z0-9-]{2,30}", group):
        raise ValueError("S09_SAMPLE_LOG_GROUP must identify the deployed shared sample-app Lambda")
    return group


def _make_agent(scope: dict[str, Any]) -> Any:
    agent = make_agent(scope)
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
    required = {"prompt", "log_group", "start_time", "end_time", "observed_requests", "max_results"}
    if not required.issubset(body) or any(body.get(key) in (None, "") for key in ("log_group", "start_time", "end_time")):
        raise LookupError("対象ロググループとUTCの開始・終了時刻を指定してください。情報がそろうまでログは検索しません。")
    if set(body) != required:
        raise ValueError("request must contain exactly prompt, log_group, start_time, end_time, observed_requests, and max_results")
    prompt, log_group = body["prompt"], body["log_group"]
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT:
        raise ValueError("prompt must be a non-empty string of at most 2000 characters")
    if log_group != _configured_group():
        raise ValueError("log_group differs from the exact configured sample-app log group")
    requests = body["observed_requests"]
    if not isinstance(requests, list) or len(requests) not in {1, 4}:
        raise ValueError("observed_requests must contain four order responses or one untrusted_input response")
    ids = [item.get("request_id") for item in requests if isinstance(item, dict)]
    if (len(ids) != len(requests) or any(not isinstance(rid, str) or not rid or len(rid) > 128 for rid in ids)
            or len(set(ids)) != len(requests)
            or any(isinstance(item.get("http_status"), bool) or item.get("http_status") not in {200, 400, 500}
                   for item in requests if isinstance(item, dict))):
        raise ValueError("observed_requests must contain unique request IDs and HTTP statuses 200/400/500")
    if len(requests) == 1 and (requests[0].get("label") != "untrusted_input" or requests[0].get("http_status") != 200):
        raise ValueError("the single-request case must be the untrusted_input response with HTTP 200")
    if len(requests) == 4:
        expected_statuses = {"success": 200, "validation": 400, "injected_failure": 500, "bounded_delay": 200}
        observed_statuses = {item.get("label"): item.get("http_status") for item in requests}
        if observed_statuses != expected_statuses:
            raise ValueError("four-request case must contain the exact scenario labels and HTTP status mapping")
    max_results = body["max_results"]
    if isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= 50:
        raise ValueError("max_results must be an integer from 1 through 50")
    start, end = _utc(body["start_time"], "start_time"), _utc(body["end_time"], "end_time")
    current = now or datetime.now(timezone.utc)
    if start >= end or end > current or start < current - MAX_AGE or end - start > MAX_WINDOW:
        raise ValueError("requested UTC window must be past, within 3 days, and no longer than 90 minutes")
    return {"prompt": prompt, "log_group": log_group, "start_time": start.isoformat().replace("+00:00", "Z"), "end_time": end.isoformat().replace("+00:00", "Z"), "observed_requests": requests, "max_results": max_results}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        _emit({"event": "s09_http", "method": self.command, "status": args[1] if len(args) > 1 else "unknown"})

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/ping":
            self.send_error(404)
            return
        self._send(200, {"status": "healthy", "ready": bool(os.environ.get("S09_SAMPLE_LOG_GROUP"))})

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
                "reader_result": _reader_result(trace),
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
                diagnostic = finish_diagnostic(status="needs_input", missing_data_types=["logs"])
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
                "reader_result": _reader_result(raw_tool_trace),
                "tool_trace": tool_trace,
                "usage": {"input_tokens": usage.get("inputTokens"), "output_tokens": usage.get("outputTokens"), "total_tokens": usage.get("totalTokens")},
            }
            if not _complete_result(response_text, stop_reason, diagnostic["missing_data_types"]):
                diagnostic.update({
                    "status": "error",
                    "error_code": "incomplete_result",
                    "error": "S09 did not return final text backed by a completed scoped sample-app log read",
                    "started_at_utc": started_at,
                    "completed_at_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    "elapsed_ms": round((time.monotonic() - started) * 1_000),
                })
                _emit({"event": "s09_invocation", **diagnostic, "outer_http_status": 200})
                self._send(200, diagnostic)
                return
            response = {
                "status": "success",
                "response": response_text, "model_summary": response_text,
                "report": _learner_report(diagnostic["reader_result"]),
                "request_id": request_id,
                "elapsed_ms": round((time.monotonic() - started) * 1_000),
                "started_at_utc": started_at,
                "completed_at_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "stop_reason": stop_reason,
                "response_block_types": block_types,
                "model_requests": _model_requests,
                "log_read_attempts": diagnostic["log_read_attempts"],
                "log_read_completed": diagnostic["log_read_completed"],
                "log_read_failed": diagnostic["log_read_failed"],
                "log_read_unknown": diagnostic["log_read_unknown"],
                "log_read_not_sent": diagnostic["log_read_not_sent"],
                "missing_data_types": diagnostic["missing_data_types"],
                "reader_result": diagnostic["reader_result"],
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
                   "log_read_attempts": response["log_read_attempts"],
                   "log_read_completed": response["log_read_completed"],
                   "log_read_failed": response["log_read_failed"],
                   "log_read_unknown": response["log_read_unknown"],
                   "log_read_not_sent": response["log_read_not_sent"], "tool_trace": tool_trace,
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
    _configured_group()
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
