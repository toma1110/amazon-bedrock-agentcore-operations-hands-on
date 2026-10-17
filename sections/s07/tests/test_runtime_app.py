from __future__ import annotations

import json
import sys
import threading
from http.server import HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

S07_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(S07_DIR))

import runtime_app


@pytest.fixture
def local_runtime(monkeypatch):
    runtime_app._requests = 0
    runtime_app._agent = object()
    monkeypatch.setattr(runtime_app, "_get_agent", lambda: lambda prompt: f"ok: {prompt}")
    server = HTTPServer(("127.0.0.1", 0), runtime_app.Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=1)
        runtime_app._agent = None
        runtime_app._requests = 0


def post(base_url: str, body: bytes) -> tuple[int, dict]:
    request = Request(base_url + "/invocations", data=body, method="POST",
                      headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=1) as response:
            return response.status, json.loads(response.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_runtime_accepts_one_bounded_prompt_and_returns_request_id(local_runtime):
    status, body = post(local_runtime, b'{"prompt":"diagnose synthetic 429"}')

    assert status == 200
    assert body["response"] == "ok: diagnose synthetic 429"
    assert len(body["request_id"]) == 36


def test_runtime_resets_model_request_guard_for_each_invocation(local_runtime, monkeypatch):
    class Guard:
        def __init__(self):
            self.begins = 0

        def begin_invocation(self):
            self.begins += 1

        def snapshot(self):
            return {"model_request_attempts": 0}

    guard = Guard()
    monkeypatch.setattr(runtime_app, "_request_guard", guard)
    assert post(local_runtime, b'{"prompt":"one"}')[0] == 200
    assert post(local_runtime, b'{"prompt":"two"}')[0] == 200
    assert guard.begins == 2


@pytest.mark.parametrize("body", [
    b'{"prompt":""}',
    b'{"prompt":"' + b'x' * (runtime_app.MAX_PROMPT + 1) + b'"}',
    b'x' * (runtime_app.MAX_BODY + 1),
])
def test_runtime_rejects_empty_long_or_oversized_requests(local_runtime, body):
    status, body = post(local_runtime, body)
    assert status == 400
    assert body["error"] == "ValueError"


@pytest.mark.parametrize("gateway_url", [
    "https://attacker.example/mcp",
    "https://seilgfwckz.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com/mcp",
])
def test_gateway_configuration_rejects_any_nonapproved_host_before_network(monkeypatch, gateway_url):
    called = []
    monkeypatch.setenv("S04_GATEWAY_URL", gateway_url)
    monkeypatch.setenv("S04_GATEWAY_TOOL_NAME", "udemy-agentcore-s04-cw-target___read_synthetic_cloudwatch")
    monkeypatch.setattr(runtime_app.gateway_tools, "create_gateway_client",
                        lambda *args: called.append(args))

    with pytest.raises(ValueError, match="fixed Tokyo gateway endpoint"):
        runtime_app._gateway_reader()

    assert called == []


def test_gateway_configuration_accepts_only_console_verified_s04_endpoint(monkeypatch):
    expected_url = "https://udemy-agentcore-s04-gateway-seilgfwckz.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com/mcp"
    tool_name = "udemy-agentcore-s04-cw-target___read_synthetic_cloudwatch"
    monkeypatch.setenv("S04_GATEWAY_URL", expected_url)
    monkeypatch.setenv("S04_GATEWAY_TOOL_NAME", tool_name)
    monkeypatch.setattr(runtime_app, "_gateway_client", None)
    calls = []

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def list_tools_sync(self):
            return [type("Tool", (), {"tool_name": tool_name})()]

    client = Client()
    monkeypatch.setattr(runtime_app.gateway_tools, "create_gateway_client",
                        lambda url, region: calls.append((url, region)) or client)
    monkeypatch.setattr(runtime_app.gateway_tools, "create_reader_tool",
                        lambda received_client, received_name: (received_client, received_name))

    assert runtime_app._gateway_reader() == (client, tool_name)
    assert calls == [(expected_url, "ap-northeast-1")]


@pytest.mark.parametrize("name,value", [
    ("S07_MODEL_ID", "not-a-bedrock-model"),
    ("S07_BEDROCK_REGION", "us-east-1"),
])
def test_model_settings_reject_arbitrary_values(name, value, monkeypatch):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match="fixed"):
        runtime_app._make_agent()


def test_invalid_region_is_blocked_before_bedrock_client_creation(monkeypatch):
    called = []
    monkeypatch.setenv("S07_BEDROCK_REGION", "ap-northeast-99")
    monkeypatch.setattr(runtime_app, "BedrockModel", lambda **kwargs: called.append(kwargs))

    with pytest.raises(ValueError, match="blocked before creating an AWS client"):
        runtime_app._make_agent()

    assert called == []


def test_runtime_enforces_eight_invocations_per_process(local_runtime):
    for _ in range(runtime_app.MAX_INVOCATIONS):
        status, _ = post(local_runtime, b'{"prompt":"ok"}')
        assert status == 200

    status, body = post(local_runtime, b'{"prompt":"same input"}')
    assert status == 429
    assert body == {"error": "session invocation limit reached"}


def test_agent_configuration_bounds_model_and_parallel_tool_requests(monkeypatch):
    captured = {}

    class Events:
        def register(self, event_name, handler):
            captured["event_name"] = event_name
            captured["guard_handler"] = handler

    class ModelStub:
        def __init__(self, **kwargs):
            captured["model_options"] = kwargs
            self.client = type("Client", (), {"meta": type("Meta", (), {"events": Events()})()})()

    class AgentStub:
        def __init__(self, **kwargs):
            captured["agent_options"] = kwargs

    monkeypatch.setattr(runtime_app, "BedrockModel", ModelStub)
    monkeypatch.setattr(runtime_app, "Agent", AgentStub)
    monkeypatch.setattr(runtime_app, "_gateway_reader", lambda: "fixed-gateway-tool")

    result = runtime_app._make_agent()

    assert isinstance(result, AgentStub)
    assert captured["model_options"]["max_tokens"] == 256
    assert captured["model_options"]["streaming"] is False
    assert captured["event_name"] == "before-send.bedrock-runtime.*"
    assert captured["agent_options"]["retry_strategy"] is None
    assert captured["agent_options"]["max_parallel_tools"] == 1
    assert captured["agent_options"]["tools"] == [
        runtime_app.inspect_synthetic_runbook,
        "fixed-gateway-tool",
        runtime_app.call_local_status_api_stub,
    ]
    assert captured["guard_handler"].__self__.max_attempts == runtime_app.MAX_TURNS
