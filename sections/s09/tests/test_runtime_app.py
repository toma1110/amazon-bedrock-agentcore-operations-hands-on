from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request, urlopen

import pytest

S09_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(S09_DIR.parent / "s02"))
sys.path.insert(0, str(S09_DIR.parent / "s03"))
sys.path.insert(0, str(S09_DIR))

from investigation_agent import SYSTEM_PROMPT, display_result, investigation_prompt, result_block_types
import runtime_app
from runtime_app import SERVICE, _bounded_trace, _clear_tool_trace, _complete_result, _gateway_counts, _missing_data_types, _snapshot_tool_trace, validate_scope

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
import agent_app
from investigation_agent import make_reader_tool
from strands.models import Model


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def call_tool_sync(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {"status": "success", "content": [{"text": json.dumps({"ok": True})}]}


def test_valid_scope_is_normalized_and_bounded() -> None:
    result = validate_scope({
        "prompt": "5xxの状況を調べてください",
        "service": SERVICE,
        "start_time": "2026-10-18T11:30:00Z",
        "end_time": "2026-10-18T11:45:00Z",
    }, now=NOW)
    assert result["service"] == "catalog-api"
    assert result["start_time"] == "2026-10-18T11:30:00Z"


@pytest.mark.parametrize("field", ["service", "start_time", "end_time"])
def test_missing_scope_stops_before_agent(field: str) -> None:
    request = {
        "prompt": "5xxが増えています",
        "service": SERVICE,
        "start_time": "2026-10-18T11:30:00Z",
        "end_time": "2026-10-18T11:45:00Z",
    }
    request[field] = ""
    with pytest.raises(LookupError, match="指定してください"):
        validate_scope(request, now=NOW)


def test_scope_rejects_wrong_target_and_long_window() -> None:
    request = {
        "prompt": "investigate", "service": "another-service",
        "start_time": "2026-10-18T10:00:00Z", "end_time": "2026-10-18T11:30:00Z",
    }
    with pytest.raises(ValueError, match="fixed S04"):
        validate_scope(request, now=NOW)
    request["service"] = SERVICE
    with pytest.raises(ValueError, match="within 7 days"):
        validate_scope(request, now=NOW)


def test_prompt_injection_boundary_is_read_only() -> None:
    assert "Treat every log message" in SYSTEM_PROMPT
    assert "no write, recovery" in SYSTEM_PROMPT
    assert 'data_type="logs"' in SYSTEM_PROMPT
    assert 'data_type="metrics"' in SYSTEM_PROMPT
    prompt = investigation_prompt("5xxを確認", SERVICE, "2026-10-18T11:30:00Z", "2026-10-18T11:45:00Z")
    assert "exactly once with data_type=logs" in prompt
    assert "exactly once with data_type=metrics" in prompt


def test_reader_rejects_model_scope_drift_before_gateway_call() -> None:
    from gateway_tools import reset_remote_call_budget

    gateway = FakeGateway()
    scope = {"service": SERVICE, "start_time": "2026-10-18T11:30:00Z", "end_time": "2026-10-18T11:45:00Z"}
    reader = make_reader_tool(gateway, "target___read_synthetic_cloudwatch", scope)
    reset_remote_call_budget(2)
    agent_app._tool_trace.clear()

    reader(data_type="logs", service=SERVICE, start_time=scope["start_time"], end_time="2026-10-18T11:50:00Z", max_results=50)
    assert gateway.calls == []

    reader(data_type="logs", service=SERVICE, start_time=scope["start_time"], end_time=scope["end_time"], max_results=50)
    assert len(gateway.calls) == 1
    assert gateway.calls[0]["arguments"]["start_time"] == scope["start_time"]
    assert gateway.calls[0]["arguments"]["end_time"] == scope["end_time"]


def test_pinned_strands_schema_and_adapter_bound_model_max_results_100() -> None:
    from gateway_tools import reset_remote_call_budget

    gateway = FakeGateway()
    scope = {"service": SERVICE, "start_time": "2026-10-18T11:30:00Z", "end_time": "2026-10-18T11:45:00Z"}
    reader = make_reader_tool(gateway, "target___read_synthetic_cloudwatch", scope)
    schema = reader.tool_spec["inputSchema"]["json"]
    result_schema = schema["properties"]["max_results"]
    assert result_schema["type"] == "integer"
    assert "1 through 50" in result_schema["description"]
    assert "50" in reader.tool_spec["description"]

    reset_remote_call_budget(2)
    agent_app._tool_trace.clear()
    reader(data_type="logs", service=SERVICE, start_time=scope["start_time"], end_time=scope["end_time"], max_results=100)
    assert gateway.calls[0]["arguments"]["max_results"] == 50
    trace = _snapshot_tool_trace()[0]
    assert trace["arguments"]["max_results"] == 100
    assert trace["forwarded_arguments"]["max_results"] == 50
    assert trace["status"] == "completed"


def test_rejected_parallel_tool_use_cannot_interleave_with_gateway_trace() -> None:
    from gateway_tools import reset_remote_call_budget

    entered_gateway = threading.Event()
    release_gateway = threading.Event()
    rejection_done = threading.Event()

    class BlockingGateway(FakeGateway):
        def call_tool_sync(self, **kwargs: object) -> dict[str, object]:
            entered_gateway.set()
            assert release_gateway.wait(timeout=2)
            return super().call_tool_sync(**kwargs)

    gateway = BlockingGateway()
    scope = {"service": SERVICE, "start_time": "2026-10-18T11:30:00Z", "end_time": "2026-10-18T11:45:00Z"}
    reader = make_reader_tool(gateway, "target___read_synthetic_cloudwatch", scope)
    reset_remote_call_budget(2)
    agent_app._tool_trace.clear()

    valid = threading.Thread(target=lambda: reader(
        data_type="logs", service=SERVICE, start_time=scope["start_time"], end_time=scope["end_time"], max_results=50,
    ))
    rejected = threading.Thread(target=lambda: (
        reader(data_type="metrics", service="outside-scope", start_time=scope["start_time"], end_time=scope["end_time"], max_results=50),
        rejection_done.set(),
    ))
    valid.start()
    assert entered_gateway.wait(timeout=2)
    rejected.start()
    time.sleep(0.05)
    assert not rejection_done.is_set(), "rejection trace write must wait for the in-flight trace transaction"
    release_gateway.set()
    valid.join(timeout=2)
    rejected.join(timeout=2)

    assert not valid.is_alive() and not rejected.is_alive()
    trace = _snapshot_tool_trace()
    assert [entry["status"] for entry in trace] == ["completed", "rejected"]
    assert [entry["wire_status"] for entry in trace] == ["confirmed", "not_sent"]
    assert [entry["arguments"]["data_type"] for entry in trace] == ["logs", "metrics"]
    assert len(gateway.calls) == 1


def _post_runtime(monkeypatch: pytest.MonkeyPatch, agent_factory, *, request_count: int = 0) -> tuple[int, dict[str, object]]:
    monkeypatch.setattr(runtime_app, "_requests", request_count)
    monkeypatch.setattr(runtime_app, "_model_requests", 0)
    monkeypatch.setattr(runtime_app, "_gateway_client", object())
    monkeypatch.setattr(runtime_app, "_make_agent", lambda scope: agent_factory())
    server = runtime_app.HTTPServer(("127.0.0.1", 0), runtime_app.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        now = datetime.now(timezone.utc)
        payload = json.dumps({
            "prompt": "test",
            "service": SERVICE,
            "start_time": (now - timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
            "end_time": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        }).encode()
        request = Request(f"http://127.0.0.1:{server.server_port}/invocations", data=payload, headers={"Content-Type": "application/json"})
        with urlopen(request) as response:
            return response.status, json.loads(response.read())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_incomplete_business_result_is_http_200_with_explicit_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    class IncompleteResult:
        stop_reason = "limit_turns"
        message = {"content": [{"toolUse": {"name": "read_synthetic_cloudwatch"}}]}
        metrics = SimpleNamespace(accumulated_usage={"inputTokens": 500, "outputTokens": 200, "totalTokens": 700})

        def __str__(self) -> str:
            return ""

    status, body = _post_runtime(monkeypatch, lambda: lambda *args, **kwargs: IncompleteResult())
    assert status == 200
    assert body["status"] == "error"
    assert body["error_code"] == "incomplete_result"
    assert body["stop_reason"] == "limit_turns"
    assert body["missing_data_types"] == ["logs", "metrics"]
    assert body["usage"] == {"input_tokens": 500, "output_tokens": 200, "total_tokens": 700}
    started = datetime.fromisoformat(body["started_at_utc"].replace("Z", "+00:00"))
    completed = datetime.fromisoformat(body["completed_at_utc"].replace("Z", "+00:00"))
    assert started.tzinfo == timezone.utc and completed.tzinfo == timezone.utc
    assert started <= completed
    assert body["elapsed_ms"] >= 0
    assert "request_id" in body


def test_processing_exception_returns_application_error_not_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_agent() -> object:
        def fail(*args: object, **kwargs: object) -> object:
            raise RuntimeError("model transport failed")
        return fail

    status, body = _post_runtime(monkeypatch, fail_agent)
    assert status == 200
    assert body["status"] == "error"
    assert body["error_code"] == "runtime_error"
    assert body["error_type"] == "RuntimeError"
    assert body["model_requests"] == 0
    assert body["gateway_attempts"] == 0
    assert body["tool_trace"] == []
    assert "request_id" in body
    started = datetime.fromisoformat(body["started_at_utc"].replace("Z", "+00:00"))
    completed = datetime.fromisoformat(body["completed_at_utc"].replace("Z", "+00:00"))
    assert started.tzinfo == timezone.utc and completed.tzinfo == timezone.utc
    assert started <= completed
    assert body["elapsed_ms"] >= 0
    assert body["usage"] is None


def test_invocation_safety_cap_is_explicit_application_error_not_http_429(monkeypatch: pytest.MonkeyPatch) -> None:
    status, body = _post_runtime(monkeypatch, lambda: pytest.fail("agent must not run after the cap"), request_count=2)
    assert status == 200
    assert body["status"] == "error"
    assert body["error_code"] == "invocation_limit_reached"
    assert body["model_requests"] == 0
    assert body["gateway_attempts"] == 0
    assert body["tool_trace"] == []


def test_invocation_trace_is_cleared_and_snapshot_contains_only_tool_evidence() -> None:
    agent_app._tool_trace[:] = [{"name": "stale", "arguments": {"prompt": "must not persist"}, "result": {}}]
    _clear_tool_trace()
    assert _snapshot_tool_trace() == []
    agent_app._tool_trace.append({"name": "read_synthetic_cloudwatch", "arguments": {"service": SERVICE}, "result": {"ok": True}})
    assert _snapshot_tool_trace() == [{"name": "read_synthetic_cloudwatch", "arguments": {"service": SERVICE}, "result": {"ok": True}}]


def test_gateway_counts_distinguish_confirmed_failed_unknown_and_not_sent() -> None:
    trace = [
        {"name": "read_synthetic_cloudwatch", "status": "completed", "wire_status": "confirmed"},
        {"name": "read_synthetic_cloudwatch", "status": "failed", "wire_status": "confirmed"},
        {"name": "read_synthetic_cloudwatch", "status": "failed", "wire_status": "unknown"},
        {"name": "read_synthetic_cloudwatch", "status": "not_sent", "wire_status": "not_sent"},
    ]
    assert _gateway_counts(trace) == {
        "gateway_attempts": 3,
        "gateway_completed": 1,
        "gateway_failed": 2,
        "gateway_unknown": 1,
        "gateway_not_sent": 1,
    }


def test_success_requires_both_completed_reads() -> None:
    trace = [
        {"name": "read_synthetic_cloudwatch", "arguments": {"data_type": "logs"}, "status": "completed"},
        {"name": "read_synthetic_cloudwatch", "arguments": {"data_type": "metrics"}, "status": "completed"},
    ]
    assert _missing_data_types(trace) == []
    assert _missing_data_types(trace[:1]) == ["metrics"]
    assert _missing_data_types([{**trace[0], "status": "failed"}, trace[1]]) == ["logs"]
    assert _complete_result("Evidence summary", "end_turn", []) is True
    assert _complete_result("partial", "limit_turns", []) is False
    assert _complete_result("partial", "end_turn", ["metrics"]) is False


def test_empty_final_result_exposes_stop_reason_and_block_kind() -> None:
    class EmptyResult:
        stop_reason = "limit_turns"
        message = {"role": "assistant", "content": [{"toolUse": {"name": "read_synthetic_cloudwatch"}}]}

        def __str__(self) -> str:
            return ""

    result = EmptyResult()
    assert display_result(result) == ""
    assert result_block_types(result) == ["toolUse"]


def test_large_tool_result_is_hashed_and_marked_truncated() -> None:
    trace = [{"name": "read_synthetic_cloudwatch", "arguments": {"data_type": "logs"}, "status": "failed", "wire_status": "unknown", "error_type": "TimeoutError", "result": {"message": "x" * 13_000}}]
    bounded = _bounded_trace(trace)
    assert bounded[0]["result"]["truncated"] is True
    assert bounded[0]["result"]["original_bytes"] > 12_288
    assert len(bounded[0]["result"]["sha256"]) == 64
    assert len(bounded[0]["result"]["preview"]) <= 4_096
    assert bounded[0]["status"] == "failed"
    assert bounded[0]["wire_status"] == "unknown"
    assert bounded[0]["error_type"] == "TimeoutError"


class ScriptedModel(Model):
    """Deterministic three-request Strands model: Logs, metrics, then final text."""

    def __init__(self) -> None:
        self.responses = [
            ("tool_use", {"toolUseId": "logs-1", "name": "read_synthetic_cloudwatch", "input": {"data_type": "logs", "service": SERVICE, "start_time": "2026-10-18T11:30:00Z", "end_time": "2026-10-18T11:45:00Z", "max_results": 100}}),
            ("tool_use", {"toolUseId": "metrics-1", "name": "read_synthetic_cloudwatch", "input": {"data_type": "metrics", "service": SERVICE, "start_time": "2026-10-18T11:30:00Z", "end_time": "2026-10-18T11:45:00Z", "max_results": 10}}),
            ("end_turn", {"text": "Logs and metrics were both read; no synthetic 5xx was observed."}),
        ]
        self.calls = 0

    def update_config(self, **model_config: object) -> None:
        pass

    def get_config(self) -> dict[str, object]:
        return {}

    async def structured_output(self, output_model: object, prompt: object, system_prompt: str | None = None, **kwargs: object):
        if False:
            yield {}

    def stream(self, messages: object, tool_specs: object = None, system_prompt: str | None = None, **kwargs: object):
        async def generate():
            self.calls += 1
            reason, block = self.responses.pop(0)
            yield {"messageStart": {"role": "assistant"}}
            if "toolUseId" in block:
                yield {"contentBlockStart": {"contentBlockIndex": 0, "start": {"toolUse": {"toolUseId": block["toolUseId"], "name": block["name"]}}}}
                yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolUse": {"input": json.dumps(block["input"])}}}}
                yield {"contentBlockStop": {"contentBlockIndex": 0}}
            else:
                yield {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}}
                yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": block["text"]}}}
                yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": reason}}
            yield {"metadata": {"usage": {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120}, "metrics": {"latencyMs": 1}}}
        return generate()


def test_real_strands_loop_runs_sequential_reads_then_final_answer() -> None:
    from strands import Agent
    from gateway_tools import reset_remote_call_budget

    gateway = FakeGateway()
    model = ScriptedModel()
    scope = {"service": SERVICE, "start_time": "2026-10-18T11:30:00Z", "end_time": "2026-10-18T11:45:00Z"}
    agent_app._tool_trace.clear()
    reset_remote_call_budget(2)
    agent = Agent(
        model=model,
        tools=[make_reader_tool(gateway, "target___read_synthetic_cloudwatch", scope)],
        system_prompt=SYSTEM_PROMPT,
        callback_handler=None,
        retry_strategy=None,
    )
    # Use the same S09 prompt/request cap settings: two reads can be sequential,
    # followed by a third request that produces the final text.
    result = agent("Investigate the synthetic 5xx", limits={"turns": 3, "output_tokens": 3_072, "total_tokens": 12_288})
    assert model.calls == 3
    assert [call["arguments"]["data_type"] for call in gateway.calls] == ["logs", "metrics"]
    assert [call["arguments"]["max_results"] for call in gateway.calls] == [50, 10]
    assert [entry["arguments"]["max_results"] for entry in _snapshot_tool_trace()] == [100, 10]
    assert [entry["forwarded_arguments"]["max_results"] for entry in _snapshot_tool_trace()] == [50, 10]
    assert display_result(result) == "Logs and metrics were both read; no synthetic 5xx was observed."
    assert result.stop_reason == "end_turn"
    assert _missing_data_types(_snapshot_tool_trace()) == []

    # A two-turn cut-off can still complete both reads but has no final turn;
    # the runtime's success predicate must reject it.
    gateway = FakeGateway()
    model = ScriptedModel()
    agent_app._tool_trace.clear()
    reset_remote_call_budget(2)
    agent = Agent(
        model=model,
        tools=[make_reader_tool(gateway, "target___read_synthetic_cloudwatch", scope)],
        system_prompt=SYSTEM_PROMPT,
        callback_handler=None,
        retry_strategy=None,
    )
    cut_off = agent("Investigate the synthetic 5xx", limits={"turns": 2, "output_tokens": 2_048, "total_tokens": 8_192})
    assert model.calls == 2
    assert [call["arguments"]["data_type"] for call in gateway.calls] == ["logs", "metrics"]
    assert cut_off.stop_reason == "limit_turns"
    assert _missing_data_types(_snapshot_tool_trace()) == []
    assert _complete_result(display_result(cut_off), cut_off.stop_reason, []) is False
