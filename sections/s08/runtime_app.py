"""S08 Runtime entry point: compare two bounded prompts against one local runbook."""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from botocore.config import Config
from strands import Agent, tool
from strands.models import BedrockModel

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "s02"))
import agent_app  # noqa: E402

PORT = 8080
MAX_BODY = 16_384
MAX_PROMPT = 2_000
MAX_INVOCATIONS = 4
MAX_MODEL_REQUESTS = 2
MAX_OUTPUT_TOKENS = 256
MAX_TOTAL_OUTPUT_TOKENS = MAX_MODEL_REQUESTS * MAX_OUTPUT_TOKENS

PROMPTS = {
    "baseline": "確認事項と結論を短くまとめてください。",
    "candidate": (
        "確認事項をrunbookの順に短く挙げ、最後の一文は、受講者への"
        "直接的な注意として「合成runbookは実AWS環境の現在の状態や"
        "障害原因を示すものではありません」と伝えてください。"
    ),
}
COMMON_PROMPT = (
    "回答は日本語で簡潔にしてください。確認項目を3点示してください。"
    "この合成データは実AWS環境の現在状態を示さないと一言添えてください。"
    "導入のあいさつや固定の見出しは不要です。"
)

_agent: Any | None = None
_requests = 0
_model_requests = 0
_process_model_requests = 0
_failed = False


def _variant() -> str:
    return os.environ.get("S08_PROMPT_VARIANT", "baseline")


def _emit(event: dict[str, Any]) -> None:
    print(json.dumps(event, ensure_ascii=False, separators=(",", ":")), flush=True)


def _before_model_request(**_: Any) -> None:
    global _model_requests, _process_model_requests
    if _model_requests >= MAX_MODEL_REQUESTS:
        raise RuntimeError("S08 model request limit reached; no further model request sent")
    _model_requests += 1
    _process_model_requests += 1


@tool
def inspect_synthetic_runbook(service: str, topic: str) -> str:
    """Read one exact match from the bundled synthetic runbook.

    Args:
        service: Exact service key such as catalog-api.
        topic: Exact topic key such as 5xx.
    """
    result = agent_app.find_runbook(service=service, topic=topic)
    _emit({"event": "s08_tool", "status": "ok", "source": "sections/s02/runbooks.json"})
    return json.dumps(result, ensure_ascii=False)


