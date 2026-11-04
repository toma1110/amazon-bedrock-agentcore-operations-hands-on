import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace

S04_DIR = Path(__file__).resolve().parents[1]
S02_DIR = S04_DIR.parent / "s02"
sys.path.insert(0, str(S02_DIR))
sys.path.insert(0, str(S04_DIR))

spec = importlib.util.spec_from_file_location("s04_runtime_candidate", S04_DIR / "s04_runtime_app.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
import s04_gateway_tools as gateway


def test_dedicated_entrypoint_has_s03_reader_prompt_and_fixed_model_budget():
    assert "read_s03_invocation" in runtime.SYSTEM_PROMPT
    assert "truncated=true" in runtime.SYSTEM_PROMPT
    assert "target S03 invocation" in runtime.SYSTEM_PROMPT
    assert (runtime.MAX_AGENT_TURNS, runtime.MAX_OUTPUT_TOKENS, runtime.MAX_TOTAL_TOKENS) == (2, 512, 2048)


def test_runtime_rejects_invalid_payload_without_constructing_agent(monkeypatch):
    monkeypatch.setattr(runtime, "_get_agent", lambda: pytest.fail("must not call model"))
    status, result = runtime._handle(b'{"not_prompt":true}')
    assert status == 400
    assert result["error_type"] == "RequestValidationError"


def test_agent_factory_receives_s04_prompt_no_retry_guard_and_exact_tool(monkeypatch):
    calls = []
    gateway_tool_name = "reader-target___read_s03_invocation"

    class FakeGateway:
        def __enter__(self):
            return self

        def list_tools_sync(self):
            return [SimpleNamespace(tool_name=gateway_tool_name)]

    monkeypatch.setattr(runtime, "_agent", None)
    monkeypatch.setattr(runtime, "_gateway", None)
    monkeypatch.setenv("S04_GATEWAY_URL", "https://gateway.example/mcp")
    monkeypatch.setenv("S04_GATEWAY_TOOL_NAME", gateway_tool_name)
    monkeypatch.setattr(gateway, "create_gateway_client", lambda *_args: FakeGateway())
    tool_calls = []
    monkeypatch.setattr(gateway, "create_reader_tool", lambda *args: tool_calls.append(args) or "reader-tool")
    monkeypatch.setattr(runtime.agent_app, "_make_agent", lambda *args, **kwargs: calls.append((args, kwargs)) or "agent")

    assert runtime._get_agent() == "agent"
    assert tool_calls == [(runtime._gateway, gateway_tool_name)]
    kwargs = calls[0][1]
    assert kwargs["system_prompt"] == runtime.SYSTEM_PROMPT
    assert "tool_names_truncated" in kwargs["system_prompt"]
    assert kwargs["s06_model_request_guard"] is runtime._model_request_guard
    assert kwargs["s06_max_tokens"] == runtime.MAX_OUTPUT_TOKENS


def test_isolated_packager_places_dedicated_modules_at_runtime_import_names():
    spec = importlib.util.spec_from_file_location("s04_package_artifacts", S04_DIR / "package_artifacts.py")
    packager = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(packager)
    assert packager.S03_READER_RUNTIME_FILES["runtime_app.py"] == S04_DIR / "s04_runtime_app.py"
    assert packager.S03_READER_RUNTIME_FILES["s04_gateway_tools.py"] == S04_DIR / "s04_gateway_tools.py"
    assert "from s04_gateway_tools import" in (S04_DIR / "s04_runtime_app.py").read_text(encoding="utf-8")


def test_s04_gateway_wrapper_makes_one_exact_tool_call_and_never_retries():
    calls = []

    class FakeClient:
        def call_tool_sync(self, **kwargs):
            calls.append(kwargs)
            return {"status": "success", "content": [{"text": '{"ok":true,"data":{"found":false}}'}]}

    gateway.reset_remote_call_budget()
    timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - 30_000
    args = {"target_invocation_id": "12345678-1234-5678-1234-567812345678", "target_timestamp_ms": timestamp_ms, "max_results": 2}
    result = gateway.read_s03_invocation(FakeClient(), "reader-tool", **args)
    second = gateway.read_s03_invocation(FakeClient(), "reader-tool", **args)
    assert json.loads(result) == {"ok": True, "data": {"found": False}}
    assert json.loads(second)["ok"] is False
    assert len(calls) == 1
    assert calls[0]["name"] == "reader-tool"
    assert calls[0]["arguments"] == args


def test_s04_gateway_wrapper_rejects_invalid_query_without_network_call():
    class NeverCall:
        def call_tool_sync(self, **_kwargs):
            pytest.fail("invalid inputs must be rejected before the Gateway call")

    gateway.reset_remote_call_budget()
    result = gateway.read_s03_invocation(
        NeverCall(), "reader-tool", target_invocation_id="bad", target_timestamp_ms=1, max_results=99,
    )
    assert json.loads(result)["ok"] is False


def test_s04_gateway_wrapper_fails_closed_on_oversized_response():
    class Oversized:
        def call_tool_sync(self, **_kwargs):
            return {"status": "success", "content": [{"text": "x" * (gateway.MAX_GATEWAY_RESPONSE_BYTES + 1)}]}

    gateway.reset_remote_call_budget()
    timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - 30_000
    result = gateway.read_s03_invocation(
        Oversized(), "reader-tool", target_invocation_id="12345678-1234-5678-1234-567812345678",
        target_timestamp_ms=timestamp_ms, max_results=1,
    )
    assert json.loads(result)["error"] == "S04 reader Gateway response exceeded the fixed byte limit"


def test_each_runtime_invocation_resets_one_gateway_call_and_two_no_retry_model_attempts():
    calls = []

    class FakeClient:
        def call_tool_sync(self, **kwargs):
            calls.append(kwargs)
            return {"status": "success", "content": [{"text": '{"ok":true}'}]}

    timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - 30_000
    args = {"target_invocation_id": "12345678-1234-5678-1234-567812345678", "target_timestamp_ms": timestamp_ms, "max_results": 1}
    client = FakeClient()
    for _ in range(2):
        runtime._begin_invocation()
        gateway.read_s03_invocation(client, "reader-tool", **args)
        gateway.read_s03_invocation(client, "reader-tool", **args)
    assert len(calls) == 2

    guard = runtime._ModelRequestGuard()
    guard.begin_invocation()
    guard.before_send(type("Request", (), {"body": b"one"})())
    guard.before_send(type("Request", (), {"body": b"two"})())
    with pytest.raises(RuntimeError, match="no retry"):
        guard.before_send(type("Request", (), {"body": b"three"})())
    guard.begin_invocation()
    guard.before_send(type("Request", (), {"body": b"next invocation"})())


import pytest
