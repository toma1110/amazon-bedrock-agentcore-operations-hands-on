from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from pathlib import Path
import sys

import pytest

S04_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(S04_DIR))

import s03_invocation_reader as reader

NOW = datetime(2026, 10, 24, 3, 0, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)
INVOCATION_ID = "12345678-1234-5678-1234-567812345678"
REQUEST = {
    "target_invocation_id": INVOCATION_ID,
    "target_timestamp_ms": NOW_MS - 30_000,
    "max_results": 3,
}


def test_valid_request_uses_a_fixed_five_minute_slice_and_seven_day_age_bound():
    parsed = reader.validate_request(REQUEST, now=NOW)
    assert parsed == {
        "target_invocation_id": INVOCATION_ID,
        "start_ms": NOW_MS - 180_000,
        "end_ms": NOW_MS,
        "limit": 3,
    }
    assert parsed["end_ms"] - parsed["start_ms"] <= int(reader.WINDOW.total_seconds() * 1000)


@pytest.mark.parametrize("change", [
    {"target_invocation_id": "not-a-uuid"},
    {"target_invocation_id": '" OR 1=1'},
    {"target_timestamp_ms": True},
    {"target_timestamp_ms": 0},
    {"target_timestamp_ms": NOW_MS + 1},
    {"target_timestamp_ms": 10**100},
    {"max_results": 0},
    {"max_results": 6},
    {"max_results": False},
    {"log_group": "/other/group"},
    {"runtime_id": "another-runtime"},
])
def test_rejects_unsupported_or_unbounded_input(change):
    with pytest.raises(reader.RequestRejected):
        reader.validate_request({**REQUEST, **change}, now=NOW)


def test_rejects_timestamp_older_than_seven_days():
    old = int((NOW - timedelta(days=7, milliseconds=1)).timestamp() * 1000)
    with pytest.raises(reader.RequestRejected, match="last 7 days"):
        reader.validate_request({**REQUEST, "target_timestamp_ms": old}, now=NOW)


def test_event_allowlist_matches_only_exact_s03_agent_invocation():
    good = json.dumps({
        "event": "agent_invocation",
        "invocation_id": INVOCATION_ID,
        "status": 200,
        "tool_call_count": 1,
        "tool_names": ["lookup"],
        "prompt": "must never be returned",
        "answer": "must never be returned",
        "tool_args": {"secret": "must never be returned"},
    })
    summary = reader._matches_event(good, INVOCATION_ID)
    assert summary == {
        "event": "agent_invocation",
        "target_invocation_id": INVOCATION_ID,
        "status": 200,
        "tool_call_count": 1,
        "tool_names": ["lookup"],
        "tool_names_truncated": False,
    }
    assert "prompt" not in summary and "answer" not in summary and "tool_args" not in summary

    wrong_type = good.replace("agent_invocation", "other_event")
    wrong_id = good.replace(INVOCATION_ID, "87654321-4321-8765-4321-876543218765")
    assert reader._matches_event(wrong_type, INVOCATION_ID) is None
    assert reader._matches_event(wrong_id, INVOCATION_ID) is None
    assert reader._matches_event("not-json", INVOCATION_ID) is None


def test_tool_name_output_is_bounded_by_utf8_bytes_and_marks_loss():
    long_name = "測" * 100
    summary = reader._matches_event(json.dumps({
        "event": "agent_invocation",
        "invocation_id": INVOCATION_ID,
        "status": 200,
        "tool_call_count": 1,
        "tool_names": [long_name],
    }), INVOCATION_ID)
    assert len(summary["tool_names"][0].encode("utf-8")) <= reader.MAX_TOOL_NAME_BYTES
    assert summary["tool_names_truncated"] is True
    assert summary["tool_names"][0] != long_name


def test_tool_name_count_is_bounded_and_marks_loss():
    summary = reader._matches_event(json.dumps({
        "event": "agent_invocation",
        "invocation_id": INVOCATION_ID,
        "status": 200,
        "tool_call_count": 8,
        "tool_names": [f"tool-{n}" for n in range(8)],
    }), INVOCATION_ID)
    assert summary["tool_names"] == [f"tool-{n}" for n in range(reader.MAX_RESULTS)]
    assert summary["tool_names_truncated"] is True


