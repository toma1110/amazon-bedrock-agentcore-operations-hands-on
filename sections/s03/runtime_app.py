"""HTTP entrypoint for the S03 AgentCore Runtime direct-code deployment."""

from __future__ import annotations

import json
import importlib
import os
import threading
import uuid
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
_ready = False
_agent_limits = {
    "turns": 4,
    "output_tokens": 2_048,
    "total_tokens": 6_000,
}


def _get_agent() -> Any:
    global _agent, _gateway_client
    if _agent is None:
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
            )
        else:
            _agent = agent_app._make_agent(agent_app.DEFAULT_REGION)
    return _agent


def handle_invocation(payload: bytes, invoke: Callable[[str], Any]) -> tuple[int, dict[str, Any]]:
    """Validate a Runtime JSON request and return a JSON-compatible result."""
    if len(payload) > MAX_REQUEST_BYTES:
        return 413, {"error": "request body is too large"}
    try:
        body = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 400, {"error": "request body must be UTF-8 JSON"}
    if not isinstance(body, dict) or not isinstance(body.get("prompt"), str):
        return 400, {"error": "JSON body must contain a string prompt"}
    prompt = body["prompt"].strip()
    if not prompt or len(prompt) > MAX_PROMPT_CHARS:
        return 400, {"error": "prompt must contain 1 to 2000 characters"}
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
        global _invocation_number
        with _agent_lock:
            _invocation_number += 1
            invocation_id = str(uuid.uuid4())
            agent_app._tool_trace.clear()
            def invoke(prompt: str) -> Any:
                agent = _get_agent()
                if _gateway_client is not None:
                    from gateway_tools import reset_remote_call_budget

                    reset_remote_call_budget()
                return agent(
                    prompt,
                    limits=_agent_limits,
                )

            status, response = handle_invocation(payload, invoke)
            trace = list(agent_app._tool_trace)
            response["invocation_id"] = invocation_id
            if status == 200:
                response["trace"] = trace
            # Never log the learner's prompt, model answer, or tool arguments.
            print(json.dumps({
                "event": "agent_invocation",
                "invocation_id": invocation_id,
                "invocation_number": _invocation_number,
                "status": status,
                "tool_call_count": len(trace),
                "tool_names": [item.get("name", "unknown") for item in trace],
                "error_type": response.get("error_type"),
            }, ensure_ascii=False))
        self._write_json(status, response)

    def log_message(self, format: str, *args: Any) -> None:
        # Do not copy request paths, prompts, or provider output to CloudWatch.
        print(f"Runtime HTTP {self.command} status={args[1] if len(args) > 1 else 'unknown'}")


def main() -> None:
    global _ready
    # Readiness means the static HTTP application is loaded. Do not create the
    # Bedrock client here: Botocore may resolve runtime credentials while the
    # SDK client is constructed, before AgentCore V2 takes its snapshot.
    _ready = True
    server = HTTPServer(("0.0.0.0", PORT), RuntimeHandler)
    print(f"AgentCore Runtime HTTP server listening on port {PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
