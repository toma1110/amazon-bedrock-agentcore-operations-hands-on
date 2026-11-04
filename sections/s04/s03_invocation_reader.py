"""Bounded, read-only lookup of one existing S03 AgentCore invocation event."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import boto3

REGION = "ap-northeast-1"
LOG_GROUP_RE = re.compile(r"^/aws/bedrock-agentcore/runtimes/[A-Za-z0-9_-]+-DEFAULT$")
MAX_AGE = timedelta(days=7)
WINDOW = timedelta(minutes=5)
MAX_RESULTS = 5
MIN_RESULTS = 1
MAX_PAGES = 3
MAX_TOOL_NAME_BYTES = 128
MAX_RESPONSE_BYTES = 4096
_UUID = re.compile(r"^[0-9a-fA-F-]{36}$")


class RequestRejected(ValueError):
    """The request is outside the fixed lookup contract."""


def configured_log_group(value: str | None = None) -> str:
    """Return the explicitly configured S03 Runtime log group; never guess it."""
    group = value if value is not None else os.environ.get("S03_LOG_GROUP", "")
    if not isinstance(group, str) or not LOG_GROUP_RE.fullmatch(group):
        raise RuntimeError(
            "S03_LOG_GROUP must be the exact /aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT group"
        )
    return group


def _utc_now(now: datetime | None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() != timedelta(0):
        raise ValueError("validation clock must be timezone-aware UTC")
    return current.astimezone(timezone.utc)


def validate_request(event: object, *, now: datetime | None = None) -> dict[str, Any]:
    if not isinstance(event, dict) or set(event) != {"target_invocation_id", "target_timestamp_ms", "max_results"}:
        raise RequestRejected("request must contain exactly target_invocation_id, target_timestamp_ms, and max_results")

    invocation_id = event["target_invocation_id"]
    if not isinstance(invocation_id, str) or not _UUID.fullmatch(invocation_id):
        raise RequestRejected("target_invocation_id must be a UUID from the S03 invocation output")
    try:
        normalized_id = str(UUID(invocation_id))
    except ValueError as exc:
        raise RequestRejected("target_invocation_id must be a UUID from the S03 invocation output") from exc

    timestamp_ms = event["target_timestamp_ms"]
    if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int) or timestamp_ms <= 0:
        raise RequestRejected("target_timestamp_ms must be the integer timestamp shown by S03 logs")
    try:
        event_time = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise RequestRejected("target_timestamp_ms is outside the supported UTC range") from exc
    current = _utc_now(now)
    if event_time > current:
        raise RequestRejected("target_timestamp_ms cannot be in the future")
    if event_time < current - MAX_AGE:
        raise RequestRejected("target_timestamp_ms must be within the last 7 days")

    limit = event["max_results"]
    if isinstance(limit, bool) or not isinstance(limit, int) or not MIN_RESULTS <= limit <= MAX_RESULTS:
        raise RequestRejected(f"max_results must be an integer from {MIN_RESULTS} to {MAX_RESULTS}")

    half_window_ms = int(WINDOW.total_seconds() * 1000 / 2)
    start_ms = max(int((current - MAX_AGE).timestamp() * 1000), timestamp_ms - half_window_ms)
    end_ms = min(int(current.timestamp() * 1000), timestamp_ms + half_window_ms)
    if start_ms >= end_ms:
        raise RequestRejected("target_timestamp_ms does not identify a searchable time window")
    return {
        "target_invocation_id": normalized_id,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "limit": limit,
    }


def _matches_event(message: object, invocation_id: str) -> dict[str, Any] | None:
    if not isinstance(message, str):
        return None
    try:
        document = json.loads(message)
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(document, dict)
        or document.get("event") != "agent_invocation"
        or document.get("invocation_id") != invocation_id
    ):
        return None

    status = document.get("status")
    tool_call_count = document.get("tool_call_count")
    tool_names = document.get("tool_names")
    if isinstance(status, bool) or not isinstance(status, int):
        return None
    if isinstance(tool_call_count, bool) or not isinstance(tool_call_count, int) or tool_call_count < 0:
        return None
    if not isinstance(tool_names, list) or any(not isinstance(name, str) for name in tool_names):
        return None
    bounded_names: list[str] = []
    names_truncated = len(tool_names) > MAX_RESULTS
    for name in tool_names[:MAX_RESULTS]:
        encoded = name.encode("utf-8")
        if len(encoded) > MAX_TOOL_NAME_BYTES:
            # Decode with ignore only at a UTF-8 boundary; the flag makes this loss explicit.
            name = encoded[:MAX_TOOL_NAME_BYTES].decode("utf-8", errors="ignore")
            names_truncated = True
        bounded_names.append(name)
    return {
        "event": "agent_invocation",
        "target_invocation_id": invocation_id,
        "status": status,
        "tool_call_count": tool_call_count,
        "tool_names": bounded_names,
        "tool_names_truncated": names_truncated,
    }


def _read_invocation(client: Any, request: dict[str, Any]) -> dict[str, Any]:
    matches = []
    next_token: str | None = None
    seen_tokens: set[str] = set()
    truncated = False

    for page_number in range(MAX_PAGES):
        call: dict[str, Any] = {
            "logGroupName": configured_log_group(),
            "startTime": request["start_ms"],
            "endTime": request["end_ms"],
            "filterPattern": f'"{request["target_invocation_id"]}"',
            "limit": request["limit"] - len(matches),
        }
        if next_token is not None:
            call["nextToken"] = next_token
        response = client.filter_log_events(**call)
        events = response.get("events", [])
        if not isinstance(events, list):
            raise RuntimeError("CloudWatch returned an invalid events page")
        for item in events:
            if not isinstance(item, dict):
                continue
            summary = _matches_event(item.get("message"), request["target_invocation_id"])
            if summary is not None:
                timestamp_ms = item.get("timestamp")
                if isinstance(timestamp_ms, int) and not isinstance(timestamp_ms, bool):
                    summary["target_timestamp_ms"] = timestamp_ms
                else:
                    continue
                matches.append(summary)
                if len(matches) >= request["limit"]:
                    break

        candidate = response.get("nextToken")
        if candidate is None:
            break
        if not isinstance(candidate, str) or not candidate or candidate in seen_tokens:
            truncated = True
            break
        if len(matches) >= request["limit"] or page_number == MAX_PAGES - 1:
            truncated = True
            break
        seen_tokens.add(candidate)
        next_token = candidate

    result = {
        "found": bool(matches),
        "events": matches,
        "truncated": truncated,
        "note": (
            "Additional matching events may exist; the result limit was reached."
            if matches and truncated
            else None if matches
            else "Search incomplete; pagination stopped before completion, so absence is not established. Retry later; this does not indicate runtime health."
            if truncated
            else "No matching event found yet; retry later. This does not indicate runtime health."
        ),
    }
    # The allowlist and per-name cap make this bound deterministic; fail closed if
    # a future field change accidentally exceeds the external response contract.
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise RuntimeError("reader response exceeded the fixed byte limit")
    return result


def lambda_handler(
    event: object,
    _context: Any = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    try:
        request = validate_request(event, now=now)
    except RequestRejected as exc:
        return {"ok": False, "error": str(exc)}

    logs = boto3.Session(region_name=REGION).client("logs")
    reader_execution = {}
    lambda_request_id = getattr(_context, "aws_request_id", None)
    if isinstance(lambda_request_id, str) and lambda_request_id:
        reader_execution["lambda_request_id"] = lambda_request_id
    return {
        "ok": True,
        "reader_execution": reader_execution,
        "data": _read_invocation(logs, request),
    }


TOOL_SCHEMA = {
    "inlinePayload": [{
        "name": "read_s03_invocation",
        "description": (
            "Read the status and tool-call summary for one existing S03 invocation. "
            "Use target_invocation_id and target_timestamp_ms from one S03 logs output. "
            "This creates a separate S04 reader_execution; the target S03 record and current S04 Runtime invocation are distinct."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target_invocation_id": {"type": "string", "description": "UUID printed by the S03 logs command"},
                "target_timestamp_ms": {"type": "integer", "description": "CloudWatch timestamp in milliseconds printed by S03 logs"},
                "max_results": {"type": "integer", "minimum": MIN_RESULTS, "maximum": MAX_RESULTS},
            },
            "required": ["target_invocation_id", "target_timestamp_ms", "max_results"],
        },
    }],
}
