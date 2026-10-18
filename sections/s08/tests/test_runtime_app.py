from __future__ import annotations

import json
import sys
import threading
from http.server import HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

S08_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(S08_DIR))

import runtime_app
import run_exercise
import aws_workflow


@pytest.fixture
def local_runtime(monkeypatch):
    runtime_app._requests = 0
    runtime_app._model_requests = 0
    runtime_app._agent = lambda prompt, limits: type(
        "Result", (), {"metrics": type("Metrics", (), {"accumulated_usage": {"inputTokens": 50, "outputTokens": 12, "totalTokens": 62}})(), "__str__": lambda self: "synthetic runbook checks"}
    )()
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
        runtime_app._model_requests = 0


def post(base_url: str, body: bytes) -> tuple[int, dict]:
    request = Request(base_url + "/invocations", data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=1) as response:
            return response.status, json.loads(response.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_accepts_request_and_returns_realistic_usage_fields(local_runtime):
    status, body = post(local_runtime, b'{"prompt":"catalog-api 5xx"}')
    assert status == 200
    assert body["response"] == "synthetic runbook checks"
    assert body["usage"]["total_tokens"] == 62
    assert body["elapsed_ms"] >= 0
    assert len(body["request_id"]) == 36


@pytest.mark.parametrize("body", [b'{"prompt":""}', b'{"prompt":"' + b'x' * (runtime_app.MAX_PROMPT + 1) + b'"}', b'x' * (runtime_app.MAX_BODY + 1)])
def test_rejects_invalid_or_oversized_request(local_runtime, body):
    status, payload = post(local_runtime, body)
    assert status == 400
    assert payload["error"] in {"ValueError", "JSONDecodeError"}


def test_limits_each_runtime_process_to_four_invocations(local_runtime):
    for _ in range(runtime_app.MAX_INVOCATIONS):
        assert post(local_runtime, b'{"prompt":"ok"}')[0] == 200
    status, body = post(local_runtime, b'{"prompt":"no fifth request"}')
    assert status == 429
    assert body["error"] == "S08 invocation limit reached"


def test_prompt_variants_are_fixed_and_unknown_values_rejected(monkeypatch):
    monkeypatch.setenv("S08_PROMPT_VARIANT", "candidate")
    assert "List each check" in runtime_app.PROMPTS["candidate"]
    monkeypatch.setenv("S08_PROMPT_VARIANT", "unbounded")
    with pytest.raises(ValueError, match="baseline or candidate"):
        runtime_app._make_agent()


def test_runtime_arn_must_be_exact_s08_account_and_region():
    arn = "arn:aws:bedrock-agentcore:ap-northeast-1:123456789012:runtime/udemy_agentcore_s08-AbCd123456"
    assert run_exercise.runtime_id_from_arn(arn, "123456789012") == "udemy_agentcore_s08-AbCd123456"
    with pytest.raises(ValueError, match="Runtime ARN"):
        run_exercise.runtime_id_from_arn(arn, "999999999999")
    with pytest.raises(ValueError, match="S08 Runtime"):
        run_exercise.runtime_id_from_arn(
            "arn:aws:bedrock-agentcore:ap-northeast-1:123456789012:runtime/udemy_agentcore_s07-AbCd123456",
            "123456789012",
        )


def test_offline_probe_reads_bundled_dataset_without_aws(monkeypatch, capsys):
    called = []
    holds = []
    countdowns = []

    class Stub:
        @staticmethod
        def find_runbook(service, topic):
            called.append((service, topic))
            return {"found": True, "service": service, "topic": topic}

    monkeypatch.setitem(sys.modules, "agent_app", Stub)
    monkeypatch.setattr(run_exercise, "verify_target", lambda *args: pytest.fail("AWS identity must not be read"))
    monkeypatch.setattr(run_exercise, "_hold", lambda message, seconds: holds.append((message, seconds)))
    monkeypatch.setattr(run_exercise, "_hold_with_countdown", lambda message, seconds: countdowns.append((message, seconds)))
    run_exercise.run_offline_probe(display_seconds=20, start_delay_seconds=8)
    out = capsys.readouterr().out
    assert called == [("catalog-api", "5xx")]
    assert "L02/L03の実行結果ではありません" in out
    assert "catalog-api" in out
    assert holds == [("質問を確認してください。", 8)]
    assert countdowns == [("結果を確認してください。", 20)]


def test_rollback_uses_explicit_account_and_restores_v2(monkeypatch):
    class Session:
        def client(self, *args, **kwargs):
            return object()

    account = "123456789012"
    calls = []
    monkeypatch.setattr(run_exercise, "_runtime_state", lambda control, runtime_id, runtime_arn, passed_account: calls.append(("state", passed_account)) or {"environmentVariables": {"S08_PROMPT_VARIANT": "candidate"}})
    monkeypatch.setattr(run_exercise, "_versions", lambda control, runtime_id: ["1", "2"])
    monkeypatch.setattr(run_exercise, "_endpoint", lambda control, runtime_id, endpoint, version: calls.append(("check", version)))
    monkeypatch.setattr(run_exercise, "_invoke", lambda session, arn, endpoint, step, variant: calls.append(("invoke", variant)))
    monkeypatch.setattr(run_exercise, "_route_endpoint", lambda control, runtime_id, version: calls.append(("route", version)) or {"liveVersion": version})
    monkeypatch.setattr(run_exercise, "_hold", lambda message, seconds: None)
    run_exercise.run_rollback(Session(), "runtime-id", "runtime-arn", account, 1, 1, "1")
    assert calls == [
        ("state", account), ("check", "2"), ("invoke", "candidate"),
        ("route", "1"), ("invoke", "baseline"), ("route", "1"),
    ]


def test_runtime_role_stays_scoped_without_resource_policy_admin():
    trust, policy = aws_workflow.runtime_role_documents("123456789012")
    assert trust["Statement"][0]["Condition"]["StringEquals"]["aws:SourceAccount"] == "123456789012"
    statements = policy["Statement"]
    actions = [action for statement in statements for action in (statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]])]
    assert "logs:PutResourcePolicy" not in actions
    assert "s3:GetObject" not in actions
    assert "bedrock:InvokeModel" in actions
    write_log = next(item for item in statements if item["Sid"] == "S08RuntimeLogStreams")
    assert write_log["Resource"].startswith("arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/bedrock-agentcore/runtimes/udemy_agentcore_s08-*")


