"""Bounded read-only reader for the shared observability sample application's logs."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
import re
import threading
from typing import Any

from strands import tool

REGION = "ap-northeast-1"
MAX_RESULTS = 50
MAX_PAGES = 5
MAX_WINDOW = timedelta(minutes=30)
MAX_AGE = timedelta(days=3)


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("timestamps must include UTC")
    return parsed.astimezone(timezone.utc)


def _expected_group() -> str:
    group = os.environ.get("S09_SAMPLE_LOG_GROUP", "")
    if not re.fullmatch(r"/aws/lambda/[A-Za-z][A-Za-z0-9-]{2,30}", group):
        raise ValueError("S09_SAMPLE_LOG_GROUP must be the exact shared-app Lambda log group")
    return group


def make_sample_log_reader(scope: dict[str, Any], client: Any | None = None) -> Any:
    """Return one tool bound to a caller's exact log group, UTC window and request IDs."""
    group = _expected_group()
    if scope.get("log_group") != group:
        raise ValueError("requested log group differs from the configured sample-app log group")
    start, end = _utc(scope["start_time"]), _utc(scope["end_time"])
    now = datetime.now(timezone.utc)
    if start >= end or end > now or start < now - MAX_AGE or end - start > MAX_WINDOW:
        raise ValueError("UTC window must be in the last 3 days and no longer than 30 minutes")
    expected = scope.get("observed_requests")
    if not isinstance(expected, list) or len(expected) not in {1, 4}:
        raise ValueError("provide either four order scenarios or the one fixed untrusted-input scenario")
    indexed: dict[str, dict[str, Any]] = {}
    for item in expected:
        if not isinstance(item, dict):
            raise ValueError("observed request entries must be objects")
        request_id, status = item.get("request_id"), item.get("http_status")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", request_id):
            raise ValueError("request_id must contain only letters, digits, and hyphens")
        if isinstance(status, bool) or status not in {200, 400, 500}:
            raise ValueError("http_status must be 200, 400, or 500")
        if request_id in indexed:
            raise ValueError("request IDs must be unique")
        indexed[request_id] = {"request_id": request_id, "http_status": status, "label": str(item.get("label", ""))[:40]}
    untrusted_case = len(indexed) == 1 and next(iter(indexed.values())).get("label") == "untrusted_input"
    if len(indexed) == 1 and not untrusted_case:
        raise ValueError("the single-request case must be the fixed untrusted_input scenario")
    caller_limit = scope.get("max_results")
    if isinstance(caller_limit, bool) or not isinstance(caller_limit, int) or not 1 <= caller_limit <= MAX_RESULTS:
        raise ValueError("caller max_results must be from 1 through 50")

    logs = client
    if logs is None:
        import boto3
        from botocore.config import Config

        logs = boto3.client("logs", region_name=REGION,
                            config=Config(retries={"mode": "standard", "total_max_attempts": 1},
                                          connect_timeout=5, read_timeout=30))

    call_lock = threading.Lock()
    call_claimed = False

    @tool(
        description=(
            "Read only the configured shared sample application's exact Lambda log group and validated UTC window. "
            "Use the supplied four request IDs; return observed request_complete records, status counts, durationMs "
            "summary and ID matches. Do not infer missing events or cause."
        ),
        inputSchema={"json": {"type": "object", "properties": {
            "log_group": {"type": "string"}, "start_time": {"type": "string"},
            "end_time": {"type": "string"}, "request_ids": {"type": "array", "items": {"type": "string"}},
            "max_results": {"type": "integer"}},
            "required": ["log_group", "start_time", "end_time", "request_ids", "max_results"]}},
    )
    def read_sample_app_logs(log_group: str, start_time: str, end_time: str,
                             request_ids: list[str], max_results: int = MAX_RESULTS) -> str:
        """Read and correlate only the four supplied request IDs."""
        nonlocal call_claimed
        if log_group != group or start_time != scope["start_time"] or end_time != scope["end_time"]:
            return json.dumps({"ok": False, "wire_status": "not_sent", "error": "request scope differs from validated scope; no AWS read sent"})
        if request_ids != list(indexed) or isinstance(max_results, bool) or not isinstance(max_results, int) or max_results < 1:
            return json.dumps({"ok": False, "wire_status": "not_sent", "error": "request IDs or result limit invalid; no AWS read sent"})
        with call_lock:
            if call_claimed:
                return json.dumps({"ok": False, "wire_status": "not_sent", "error": "reader call limit reached; this request was not sent to AWS"})
            call_claimed = True
        limit = min(max_results, caller_limit, MAX_RESULTS)
        start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
        ids_filter = " || ".join(f'($.requestId = "{rid}")' for rid in indexed)
        pattern = f'{{ ($.event = "request_complete") && ({ids_filter}) }}'
        token = None
        seen_tokens: set[str] = set()
        events: list[dict[str, Any]] = []
        pages = 0
        while pages < MAX_PAGES and len(events) < limit:
            args: dict[str, Any] = {"logGroupName": group, "startTime": start_ms, "endTime": end_ms,
                                    "filterPattern": pattern, "limit": min(10000, limit - len(events))}
            if token:
                args["nextToken"] = token
            page = logs.filter_log_events(**args)
            pages += 1
            for event in page.get("events", []):
                try:
                    row = json.loads(event["message"])
                except (KeyError, TypeError, json.JSONDecodeError):
                    continue
                if row.get("event") == "request_complete" and row.get("requestId") in indexed:
                    events.append({"request_id": row["requestId"], "log_timestamp": event.get("timestamp"),
                                   "status": row.get("status"), "durationMs": row.get("durationMs"),
                                   "error": row.get("error"),
                                   **({"untrustedInput": row.get("untrustedInput")} if untrusted_case else {})})
            next_token = page.get("nextToken")
            if not next_token or next_token == token or next_token in seen_tokens:
                token = None
                break
            seen_tokens.add(next_token)
            token = next_token
        more = token is not None
        by_id = {row["request_id"]: row for row in events}
        matches = [{**indexed[rid], "log": by_id.get(rid)} for rid in indexed]
        statuses = Counter(str(row.get("status")) for row in events)
        durations = [row["durationMs"] for row in events if isinstance(row.get("durationMs"), (int, float))]
        matched = sum(row["log"] is not None for row in matches)
        return json.dumps({"ok": True, "source": group, "start_time": start_time, "end_time": end_time,
                           "filter_pattern": pattern, "pages_read": pages, "result_limit": limit,
                           "more_results_possible": more, "matched_request_count": matched,
                           "expected_request_count": len(indexed), "request_matches": matches,
                           "missing_request_ids": [row["request_id"] for row in matches if row["log"] is None],
                           "untrusted_input_observed": bool(untrusted_case and any(
                               isinstance(row.get("log", {}).get("untrustedInput"), str) and row["log"].get("untrustedInput")
                               for row in matches if row.get("log"))),
                           "log_status_counts": dict(statuses),
                           "durationMs": {"count": len(durations), "min": min(durations) if durations else None,
                                          "max": max(durations) if durations else None,
                                          "mean": round(sum(durations) / len(durations), 2) if durations else None}},
                          ensure_ascii=False, separators=(",", ":"))

    return read_sample_app_logs