def test_log_query_is_fixed_to_exact_group_uuid_filter_bounded_time_and_small_page():
    request = reader.validate_request(REQUEST, now=NOW)
    expected = {
        "logGroupName": "/aws/bedrock-agentcore/runtimes/udemy_agentcore_s03-RsrJEUHMQ7-DEFAULT",
        "startTime": request["start_ms"],
        "endTime": request["end_ms"],
        "filterPattern": f'"{INVOCATION_ID}"',
        "limit": 3,
    }

    calls = []

    class FakeLogs:
        def filter_log_events(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                assert kwargs == expected
                return {"events": [], "nextToken": "more"}
            assert kwargs == {**expected, "nextToken": "more"}
            return {
                "events": [{
                    "timestamp": request["start_ms"],
                    "message": json.dumps({
                        "event": "agent_invocation",
                        "invocation_id": INVOCATION_ID,
                        "status": 200,
                        "tool_call_count": 1,
                        "tool_names": ["lookup"],
                        "prompt": "not returned",
                    }),
                }],
            }

    result = reader._read_invocation(FakeLogs(), request)
    assert result == {
        "found": True,
        "events": [{
            "event": "agent_invocation",
            "target_invocation_id": INVOCATION_ID,
            "status": 200,
            "tool_call_count": 1,
            "tool_names": ["lookup"],
            "tool_names_truncated": False,
            "target_timestamp_ms": request["start_ms"],
        }],
        "truncated": False,
        "note": None,
    }
    assert len(calls) == 2


def test_stops_after_page_cap_and_marks_partial_empty_result():
    class EndlessLogs:
        def __init__(self):
            self.calls = 0

        def filter_log_events(self, **_kwargs):
            self.calls += 1
            return {"events": [], "nextToken": f"token-{self.calls}"}

    client = EndlessLogs()
    result = reader._read_invocation(client, reader.validate_request(REQUEST, now=NOW))
    assert client.calls == reader.MAX_PAGES
    assert result["found"] is False
    assert result["truncated"] is True
    assert "Search incomplete" in result["note"]
    assert "absence is not established" in result["note"]
    assert "does not indicate runtime health" in result["note"]


def test_stops_on_repeated_pagination_token():
    class RepeatingLogs:
        def __init__(self):
            self.calls = 0

        def filter_log_events(self, **_kwargs):
            self.calls += 1
            return {"events": [], "nextToken": "same-token"}

    client = RepeatingLogs()
    result = reader._read_invocation(client, reader.validate_request(REQUEST, now=NOW))
    assert client.calls == 2
    assert result["found"] is False
    assert result["truncated"] is True
    assert "Search incomplete" in result["note"]
    assert "absence is not established" in result["note"]


def test_empty_result_is_not_described_as_healthy():
    class EmptyLogs:
        def filter_log_events(self, **_kwargs):
            return {"events": []}

    result = reader._read_invocation(EmptyLogs(), reader.validate_request(REQUEST, now=NOW))
    assert result["found"] is False
    assert result["truncated"] is False
    assert "No matching event found yet" in result["note"]
    assert "does not indicate runtime health" in result["note"]


def test_reader_serialized_response_stays_within_fixed_byte_budget():
    class ManyNames:
        def filter_log_events(self, **_kwargs):
            return {"events": [{
                "timestamp": REQUEST["target_timestamp_ms"],
                "message": json.dumps({
                    "event": "agent_invocation",
                    "invocation_id": INVOCATION_ID,
                    "status": 200,
                    "tool_call_count": 10,
                    "tool_names": ["x" * reader.MAX_TOOL_NAME_BYTES] * 10,
                }),
            }]}
    result = reader._read_invocation(ManyNames(), reader.validate_request(REQUEST, now=NOW))
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= reader.MAX_RESPONSE_BYTES
    assert result["events"][0]["tool_names_truncated"] is True


def test_result_limit_marks_truncation_without_returning_raw_event():
    request = reader.validate_request({**REQUEST, "max_results": 1}, now=NOW)
    message = json.dumps({
        "event": "agent_invocation",
        "invocation_id": INVOCATION_ID,
        "status": 200,
        "tool_call_count": 0,
        "tool_names": [],
        "prompt": "secret prompt",
    })

    class MoreLogs:
        def filter_log_events(self, **_kwargs):
            return {
                "events": [{"timestamp": request["start_ms"], "message": message}],
                "nextToken": "more",
            }

    result = reader._read_invocation(MoreLogs(), request)
    serialized = json.dumps(result)
    assert result["found"] is True
    assert result["truncated"] is True
    assert "result limit" in result["note"]
    assert "secret prompt" not in serialized


def test_handler_rejects_invalid_input_before_aws_access(monkeypatch):
    monkeypatch.setattr(reader.boto3, "Session", lambda **_kwargs: pytest.fail("AWS access is forbidden"))
    response = reader.lambda_handler({**REQUEST, "max_results": 99}, now=NOW)
    assert response["ok"] is False
    assert "max_results" in response["error"]


def test_handler_uses_only_tokyo_logs_client(monkeypatch):
    calls = []

    class FakeLogs:
        def filter_log_events(self, **_kwargs):
            return {"events": []}

    class FakeSession:
        def __init__(self, **kwargs):
            calls.append(("session", kwargs))

        def client(self, service):
            calls.append(("client", service))
            return FakeLogs()

    monkeypatch.setattr(reader.boto3, "Session", FakeSession)
    response = reader.lambda_handler(REQUEST, SimpleNamespace(aws_request_id="reader-lambda-call-1"), now=NOW)
    assert response["ok"] is True
    assert response["reader_execution"] == {"lambda_request_id": "reader-lambda-call-1"}
    assert calls == [
        ("session", {"region_name": "ap-northeast-1"}),
        ("client", "logs"),
    ]


def test_tool_schema_exposes_only_the_three_fixed_inputs():
    tool = reader.TOOL_SCHEMA["inlinePayload"][0]
    assert tool["name"] == "read_s03_invocation"
    schema = tool["inputSchema"]
    assert set(schema["properties"]) == {"target_invocation_id", "target_timestamp_ms", "max_results"}
    assert set(schema["required"]) == set(schema["properties"])
