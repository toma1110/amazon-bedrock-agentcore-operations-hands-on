"""Create the IAM-authenticated MCP client used by the S04 Runtime path."""

from __future__ import annotations

from typing import Any
import json
import threading
import uuid

MAX_REMOTE_CALLS_PER_INVOCATION = 4
_budget_lock = threading.Lock()
_remote_call_count = 0


def reset_remote_call_budget() -> None:
    """Reset the remote Gateway tool-call budget for one Runtime request."""
    global _remote_call_count
    with _budget_lock:
        _remote_call_count = 0


def _reserve_remote_call() -> bool:
    """Atomically reserve one wire call, including concurrent tool requests."""
    global _remote_call_count
    with _budget_lock:
        if _remote_call_count >= MAX_REMOTE_CALLS_PER_INVOCATION:
            return False
        _remote_call_count += 1
        return True


def create_gateway_client(endpoint: str, region: str) -> Any:
    """Return a Strands MCP client that signs Gateway requests with Runtime IAM."""
    if not endpoint.startswith("https://") or not endpoint.endswith("/mcp"):
        raise ValueError("S04_GATEWAY_URL must be an HTTPS Gateway MCP endpoint ending in /mcp")
    if region != "ap-northeast-1":
        raise ValueError("S04 Gateway must use ap-northeast-1")

    from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
    from strands.tools.mcp.mcp_client import MCPClient

    return MCPClient(lambda: aws_iam_streamablehttp_client(
        endpoint=endpoint,
        aws_region=region,
        aws_service="bedrock-agentcore",
        timeout=10,
        sse_read_timeout=20,
    ))


def create_reader_tool(client: Any, gateway_tool_name: str) -> Any:
    """Wrap the single approved MCP tool and retain a bounded invocation trace."""
    from strands import tool

    @tool
    def read_synthetic_cloudwatch(
        data_type: str,
        service: str,
        start_time: str,
        end_time: str,
        max_results: int,
    ) -> str:
        """Read bounded synthetic S04 CloudWatch logs or metric data.

        Use data_type 'logs' or 'metrics', the exact service 'catalog-api',
        UTC ISO-8601 timestamps no more than 60 minutes apart, and max_results 1-50.
        """
        arguments = {
            "data_type": data_type,
            "service": service,
            "start_time": start_time,
            "end_time": end_time,
            "max_results": max_results,
        }
        from agent_app import _tool_trace

        if not _reserve_remote_call():
            denied = {"ok": False, "error": "S04 Gateway call limit reached for this Runtime invocation"}
            _tool_trace.append({"name": "read_synthetic_cloudwatch", "arguments": arguments, "result": denied})
            return json.dumps(denied, ensure_ascii=False)
        result = client.call_tool_sync(
            tool_use_id=str(uuid.uuid4()),
            name=gateway_tool_name,
            arguments=arguments,
        )
        contents = result.get("content", [])
        text = "\n".join(
            item.get("text", json.dumps(item.get("json"), ensure_ascii=False))
            for item in contents
            if isinstance(item, dict)
        )
        if result.get("status") != "success":
            trace_result: Any = {"ok": False, "error": "Gateway tool returned an error"}
        else:
            try:
                trace_result = json.loads(text) if text else result.get("structuredContent", {})
            except json.JSONDecodeError:
                trace_result = {"message": text}
        _tool_trace.append({"name": "read_synthetic_cloudwatch", "arguments": arguments, "result": trace_result})
        if result.get("status") != "success":
            return json.dumps({"ok": False, "error": "Gateway tool returned an error"})
        return text or json.dumps(trace_result, ensure_ascii=False)

    return read_synthetic_cloudwatch
