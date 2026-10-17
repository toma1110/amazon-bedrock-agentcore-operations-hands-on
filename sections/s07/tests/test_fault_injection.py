from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

S07_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(S07_DIR))

from fault_injection import (
    ExternalStubError,
    ToolExecutionError,
    ToolInputError,
    call_checked_tool,
    call_local_api_stub,
    validate_tool_arguments,
)


def test_tool_schema_rejects_invalid_arguments_before_operation_runs():
    called = []

    with pytest.raises(ToolInputError, match="service must be"):
        call_checked_tool(
            {"service": "unknown", "topic": "5xx"},
            lambda service, topic: called.append((service, topic)),
        )

    assert called == []


def test_valid_tool_input_is_bounded_and_rejects_extra_values():
    called = []
    with pytest.raises(ToolInputError, match="only service and topic"):
        call_checked_tool(
            {"service": "catalog-api", "topic": "5xx", "extra": "rejected"},
            lambda service, topic: called.append((service, topic)),
        )
    assert called == []
    result = call_checked_tool(
        {"service": "catalog-api", "topic": "5xx"},
        lambda service, topic: called.append((service, topic)) or {"found": True},
    )

    assert result == {"found": True}
    assert called == [("catalog-api", "5xx")]
    assert validate_tool_arguments({"service": "report-worker", "topic": "throttle"}) == {
        "service": "report-worker",
        "topic": "throttle",
    }


def test_tool_internal_error_is_distinct_and_does_not_echo_input():
    with pytest.raises(ToolExecutionError, match="simulated tool implementation failure") as caught:
        call_checked_tool(
            {"service": "catalog-api", "topic": "5xx"},
            lambda service, topic: None,
            inject_internal_error=True,
        )

    assert "catalog-api" not in str(caught.value)
    assert "5xx" not in str(caught.value)


def test_stub_returns_only_synthetic_local_response():
    result = call_local_api_stub("success")

    assert result["status"] == 200
    assert result["attempts"] == 1
    assert result["destination"] == "localhost"
    assert result["body"] == {"synthetic": True, "status": 200}
    assert "127.0.0.1" not in json.dumps(result)


@pytest.mark.parametrize(("scenario", "status"), [("429", 429), ("5xx", 503)])
def test_stub_retries_transient_status_once_only(scenario: str, status: int):
    with pytest.raises(ExternalStubError) as caught:
        call_local_api_stub(scenario)

    assert caught.value.status == status
    assert caught.value.attempts == 2


def test_stub_obeys_single_attempt_setting():
    with pytest.raises(ExternalStubError) as caught:
        call_local_api_stub("5xx", max_retries=0)

    assert caught.value.status == 503
    assert caught.value.attempts == 1


def test_delayed_stub_times_out_without_retrying():
    with pytest.raises(TimeoutError, match="timed out after 100 ms"):
        call_local_api_stub("delay", timeout_ms=100)


@pytest.mark.parametrize(
    ("scenario", "kwargs"),
    [
        ("not-allowed", {}),
        ("success", {"timeout_ms": 0}),
        ("success", {"timeout_ms": 501}),
        ("success", {"max_retries": 2}),
    ],
)
def test_stub_rejects_unbounded_or_unknown_controls(scenario: str, kwargs: dict[str, int]):
    with pytest.raises(ToolInputError):
        call_local_api_stub(scenario, **kwargs)


def test_json_size_and_argument_type_are_checked():
    with pytest.raises(ToolInputError, match="JSON object"):
        validate_tool_arguments(["catalog-api", "5xx"])
    with pytest.raises(ToolInputError, match="4096-byte"):
        validate_tool_arguments({"service": "catalog-api", "topic": "5xx", "extra": "x" * 5_000})
