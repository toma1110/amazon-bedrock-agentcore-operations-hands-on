from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
import sys

import pytest

S04_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(S04_DIR))

import cloudwatch_reader as reader

NOW = datetime(2026, 10, 16, 3, 0, tzinfo=timezone.utc)
REQUEST = {
    "data_type": "logs",
    "service": "catalog-api",
    "start_time": "2026-10-16T02:00:00Z",
    "end_time": "2026-10-16T03:00:00Z",
    "max_results": 10,
}


def test_valid_request_has_fixed_time_and_result_bounds():
    parsed = reader.validate_request(REQUEST, now=NOW)
    assert parsed == {
        "data_type": "logs",
        "start": NOW - timedelta(hours=1),
        "end": NOW,
        "limit": 10,
    }


@pytest.mark.parametrize("change", [
    {"service": "production-api"},
    {"data_type": "traces"},
    {"start_time": "2026-10-16T02:00:00"},
    {"start_time": "2026-10-16T02:00:00+09:00"},
    {"start_time": "2026-10-16T01:59:59Z"},
    {"end_time": "2026-10-16T03:00:01Z"},
    {"start_time": "2026-10-09T02:59:59Z"},
    {"max_results": 0},
    {"max_results": 51},
    {"max_results": True},
    {"extra": "field"},
])
def test_rejects_unsupported_or_unbounded_request(change):
    with pytest.raises(reader.RequestRejected):
        reader.validate_request({**REQUEST, **change}, now=NOW)


def test_rejects_missing_gateway_routing_metadata_without_cloudwatch_access(monkeypatch):
    monkeypatch.setenv("EXPECTED_GATEWAY_ID", "gateway-exact")
    monkeypatch.setenv("EXPECTED_TARGET_ID", "target-exact")
    monkeypatch.setattr(reader.boto3, "Session", lambda **_: pytest.fail("AWS access is forbidden"))

    response = reader.lambda_handler(REQUEST, SimpleNamespace(client_context=None))

    assert response == {"ok": False, "error": "unexpected gateway routing metadata"}


def test_rejects_wrong_gateway_context(monkeypatch):
    monkeypatch.setenv("EXPECTED_GATEWAY_ID", "gateway-exact")
    monkeypatch.setenv("EXPECTED_TARGET_ID", "target-exact")
    context = SimpleNamespace(client_context=SimpleNamespace(custom={
        "bedrockAgentCoreMessageVersion": "1.0",
        "bedrockAgentCoreGatewayId": "gateway-other",
        "bedrockAgentCoreTargetId": "target-exact",
        "bedrockAgentCoreToolName": "S04___read_synthetic_cloudwatch",
    }))
    monkeypatch.setattr(reader.boto3, "Session", lambda **_: pytest.fail("AWS access is forbidden"))

    response = reader.lambda_handler(REQUEST, context)

    assert response["ok"] is False
    assert "routing metadata" in response["error"]


def test_rejects_wrong_service_before_cloudwatch_access(monkeypatch):
    monkeypatch.setenv("EXPECTED_GATEWAY_ID", "gateway-exact")
    monkeypatch.setenv("EXPECTED_TARGET_ID", "target-exact")
    context = SimpleNamespace(client_context=SimpleNamespace(custom={
        "bedrockAgentCoreMessageVersion": "1.0",
        "bedrockAgentCoreGatewayId": "gateway-exact",
        "bedrockAgentCoreTargetId": "target-exact",
        "bedrockAgentCoreToolName": "S04___read_synthetic_cloudwatch",
    }))
    monkeypatch.setattr(reader.boto3, "Session", lambda **_: pytest.fail("AWS access is forbidden"))

    response = reader.lambda_handler({**REQUEST, "service": "other-service"}, context)

    assert response["ok"] is False
    assert "outside" in response["error"]


def test_logs_call_is_scoped_to_fixed_log_group_time_and_limit():
    class FakeLogs:
        def filter_log_events(self, **kwargs):
            assert kwargs == {
                "logGroupName": reader.LOG_GROUP,
                "startTime": int((NOW - timedelta(hours=1)).timestamp() * 1000),
                "endTime": int(NOW.timestamp() * 1000),
                "limit": 2,
            }
            return {"events": [{"timestamp": 123, "message": "synthetic"}], "nextToken": "more"}

    result = reader._read_logs(FakeLogs(), NOW - timedelta(hours=1), NOW, 2)
    assert result["source"]["log_group"] == reader.LOG_GROUP
    assert result["truncated"] is True
    assert len(result["events"]) == 1


def test_metric_call_is_scoped_to_fixed_metric_and_dimension():
    class FakeCloudWatch:
        def get_metric_data(self, **kwargs):
            assert kwargs["MetricDataQueries"][0]["MetricStat"]["Metric"] == {
                "Namespace": reader.METRIC_NAMESPACE,
                "MetricName": reader.METRIC_NAME,
                "Dimensions": [{"Name": "Service", "Value": reader.SERVICE}],
            }
            assert kwargs["MaxDatapoints"] == 2
            return {"MetricDataResults": [{
                "Id": "http5xx",
                "Timestamps": [NOW - timedelta(minutes=2), NOW - timedelta(minutes=1)],
                "Values": [3.0, 1.0],
                "StatusCode": "Complete",
            }]}

    result = reader._read_metrics(FakeCloudWatch(), NOW - timedelta(hours=1), NOW, 2)
    assert result["source"]["namespace"] == reader.METRIC_NAMESPACE
    assert result["source"]["metric_name"] == reader.METRIC_NAME
    assert [point["value"] for point in result["datapoints"]] == [3.0, 1.0]


def test_metric_result_missing_is_not_reported_as_empty_success():
    class EmptyCloudWatch:
        def get_metric_data(self, **kwargs):
            return {"MetricDataResults": []}

    with pytest.raises(RuntimeError, match="no result"):
        reader._read_metrics(EmptyCloudWatch(), NOW - timedelta(hours=1), NOW, 2)


def test_metric_partial_data_status_is_preserved_for_caller():
    class PartialCloudWatch:
        def get_metric_data(self, **kwargs):
            return {"MetricDataResults": [{
                "Id": "http5xx", "Timestamps": [], "Values": [], "StatusCode": "PartialData",
            }]}

    result = reader._read_metrics(PartialCloudWatch(), NOW - timedelta(hours=1), NOW, 2)
    assert result["status"] == "PartialData"
