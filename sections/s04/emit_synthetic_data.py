"""Create one identifiable synthetic S04 log event and classic CloudWatch metric."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import boto3

from cloudwatch_reader import LOG_GROUP, METRIC_NAME, METRIC_NAMESPACE, SERVICE

REGION = "ap-northeast-1"
STATE_PATH = Path(__file__).with_name(".s04-seed-state.json")


def _save_state(path: Path, state: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _event_exists(logs: Any, state: dict[str, Any]) -> bool:
    timestamp_ms = state["timestamp_ms"]
    response = logs.filter_log_events(
        logGroupName=LOG_GROUP,
        logStreamNames=[state["stream_name"]],
        startTime=timestamp_ms - 60_000,
        endTime=timestamp_ms + 60_000,
        limit=50,
    )
    return any(
        json.loads(item.get("message", "{}")).get("request_id") == state["request_id"]
        for item in response.get("events", [])
    )


def _metric_exists(cloudwatch: Any, state: dict[str, Any]) -> bool:
    timestamp = datetime.fromisoformat(state["timestamp"])
    response = cloudwatch.get_metric_data(
        MetricDataQueries=[{
            "Id": "s04seed",
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
        StartTime=timestamp.replace(second=0, microsecond=0),
        EndTime=timestamp.replace(second=0, microsecond=0) + timedelta(minutes=1),
        ScanBy="TimestampAscending",
    )
    result = next((item for item in response.get("MetricDataResults", []) if item.get("Id") == "s04seed"), {})
    return any(value >= 1 for value in result.get("Values", []))


def emit(expected_account: str, state_path: Path = STATE_PATH) -> None:
    if len(expected_account) != 12 or not expected_account.isdigit():
        raise ValueError("expected account must be a 12-digit account ID")
    session = boto3.Session(region_name=REGION)
    identity = session.client("sts").get_caller_identity()
    if identity["Account"] != expected_account:
        raise ValueError("AWS account differs from the explicitly expected account; stopped")

    logs = session.client("logs")
    cloudwatch = session.client("cloudwatch")
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("account") != expected_account or state.get("region") != REGION:
            raise RuntimeError("seed state belongs to a different account or region; do not overwrite it")
    else:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        state = {
            "account": expected_account,
            "region": REGION,
            "status": "planned",
            "request_id": f"s04-demo-{int(now.timestamp())}",
            "stream_name": f"synthetic-{int(now.timestamp())}",
            "timestamp": now.isoformat().replace("+00:00", "Z"),
            "timestamp_ms": int(now.timestamp() * 1000),
        }
        _save_state(state_path, state)

    # If a write response was lost, inspect before progressing. Never replay an
    # ambiguous write automatically: CloudWatch metric writes are not idempotent.
    if state["status"] == "planned":
        state["status"] = "stream_create_requested"
        _save_state(state_path, state)
        logs.create_log_stream(logGroupName=LOG_GROUP, logStreamName=state["stream_name"])
        state["status"] = "stream_created"
        _save_state(state_path, state)
    elif state["status"] == "stream_create_requested":
        streams = logs.describe_log_streams(
            logGroupName=LOG_GROUP, logStreamNamePrefix=state["stream_name"], limit=5
        ).get("logStreams", [])
        if any(item.get("logStreamName") == state["stream_name"] for item in streams):
            state["status"] = "stream_created"
            _save_state(state_path, state)
        else:
            raise RuntimeError("log stream creation is unresolved; inspect it before continuing")

    if state["status"] == "stream_created":
        event = {
            "dataset": "synthetic-training-data",
            "service": SERVICE,
            "status_code": 503,
            "request_id": state["request_id"],
            "observed_at": state["timestamp"],
        }
        state["status"] = "event_write_requested"
        _save_state(state_path, state)
        logs.put_log_events(
            logGroupName=LOG_GROUP,
            logStreamName=state["stream_name"],
            logEvents=[{"timestamp": state["timestamp_ms"], "message": json.dumps(event)}],
        )
        state["status"] = "event_written"
        _save_state(state_path, state)
    elif state["status"] == "event_write_requested":
        if _event_exists(logs, state):
            state["status"] = "event_written"
            _save_state(state_path, state)
        else:
            raise RuntimeError("log event write is unresolved; inspect it before continuing")

    if state["status"] == "event_written":
        state["status"] = "metric_write_requested"
        _save_state(state_path, state)
        cloudwatch.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=[{
                "MetricName": METRIC_NAME,
                "Dimensions": [{"Name": "Service", "Value": SERVICE}],
                "Timestamp": datetime.fromisoformat(state["timestamp"].replace("Z", "+00:00")),
                "Value": 1,
                "Unit": "Count",
            }],
        )
        state["status"] = "metric_written"
        _save_state(state_path, state)
    elif state["status"] == "metric_write_requested":
        if _metric_exists(cloudwatch, state):
            state["status"] = "metric_written"
            _save_state(state_path, state)
        else:
            raise RuntimeError("metric write is unresolved; inspect CloudWatch before continuing")

    if state["status"] != "metric_written":
        raise RuntimeError("seed data is incomplete; do not repeat or create a new state file")
    print(f"AWS account: {identity['Account']} (expected account matched)")
    print(f"Region: {REGION}")
    print(f"Synthetic log group: {LOG_GROUP}")
    print(f"Synthetic metric: {METRIC_NAMESPACE}/{METRIC_NAME}, Service={SERVICE}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-account", required=True, help="12-digit approved learning account")
    parser.add_argument("--state-file", type=Path, default=STATE_PATH, help="local recovery state; preserve it for retries")
    args = parser.parse_args()
    emit(args.expected_account, args.state_file)


if __name__ == "__main__":
    main()