def _make_agent() -> Any:
    variant = _variant()
    if variant not in PROMPTS:
        raise ValueError("S08_PROMPT_VARIANT must be baseline or candidate")
    model = BedrockModel(
        model_id="amazon.nova-lite-v1:0",
        region_name="ap-northeast-1",
        boto_client_config=Config(retries={"mode": "standard", "total_max_attempts": 1}),
        max_tokens=MAX_OUTPUT_TOKENS,
        temperature=0,
        streaming=False,
    )
    model.client.meta.events.register("before-send.bedrock-runtime.*", _before_model_request)
    return Agent(
        model=model,
        tools=[inspect_synthetic_runbook],
        system_prompt=(
            "あなたは講座用の読み取り専用の運用調査アシスタントです。根拠は同梱の合成runbookだけです。"
            + COMMON_PROMPT
            + PROMPTS[variant]
        ),
        callback_handler=None,
        retry_strategy=None,
    )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/ping":
            self.send_error(404)
            return
        self._send(200, {"status": "healthy", "ready": _agent is not None})

    def do_POST(self) -> None:  # noqa: N802
        global _requests, _model_requests, _failed
        if self.path != "/invocations":
            self.send_error(404)
            return
        request_id = str(uuid.uuid4())
        started = time.monotonic()
        model_started = False
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY:
                raise ValueError("request body must be between 1 and 16384 bytes")
            body = json.loads(self.rfile.read(length))
            prompt = body.get("prompt") if isinstance(body, dict) else None
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT:
                raise ValueError("prompt must be a non-empty string of at most 2000 characters")
            if _requests >= MAX_INVOCATIONS:
                variant = _variant()
                elapsed_ms = round((time.monotonic() - started) * 1_000)
                _emit({
                    "event": "s08_invocation",
                    "status": "rejected",
                    "error_code": "app_invocation_limit",
                    "request_id": request_id,
                    "variant": variant,
                    "elapsed_ms": elapsed_ms,
                    "model_requests": 0,
                    "process_model_requests": _process_model_requests,
                    "usage": "unavailable",
                })
                self._send(429, {"error": "S08 invocation limit reached", "error_code": "app_invocation_limit", "retryable": False, "request_id": request_id})
                return
            variant = _variant()
            if _failed:
                elapsed_ms = round((time.monotonic() - started) * 1_000)
                _emit({
                    "event": "s08_invocation",
                    "status": "rejected_after_failure",
                    "error_code": "previous_invocation_failed",
                    "request_id": request_id,
                    "variant": variant,
                    "elapsed_ms": elapsed_ms,
                    "model_requests": 0,
                    "process_model_requests": _process_model_requests,
                    "usage": "unavailable",
                })
                self._send(500, {"error": "previous invocation failed; model requests are disabled for this process", "error_code": "previous_invocation_failed", "retryable": False, "request_id": request_id})
                return
            _requests += 1
            _model_requests = 0
            _failed = False
            model_started = True
            result = _agent(
                prompt,
                limits={
                    "turns": MAX_MODEL_REQUESTS,
                    "output_tokens": MAX_TOTAL_OUTPUT_TOKENS,
                    "total_tokens": 4_096,
                },
            )
            usage = result.metrics.accumulated_usage
            elapsed_ms = round((time.monotonic() - started) * 1_000)
            response = {
                "response": agent_app._display_answer(result),
                "request_id": request_id,
                "variant": variant,
                "elapsed_ms": elapsed_ms,
                "model_requests": _model_requests,
                "process_model_requests": _process_model_requests,
                "usage": {
                    "input_tokens": usage.get("inputTokens"),
                    "output_tokens": usage.get("outputTokens"),
                    "total_tokens": usage.get("totalTokens"),
                },
            }
            self._send(200, response)
            _emit({"event": "s08_invocation", "status": "ok", **response})
        except (ValueError, json.JSONDecodeError) as exc:
            if model_started:
                self._send_runtime_error(exc, request_id, started)
                return
            self._send(400, {"error": type(exc).__name__, "message": str(exc), "request_id": request_id})
        except Exception as exc:  # provider exceptions are returned without retry
            self._send_runtime_error(exc, request_id, started)

    def _send_runtime_error(self, exc: Exception, request_id: str, started: float) -> None:
        global _failed
        _failed = True
        error_type = type(exc).__name__
        error_code = "max_tokens_reached" if error_type == "MaxTokensReachedException" else "runtime_invocation_failed"
        status = 500 if error_type == "MaxTokensReachedException" else 502
        elapsed_ms = round((time.monotonic() - started) * 1_000)
        _emit({
            "event": "s08_invocation",
            "status": "error",
            "error_type": error_type,
            "error_code": error_code,
            "request_id": request_id,
            "variant": _variant(),
            "elapsed_ms": elapsed_ms,
            "model_requests": _model_requests,
            "process_model_requests": _process_model_requests,
            "usage": "unavailable",
        })
        payload = {
            "error": error_type,
            "error_code": error_code,
            "request_id": request_id,
            "variant": _variant(),
            "elapsed_ms": elapsed_ms,
            "model_requests": _model_requests,
            "usage": "unavailable",
        }
        if error_type == "MaxTokensReachedException":
            payload["retryable"] = False
        self._send(status, payload)

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main() -> None:
    global _agent
    _agent = _make_agent()
    _emit({"event": "s08_runtime_ready", "region": "ap-northeast-1", "port": PORT})
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
