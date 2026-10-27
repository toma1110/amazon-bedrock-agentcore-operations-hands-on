"""Small, explicit Converse examples for the S02 learning sequence.

This module is a review candidate only. It makes no AWS calls at import time.
The caller provides a boto3 Bedrock Runtime client when it is ready to run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

TOOL_NAME = "lookup_runbook"
TOOL_SPEC = {
    "toolSpec": {
        "name": TOOL_NAME,
        "description": "Look up a synthetic runbook by exact service and topic.",
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "service": {"type": "string"},
                    "topic": {"type": "string"},
                },
                "required": ["service", "topic"],
                "additionalProperties": False,
            }
        },
    }
}


def read_runbook(service: str, topic: str, path: Path) -> dict[str, Any]:
    """Read one exact match from the bundled synthetic JSON data."""
    records = json.loads(path.read_text(encoding="utf-8"))
    for record in records:
        if record.get("service") == service and record.get("topic") == topic:
            return {"found": True, **record}
    return {
        "found": False,
        "service": service,
        "topic": topic,
        "source": "runbooks.json",
        "message": "No bundled sample matches both identifiers.",
    }


def response_parts(response: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Return the model message content blocks and its stop reason."""
    message = response["output"]["message"]
    return message["content"], response["stopReason"]


def read_response(response: dict[str, Any]) -> dict[str, Any]:
    """Extract the visible text and metadata from a Converse response."""
    message = response["output"]["message"]
    unsupported = [
        next(iter(block), "unknown")
        for block in message["content"]
        if "text" not in block
    ]
    if unsupported:
        raise ValueError(f"Response contains unsupported content blocks: {unsupported}")
    text = "\n".join(block["text"] for block in message["content"])
    return {
        "role": message["role"],
        "text": text,
        "stop_reason": response["stopReason"],
        "usage": response.get("usage", {}),
    }


def call_model_once(
    client: Any, model_id: str, prompt: str, *, max_tokens: int = 256
) -> dict[str, Any]:
    """Send one text-only Converse request; no tools are offered or run."""
    if not prompt.strip():
        raise ValueError("prompt must not be empty")
    return client.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
    )


def run_tool_cycle(
    client: Any,
    model_id: str,
    prompt: str,
    execute_tool: Callable[[str, str], dict[str, Any]],
    *,
    max_tokens: int = 256,
) -> dict[str, Any]:
    """Show the explicit app-owned tool loop using only lookup_runbook.

    The model can request a tool. This Python app validates the name and input,
    executes its own local function, sends the result back, and asks Converse
    for a final response. The model itself never executes Python or file access.
    """
    if not prompt.strip():
        raise ValueError("prompt must not be empty")
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"text": prompt}]}
    ]
    response = client.converse(
        modelId=model_id,
        messages=messages,
        inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
        toolConfig={"tools": [TOOL_SPEC], "toolChoice": {"auto": {}}},
    )
    calls: list[dict[str, Any]] = []

    # One tool round is sufficient for this lesson's single local read.
    blocks, stop_reason = response_parts(response)
    tool_uses = [block["toolUse"] for block in blocks if "toolUse" in block]
    if stop_reason != "tool_use":
        if tool_uses:
            raise ValueError("Response contains a tool request without stopReason=tool_use")
        return {
            "response": response,
            "tool_calls": calls,
            "initial_stop_reason": stop_reason,
        }
    if not tool_uses:
        raise ValueError("stopReason=tool_use but the response has no toolUse block")
    if len(tool_uses) > 1:
        raise ValueError("This example accepts one tool request per model turn")

    tool_use = tool_uses[0]
    name = tool_use["name"]
    args = tool_use["input"]
    if name != TOOL_NAME:
        raise ValueError(f"Unsupported tool requested: {name}")
    if not isinstance(args, dict):
        raise ValueError("lookup_runbook input must be an object")
    if set(args) != {"service", "topic"}:
        raise ValueError("lookup_runbook requires exactly service and topic")
    if not all(isinstance(args[key], str) for key in ("service", "topic")):
        raise ValueError("service and topic must be strings")
    tool_use_id = tool_use.get("toolUseId")
    if not isinstance(tool_use_id, str) or not 1 <= len(tool_use_id) <= 64 or not tool_use_id.strip():
        raise ValueError("toolUseId must be a non-empty string of 1 to 64 characters")

    result = execute_tool(args["service"], args["topic"])
    calls.append({"name": name, "input": args, "result": result})
    messages.append({"role": "assistant", "content": blocks})
    messages.append(
        {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": tool_use_id,
                        "content": [{"json": result}],
                    }
                }
            ],
        }
    )
    final_response = client.converse(
        modelId=model_id,
        messages=messages,
        inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
        toolConfig={"tools": [TOOL_SPEC], "toolChoice": {"auto": {}}},
    )
    if final_response["stopReason"] == "tool_use":
        raise ValueError("A second tool request is outside this one-round example")
    return {
        "response": final_response,
        "tool_calls": calls,
        "initial_stop_reason": stop_reason,
        "final_stop_reason": final_response["stopReason"],
    }
