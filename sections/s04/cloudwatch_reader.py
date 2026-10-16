"""Read a small, fixed slice of the S04 synthetic CloudWatch dataset."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3

REGION = "ap-northeast-1"
LOG_GROUP = "/udemy/agentcore/s04/synthetic/catalog-api"
METRIC_NAMESPACE = "Udemy/AgentCoreS04"
METRIC_NAME = "Http5xxCount"
SERVICE = "catalog-api"
MAX_WINDOW = timedelta(hours=1)
MAX_AGE = timedelta(days=7)
MAX_RESULTS = 50
MIN_RESULTS = 1


class RequestRejected(ValueError):
    """The request exceeded the fixed S04 tool contract."""


def _parse_time(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise RequestRejected(f"{field} must be an ISO-8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RequestRejected(f"{field} must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise RequestRejected(f"{field} must include a UTC timezone")
    return parsed.astimezone(timezone.utc)


def validate_request(event: object, *, now: datetime | None = None) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise RequestRejected("request must be an object")
    allowed = {"data_type", "service", "start_time", "end_time", "max_results"}
    if set(event) != allowed:
        raise RequestRejected("request must contain exactly the documented fields")
    data_type = event["data_type"]
    if not isinstance(data_type, str) or data_type not in {"logs", "metrics"}:
        raise RequestRejected("data_type must be logs or metrics")
    if event["service"] != SERVICE:
        raise RequestRejected("service is outside the S04 synthetic dataset")
    limit = event["max_results"]
    if isinstance(limit, bool) or not isinstance(limit, int) or not MIN_RESULTS <= limit <= MAX_RESULTS:
        raise RequestRejected(f"max_results must be an integer from {MIN_RESULTS} to {MAX_RESULTS}")

    start = _parse_time(event["start_time"], "start_time")
    end = _parse_time(event["end_time"], "end_time")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() != timedelta(0):
        raise ValueError("validation clock must be timezone-aware UTC")
    if start >= end:
        raise RequestRejected("start_time must be earlier than end_time")
    if end > current:
        raise RequestRejected("end_time cannot be in the future")
    if start < current - MAX_AGE:
        raise RequestRejected("start_time must be within the last 7 days")
    if end - start > MAX_WINDOW:
        raise RequestRejected("time window cannot exceed 60 minutes")

    return {"data_type": data_type, "start": start, "end": end, "limit": limit}


def _read_logs(client: Any, start: datetime, end: datetime, limit: int) -> dict[str, Any]:
    response = client.filter_log_events(
        logGroupName=LOG_GROUP,
        startTime=int(start.timestamp() * 1000),
        endTime=int(end.timestamp() * 1000),
        limit=limit,
    )
    events = response.get("events", [])
    return {
        "source": {"service": SERVICE, "log_group": LOG_GROUP},
        "start_time": start.isoformat().replace("+00:00", "Z"),
        "end_time": end.isoformat().replace("+00:00", "Z"),
        "events": [
            {"timestamp": item.get("timestamp"), "message": item.get("message", "")}
            for item in events[:limit]
        ],
        "truncated": bool(response.get("nextToken")) or len(events) > limit,
    }


def _read_metrics(client: Any, start: datetime, end: datetime, limit: int) -> dict[str, Any]:
    response = client.get_metric_data(
        MetricDataQueries=[{
            "Id": "http5xx",
            "MetricStat": {
                "Metric": {
                    "Namespace": METRIC_NAMESPACE,
                    "MetricName": METRIC_NAME,
                    "Dimensions": [{"Name": "Service", "Value": SERVICE}],
                },
                "Period": 60,
                "Stat": "Sum",
            },
            "ReturnData": True,
        }],
        StartTime=start,
        EndTime=end,
        MaxDatapoints=limit,
        ScanBy="TimestampAscending",
    )
    result = next(
        (entry for entry in response.get("MetricDataResults", []) if entry.get("Id") == "http5xx"),
        None,
    )
    if result is None:
        raise RuntimeError("CloudWatch returned no result for the requested metric")
    # Preserve PartialData (and other provider statuses) for the caller to inspect.
    points = [
        {"timestamp": stamp.isoformat().replace("+00:00", "Z"), "value": value}
        for stamp, value in zip(result.get("Timestamps", []), result.get("Values", []))
    ][:limit]
    return {
        "source": {
            "service": SERVICE,
            "namespace": METRIC_NAMESPACE,
            "metric_name": METRIC_NAME,
            "dimensions": {"Service": SERVICE},
            "statistic": "Sum",
            "period_seconds": 60,
        },
        "start_time": start.isoformat().replace("+00:00", "Z"),
        "end_time": end.isoformat().replace("+00:00", "Z"),
        "datapoints": points,
        "status": result.get("StatusCode", "Unknown"),
        "truncated": bool(response.get("NextToken")) or len(result.get("Values", [])) > limit,
    }


def _has_expected_gateway_routing_metadata(context: Any) -> bool:
    """Guard routing/configuration; ClientContext metadata is not authentication.

    Actual invocation authorization must come from the Gateway service role's
    exact-Lambda ``lambda:InvokeFunction`` permission.
    """
    client_context = getattr(context, "client_context", None)
    custom = getattr(client_context, "custom", None)
    if not isinstance(custom, dict):
        return False
    expected_gateway = os.environ.get("EXPECTED_GATEWAY_ID", "")
    expected_target = os.environ.get("EXPECTED_TARGET_ID", "")
    if not expected_gateway or not expected_target:
        return False
    tool_name = custom.get("bedrockAgentCoreToolName", "")
    return (
        custom.get("bedrockAgentCoreMessageVersion") == "1.0"
        and custom.get("bedrockAgentCoreGatewayId") == expected_gateway
        and custom.get("bedrockAgentCoreTargetId") == expected_target
        and isinstance(tool_name, str)
        and tool_name.endswith("___read_synthetic_cloudwatch")
    )


def lambda_handler(event: object, context: Any) -> dict[str, Any]:
    if not _has_expected_gateway_routing_metadata(context):
        return {"ok": False, "error": "unexpected gateway routing metadata"}
    try:
        request = validate_request(event)
    except RequestRejected as exc:
        return {"ok": False, "error": str(exc)}

    session = boto3.Session(region_name=REGION)
    if request["data_type"] == "logs":
        data = _read_logs(session.client("logs"), request["start"], request["end"], request["limit"])
    else:
        data = _read_metrics(session.client("cloudwatch"), request["start"], request["end"], request["limit"])
    return {"ok": True, "data": data}


TOOL_SCHEMA = {
    "inlinePayload": [{
        "name": "read_synthetic_cloudwatch",
        "description": (
            "Read only the S04 synthetic catalog-api CloudWatch logs or metric. "
            "Use UTC timestamps, a window no longer than 60 minutes, and max_results 1-50."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "data_type": {"type": "string", "enum": ["logs", "metrics"]},
                "service": {
                    "type": "string",
                    "description": f"Must be exactly {SERVICE}; Lambda rejects any other service.",
                },
                "start_time": {"type": "string", "description": "ISO-8601 UTC timestamp"},
                "end_time": {"type": "string", "description": "ISO-8601 UTC timestamp"},
                "max_results": {"type": "integer", "minimum": MIN_RESULTS, "maximum": MAX_RESULTS},
            },
            "required": ["data_type", "service", "start_time", "end_time", "max_results"],
        },
    }],
}
