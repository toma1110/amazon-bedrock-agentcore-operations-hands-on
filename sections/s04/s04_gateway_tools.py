"""S04-only IAM MCP client and one-call S03 invocation reader wrapper."""

from __future__ import annotations

import json
import threading
import uuid
from typing import Any

MAX_REMOTE_CALLS_PER_INVOCATION = 1
MAX_GATEWAY_RESPONSE_BYTES = 8192
_lock = threading.Lock()
_calls = 0


def reset_remote_call_budget() -> None:
    global _calls
    with _lock:
        _calls = 0


def create_gateway_client(endpoint: str, region: str) -> Any:
    if region != "ap-northeast-1" or not endpoint.startswith("https://") or not endpoint.endswith("/mcp"):
        raise ValueError("S04 reader Gateway must be an HTTPS Tokyo MCP endpoint")
    from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
    from strands.tools.mcp.mcp_client import MCPClient

    return MCPClient(lambda: aws_iam_streamablehttp_client(
        endpoint=endpoint, aws_region=region, aws_service="bedrock-agentcore",
        timeout=10, sse_read_timeout=20,
    ))


def read_s03_invocation(client: Any, gateway_tool_name: str, *, target_invocation_id: str,
                        target_timestamp_ms: int, max_results: int) -> str:
    """Perform one bounded wire request; never retry an uncertain Gateway call."""
    global _calls
    args = {
        "target_invocation_id": target_invocation_id,
        "target_timestamp_ms": target_timestamp_ms,
        "max_results": max_results,
    }
    from s03_invocation_reader import RequestRejected, validate_request
    try:
        validate_request(args)
    except RequestRejected as exc:
        result = {"ok": False, "error": str(exc)}
        _record(args, result)
        return json.dumps(result, ensure_ascii=False)
    with _lock:
        if _calls >= MAX_REMOTE_CALLS_PER_INVOCATION:
            result = {"ok": False, "error": "S04 Gateway call limit reached; no retry was made"}
            _record(args, result)
            return json.dumps(result, ensure_ascii=False)
        _calls += 1
    response = client.call_tool_sync(tool_use_id=str(uuid.uuid4()), name=gateway_tool_name, arguments=args)
    contents = response.get("content", [])
    text = "\n".join(item.get("text", "") for item in contents if isinstance(item, dict))
    if response.get("status") != "success":
        result: Any = {"ok": False, "error": "S04 reader Gateway returned an error"}
    elif len(text.encode("utf-8")) > MAX_GATEWAY_RESPONSE_BYTES:
        result = {"ok": False, "error": "S04 reader Gateway response exceeded the fixed byte limit"}
    else:
        try:
            result = json.loads(text) if text else response.get("structuredContent", {})
        except json.JSONDecodeError:
            result = {"ok": False, "error": "S04 reader returned invalid JSON"}
    _record(args, result)
    return json.dumps(result, ensure_ascii=False)


def create_reader_tool(client: Any, gateway_tool_name: str) -> Any:
    from strands import tool

    @tool
    def read_s03_invocation(target_invocation_id: str, target_timestamp_ms: int, max_results: int) -> str:
        """Read one S03 invocation summary using its UUID and timestamp from S03 output.

        Args:
            target_invocation_id: Exact UUID printed by the S03 logs command.
            target_timestamp_ms: Exact millisecond timestamp printed by the S03 logs command.
            max_results: Number of matching records to return, from 1 to 5.
        """
        return globals()["read_s03_invocation"](
            client, gateway_tool_name, target_invocation_id=target_invocation_id,
            target_timestamp_ms=target_timestamp_ms, max_results=max_results,
        )

    return read_s03_invocation


def _record(args: dict[str, Any], result: Any) -> None:
    try:
        from agent_app import _tool_trace
        _tool_trace.append({"name": "read_s03_invocation", "arguments": args, "result": result})
    except (ImportError, AttributeError):
        return