def test_create_request_pins_v1_setup_and_s08_tags():
    request = aws_workflow.create_request("bucket", "s08/hash/zip", "role-arn", "client-token")
    assert request["platformVersion"] == "V2"
    assert request["environmentVariables"] == {"S08_PROMPT_VARIANT": "baseline"}
    assert request["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"] == {"bucket": "bucket", "prefix": "s08/hash/zip"}
    assert request["tags"] == {"Course": "amazon-bedrock-agentcore-operations", "Section": "s08"}
    assert request["clientToken"] == "client-token"


def test_caller_policy_includes_sdk_dependencies_only_for_s08_targets():
    policy = aws_workflow.caller_policy_document("123456789012")
    statements = {item["Sid"]: item for item in policy["Statement"]}
    artifact = statements["S08ArtifactReadUpload"]
    assert set(artifact["Action"]) == {"s3:GetObject", "s3:PutObject"}
    assert artifact["Resource"] == "arn:aws:s3:::udemy-agentcore-s08-apne1-123456789012-*/s08/*"

    tagging = statements["S08TagOnlyRuntime"]
    assert tagging["Action"] == "bedrock-agentcore:TagResource"
    assert tagging["Resource"] == "arn:aws:bedrock-agentcore:ap-northeast-1:123456789012:runtime/udemy_agentcore_s08-*"
    assert tagging["Condition"]["StringEquals"]["aws:RequestTag/Course"] == "amazon-bedrock-agentcore-operations"
    assert tagging["Condition"]["StringEquals"]["aws:RequestTag/Section"] == "s08"
    assert "bedrock-agentcore:PassCapacityProvider" not in {
        action for statement in policy["Statement"]
        for action in (statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]])
    }


@pytest.mark.parametrize("account", ["", "123", "12345678901x", "1234567890123"])
def test_rejects_invalid_expected_account_before_aws_session(account, monkeypatch):
    monkeypatch.setattr(aws_workflow.boto3, "Session", lambda **kwargs: pytest.fail("invalid account must fail before AWS"))
    with pytest.raises(RuntimeError, match="12-digit"):
        aws_workflow.setup(Path("unused.zip"), None, account)


def test_setup_stops_on_sts_account_mismatch_without_writes(tmp_path, monkeypatch):
    zip_path = tmp_path / "candidate.zip"
    zip_path.write_bytes(b"zip placeholder")
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(aws_workflow, "STATE_PATH", state_path)
    calls = []

    class Session:
        def __init__(self, **kwargs):
            pass

        def client(self, name, **kwargs):
            calls.append(("client", name))
            if name == "sts":
                return type("STS", (), {"get_caller_identity": lambda self: {"Account": "999999999999"}})()
            return object()

    monkeypatch.setattr(aws_workflow.boto3, "Session", Session)
    with pytest.raises(RuntimeError, match="STS account does not match"):
        aws_workflow.setup(zip_path, None, "123456789012")
    assert calls == [("client", "sts"), ("client", "s3"), ("client", "iam"), ("client", "bedrock-agentcore-control")]
    assert not state_path.exists()
