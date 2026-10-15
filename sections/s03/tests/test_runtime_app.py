from __future__ import annotations

import json
import http.client
import inspect
import sys
from pathlib import Path
from threading import Thread
from http.server import HTTPServer

import pytest

S03_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(S03_DIR))

import runtime_app
from runtime_app import RuntimeHandler, handle_invocation


def test_pinned_strands_agent_accepts_invocation_limits():
    parameters = inspect.signature(runtime_app.agent_app.Agent.__call__).parameters
    assert "limits" in parameters


def test_invocation_accepts_a_string_prompt_and_returns_agent_result():
    status, result = handle_invocation(
        '{"prompt":"catalog-api の5xxを調べて"}'.encode("utf-8"),
        lambda prompt: f"回答: {prompt}",
    )

    assert status == 200
    assert result == {"result": "回答: catalog-api の5xxを調べて"}


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b'{"prompt":"\xff"}',
        b"[]",
        b'{"prompt":[]}',
        b'{"prompt":"  "}',
        b'{"prompt":"' + (b"x" * 2_001) + b'"}',
    ],
)
def test_invocation_rejects_invalid_or_oversized_prompt(payload):
    status, result = handle_invocation(payload, lambda prompt: pytest.fail("must not invoke"))

    assert status == 400
    assert "error" in result


def test_invocation_rejects_oversized_body_before_json_parsing():
    payload = b" " * (16_385)

    status, result = handle_invocation(payload, lambda prompt: pytest.fail("must not invoke"))

    assert status == 413
    assert result["error"] == "request body is too large"


def test_provider_failure_does_not_leak_exception_text():
    def fail(prompt: str) -> str:
        raise RuntimeError("private provider response details")

    status, result = handle_invocation(b'{"prompt":"hello"}', fail)

    assert status == 500
    assert result["error"] == "agent invocation failed; inspect the Runtime log"
    assert result["error_type"] == "RuntimeError"
    assert "private" not in json.dumps(result)


def test_runtime_http_health_and_invocation_endpoints(monkeypatch, capsys):
    class FakeAgent:
        def __call__(self, prompt, *, limits):
            assert limits == {
                "turns": 4,
                "output_tokens": 2_048,
                "total_tokens": 6_000,
            }
            if prompt == "fail":
                raise AssertionError("private test detail")
            runtime_app.agent_app._tool_trace.append(
                {"arguments": {"service": prompt}, "result": "synthetic"}
            )
            return f"回答: {prompt}"

    monkeypatch.setattr(runtime_app, "_agent", FakeAgent())
    monkeypatch.setattr(runtime_app, "_ready", True)
    server = HTTPServer(("127.0.0.1", 0), RuntimeHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
    try:
        connection.request("GET", "/ping")
        health = connection.getresponse()
        assert health.status == 200
        assert json.loads(health.read()) == {"status": "Healthy"}

        connection.request(
            "POST",
            "/invocations",
            body='{"prompt":"catalog-api の5xxを調べて"}'.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        invocation = connection.getresponse()
        body = json.loads(invocation.read())
        assert invocation.status == 200
        assert body["result"] == "回答: catalog-api の5xxを調べて"
        assert body["invocation_id"]
        assert body["trace"] == [
            {"arguments": {"service": "catalog-api の5xxを調べて"}, "result": "synthetic"}
        ]
        lines = capsys.readouterr().out.splitlines()
        log_event = json.loads(next(line for line in lines if line.startswith("{")))
        assert log_event["invocation_id"] == body["invocation_id"]
        assert "catalog-api の5xxを調べて" not in json.dumps(log_event)

        connection.request(
            "POST", "/invocations", body=b'{"prompt":"fail"}',
            headers={"Content-Type": "application/json"},
        )
        failure = connection.getresponse()
        failure_body = json.loads(failure.read())
        assert failure.status == 500
        assert failure_body["invocation_id"]
        assert failure_body["error_type"] == "AssertionError"
        lines = capsys.readouterr().out.splitlines()
        error_event = json.loads(next(
            line for line in lines if '"status": 500' in line
        ))
        assert error_event["invocation_id"] == failure_body["invocation_id"]
        assert error_event["error_type"] == failure_body["error_type"]

        connection.request(
            "POST", "/invocations", body=b'{"prompt":"fresh"}',
            headers={"Content-Type": "application/json"},
        )
        fresh = connection.getresponse()
        fresh_body = json.loads(fresh.read())
        assert fresh.status == 200
        assert fresh_body["trace"] == [
            {"arguments": {"service": "fresh"}, "result": "synthetic"}
        ]

        connection.request(
            "POST", "/invocations", body=b"{}", headers={"Content-Length": "-1"}
        )
        malformed = connection.getresponse()
        assert malformed.status == 400
        malformed.read()

        connection.request(
            "POST", "/invocations", body=b"", headers={"Content-Length": "bad"}
        )
        malformed = connection.getresponse()
        assert malformed.status == 400
        malformed.read()
    finally:
        connection.close()
        server.shutdown()
        server.server_close()


def test_runtime_is_not_healthy_before_static_application_is_ready(monkeypatch):
    monkeypatch.setattr(runtime_app, "_ready", False)
    server = HTTPServer(("127.0.0.1", 0), RuntimeHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
    try:
        connection.request("GET", "/ping")
        response = connection.getresponse()
        assert response.status == 503
        assert json.loads(response.read()) == {"status": "Initializing"}
    finally:
        connection.close()
        server.shutdown()
        server.server_close()


def test_main_marks_static_app_ready_without_resolving_bedrock_credentials(monkeypatch):
    class FakeServer:
        def __init__(self, address, handler):
            assert address == ("0.0.0.0", runtime_app.PORT)
            assert handler is RuntimeHandler

        def serve_forever(self):
            assert runtime_app._ready is True
            assert runtime_app._agent is None

    monkeypatch.setattr(runtime_app, "_ready", False)
    monkeypatch.setattr(runtime_app, "_agent", None)
    monkeypatch.setattr(runtime_app.agent_app, "_make_agent", lambda *_: pytest.fail("must remain lazy"))
    monkeypatch.setattr(runtime_app, "HTTPServer", FakeServer)

    runtime_app.main()


def test_agent_client_is_created_lazily_once_per_process(monkeypatch):
    created = []
    agent = object()
    monkeypatch.setattr(runtime_app, "_agent", None)
    monkeypatch.setattr(runtime_app.agent_app, "_make_agent", lambda region: created.append(region) or agent)

    assert created == []
    assert runtime_app._get_agent() is agent
    assert runtime_app._get_agent() is agent
    assert created == [runtime_app.agent_app.DEFAULT_REGION]
