from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

S03_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(S03_DIR))

import aws_workflow


def test_invoke_cli_uses_utf8_when_windows_stdout_is_a_pipe():
    child_code = r'''
import sys
import aws_workflow as workflow

class FakeSts:
    def get_caller_identity(self):
        return {"Account": "111122223333"}

class FakeRuntime:
    pass

state = {"account_id": "111122223333", "runtime_arn": "arn:example", "sessions": []}
workflow._clients = lambda _profile: (FakeSts(), object(), object(), object(), FakeRuntime())
workflow._read_state = lambda _sts: state
workflow._write_state = lambda _state: None
workflow._invoke = lambda _runtime, _state, session_id, _prompt: {
    "invocation_id": "event-1", "result": "\u5bfe\u5fdc\u5b8c\u4e86\U0001f642", "trace": []
}
sys.argv = ["aws_workflow.py", "--profile", "test-profile", "invoke"]
code = workflow.main()
print("stderr-\u65e5\u672c\u8a9e\U0001f642", file=sys.stderr, flush=True)
raise SystemExit(code)
'''
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "cp932"
    env["PYTHONPATH"] = str(S03_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-c", child_code],
        cwd=S03_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    stdout = result.stdout.decode("utf-8")
    records = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
    assert records[0]["prompt"] == "catalog-api の5xxを調べる順序を教えてください。"
    assert records[1]["result"]["result"] == "対応完了🙂"
    assert result.stderr.decode("utf-8") == "stderr-日本語🙂\r\n"


def test_cli_fails_before_aws_when_utf8_stream_configuration_is_unavailable(monkeypatch):
    class UnconfigurableStream:
        pass

    monkeypatch.setattr(aws_workflow.sys, "stdout", UnconfigurableStream())
    monkeypatch.setattr(
        aws_workflow,
        "_clients",
        lambda _profile: pytest.fail("AWS clients must not be created"),
    )
    with pytest.raises(RuntimeError, match="Cannot configure stdout"):
        aws_workflow.main()


def test_account_guard_stops_before_wrong_account_write():
    class FakeSts:
        def get_caller_identity(self):
            return {"Account": "111122223333"}

    with pytest.raises(RuntimeError, match="account guard"):
        aws_workflow._guard(FakeSts(), "444455556666")


def test_execution_role_policy_is_scoped_to_expected_runtime_and_model():
    trust, policy = aws_workflow._runtime_role_documents("111122223333")

    serialized = json.dumps({"trust": trust, "policy": policy})
    assert "arn:aws:bedrock-agentcore:ap-northeast-1:111122223333:runtime/udemy_agentcore_s03-*" in serialized
    assert "amazon.nova-lite-v1:0" in serialized
    assert "bedrock:InvokeModel" in serialized
    assert "AdministratorAccess" not in serialized


def test_invoke_uses_sigv4_runtime_parameters_and_json_payload():
    class FakeRuntime:
        def invoke_agent_runtime(self, **kwargs):
            assert kwargs["qualifier"] == "DEFAULT"
            assert kwargs["runtimeSessionId"] == "session-example"
            assert kwargs["contentType"] == "application/json"
            assert json.loads(kwargs["payload"].decode("utf-8")) == {"prompt": "hello"}
            return {"response": io.BytesIO(b'{"invocation_id":"event-1","result":"ok"}')}

    response = aws_workflow._invoke(
        FakeRuntime(),
        {"runtime_arn": "arn:example"},
        "session-example",
        "hello",
    )

    assert response == {"invocation_id": "event-1", "result": "ok"}


def test_invoke_prints_paired_request_and_response_records_with_session_ids(
    monkeypatch, capsys
):
    state = {"account_id": "111122223333", "runtime_arn": "arn:example", "sessions": []}

    class FakeSts:
        def get_caller_identity(self):
            return {"Account": "111122223333"}

    monkeypatch.setattr(
        aws_workflow,
        "_clients",
        lambda _profile: (FakeSts(), object(), object(), object(), object()),
    )
    monkeypatch.setattr(aws_workflow, "_read_state", lambda _sts: state)
    monkeypatch.setattr(aws_workflow, "_write_state", lambda _state: None)
    calls = []

    def fake_invoke(_runtime, _state, session_id, prompt):
        calls.append((session_id, prompt))
        return {"invocation_id": f"event-{len(calls)}", "result": "ok", "trace": []}

    monkeypatch.setattr(aws_workflow, "_invoke", fake_invoke)
    aws_workflow.invoke(None)

    records = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    assert [record["event"] for record in records] == [
        "request", "response", "request", "response", "request", "response"
    ]
    requests = records[::2]
    responses = records[1::2]
    assert [record["step"] for record in requests] == [
        "same-session-1", "same-session-2", "new-session"
    ]
    assert [record["session_id"] for record in requests[:2]] == [
        requests[0]["session_id"], requests[0]["session_id"]
    ]
    assert requests[2]["session_id"] != requests[0]["session_id"]
    for request, response in zip(requests, responses, strict=True):
        assert response["step"] == request["step"]
        assert response["session_id"] == request["session_id"]
        assert response["result"]["invocation_id"]
        assert "trace" in response["result"]
    assert len(calls) == 3
    assert state["invocations"] == ["event-1", "event-2", "event-3"]


def test_state_reader_refuses_a_different_current_account(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps({"schema_version": 1, "account_id": "111122223333"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(aws_workflow, "STATE_PATH", state_path)

    class FakeSts:
        def get_caller_identity(self):
            return {"Account": "999900001111"}

    with pytest.raises(RuntimeError, match="account guard"):
        aws_workflow._read_state(FakeSts())


def test_all_resource_intents_and_runtime_idempotency_token_are_saved_before_first_write(
    monkeypatch, tmp_path
):
    state_path = tmp_path / "state.json"
    zip_path = tmp_path / "runtime.zip"
    zip_path.write_bytes(b"zip test content")
    monkeypatch.setattr(aws_workflow, "STATE_PATH", state_path)
    monkeypatch.setattr(aws_workflow.getpass, "getpass", lambda _prompt: "111122223333")

    class FakeSts:
        def get_caller_identity(self):
            return {"Account": "111122223333"}

    class FakeS3:
        def create_bucket(self, **_kwargs):
            state = json.loads(state_path.read_text(encoding="utf-8"))
            assert state["bucket_status"] == "create_requested"
            assert state["bucket"].startswith(aws_workflow.BUCKET_PREFIX)
            assert state["role_trust"]["Statement"]
            assert state["role_policy"]["Statement"]
            assert state["role_name"] == aws_workflow.ROLE_NAME
            assert state["runtime_name"] == aws_workflow.RUNTIME_NAME
            assert state["runtime_status"] == "planned"
            assert state["runtime_create_request"]["clientToken"] == state["runtime_client_token"]
            raise RuntimeError("simulated interruption after intent was persisted")

    monkeypatch.setattr(
        aws_workflow,
        "_clients",
        lambda _profile: (FakeSts(), FakeS3(), object(), object(), object()),
    )

    with pytest.raises(RuntimeError, match="simulated interruption"):
        aws_workflow.deploy(zip_path, None)

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["bucket_status"] == "create_requested"
    assert saved["zip_sha256"] == aws_workflow._zip_sha256(zip_path)


def test_runtime_create_retries_with_the_same_persisted_idempotency_request(monkeypatch, tmp_path):
    monkeypatch.setattr(aws_workflow, "STATE_PATH", tmp_path / "state.json")
    state = {
        "account_id": "111122223333",
        "runtime_status": "create_requested",
        "role_arn": "arn:aws:iam::111122223333:role/example",
        "runtime_create_request": {
            "agentRuntimeName": aws_workflow.RUNTIME_NAME,
            "roleArn": "arn:aws:iam::111122223333:role/example",
            "agentRuntimeArtifact": {"codeConfiguration": {"entryPoint": ["runtime_app.py"]}},
            "clientToken": "s03-a-stable-token-for-create",
        },
    }

    class FakeSts:
        def get_caller_identity(self):
            return {"Account": "111122223333"}

    class FakeControl:
        def __init__(self):
            self.requests = []
            self.fail_once = True

        def create_agent_runtime(self, **kwargs):
            self.requests.append(kwargs.copy())
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("simulated lost response")
            return {
                "agentRuntimeId": "runtime-id",
                "agentRuntimeArn": "runtime-arn",
                "agentRuntimeVersion": "1",
            }

        def get_agent_runtime(self, **kwargs):
            return {
                "status": "READY",
                "metadataConfiguration": {"requireMMDSV2": True},
            }

        def update_agent_runtime(self, **kwargs):
            return {"agentRuntimeVersion": "2"}

    control = FakeControl()
    with pytest.raises(RuntimeError, match="lost response"):
        aws_workflow._apply_runtime_plan(control, FakeSts(), state)
    aws_workflow._apply_runtime_plan(control, FakeSts(), state)

    assert len(control.requests) == 2
    assert control.requests[0] == control.requests[1]
    assert control.requests[0]["clientToken"] == "s03-a-stable-token-for-create"
    assert state["runtime_status"] == "ready"


def test_runtime_update_retries_with_saved_idempotency_token(monkeypatch):
    state = {
        "account_id": "111122223333",
        "runtime_status": "created",
        "runtime_id": "runtime-id",
        "runtime_arn": "runtime-arn",
        "runtime_version": "1",
        "role_arn": "role-arn",
        "runtime_create_request": {
            "agentRuntimeArtifact": {"codeConfiguration": {"entryPoint": ["runtime_app.py"]}}
        },
    }

    class FakeSts:
        def get_caller_identity(self):
            return {"Account": "111122223333"}

    class FakeControl:
        def __init__(self):
            self.requests = []

        def get_agent_runtime(self, **_kwargs):
            return {
                "status": "READY",
                "metadataConfiguration": {"requireMMDSV2": True},
            }

        def update_agent_runtime(self, **kwargs):
            self.requests.append(kwargs.copy())
            if len(self.requests) == 1:
                raise RuntimeError("simulated lost update response")
            return {"agentRuntimeVersion": "2"}

    monkeypatch.setattr(aws_workflow, "_write_state", lambda _state: None)
    control = FakeControl()
    with pytest.raises(RuntimeError, match="lost update response"):
        aws_workflow._apply_runtime_plan(control, FakeSts(), state)
    aws_workflow._apply_runtime_plan(control, FakeSts(), state)

    assert len(control.requests) == 2
    assert control.requests[0] == control.requests[1]
    assert control.requests[0]["clientToken"] == state["runtime_update_request"]["clientToken"]
    assert state["runtime_status"] == "ready"


def test_status_reads_only_saved_runtime_and_reports_observed_fields(monkeypatch, capsys):
    state = {"account_id": "111122223333", "runtime_id": "runtime-exact"}

    class FakeSts:
        def get_caller_identity(self):
            return {"Account": "111122223333"}

    class FakeControl:
        def get_agent_runtime(self, **kwargs):
            assert kwargs == {"agentRuntimeId": "runtime-exact"}
            return {
                "agentRuntimeId": "runtime-exact",
                "agentRuntimeVersion": "2",
                "status": "READY",
                "platformVersion": "V2",
                "metadataConfiguration": {"requireMMDSV2": True},
            }

    monkeypatch.setattr(aws_workflow, "_clients", lambda _profile: (FakeSts(), None, None, FakeControl(), None))
    monkeypatch.setattr(aws_workflow, "_read_state", lambda _sts: state)
    aws_workflow.status(None)
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "runtime_id": "runtime-exact",
        "version": "2",
        "status": "READY",
        "platform_version": "V2",
        "require_mmds_v2": True,
        "read_only_confirmation": True,
    }


def test_logs_filters_saved_invocation_in_derived_exact_group_only(monkeypatch, capsys):
    state = {
        "account_id": "111122223333",
        "runtime_id": "runtime-exact",
        "invocations": ["invoke-exact"],
    }

    class FakeSts:
        def get_caller_identity(self):
            return {"Account": "111122223333"}

    class FakeSession:
        def __init__(self, **kwargs):
            assert kwargs == {"profile_name": None, "region_name": aws_workflow.REGION}

        def client(self, name):
            assert name == "logs"
            return fake_logs

    class FakeLogs:
        def filter_log_events(self, **kwargs):
            assert kwargs == {
                "logGroupName": "/aws/bedrock-agentcore/runtimes/runtime-exact-DEFAULT",
                "filterPattern": '"invoke-exact"',
                "limit": 100,
            }
            return {"events": [{
                "timestamp": 123,
                "message": json.dumps({
                    "event": "agent_invocation",
                    "invocation_id": "invoke-exact",
                    "status": 200,
                    "tool_call_count": 1,
                    "tool_names": ["lookup_runbook"],
                    "prompt": "must not be printed",
                }),
            }]}

    fake_logs = FakeLogs()
    monkeypatch.setattr(aws_workflow, "_clients", lambda _profile: (FakeSts(), None, None, None, None))
    monkeypatch.setattr(aws_workflow, "_read_state", lambda _sts: state)
    monkeypatch.setattr(aws_workflow.boto3, "Session", FakeSession)
    aws_workflow.logs(None, wait_seconds=0)
    output = capsys.readouterr().out
    assert "invoke-exact" in output
    assert "lookup_runbook" in output
    assert '"tool_call_count": 1' in output
    assert "must not be printed" not in output
    assert "list_log_groups" not in output


def test_logs_rejects_missing_invocation_state(monkeypatch):
    class FakeSts:
        def get_caller_identity(self):
            return {"Account": "111122223333"}

    monkeypatch.setattr(aws_workflow, "_clients", lambda _profile: (FakeSts(), None, None, None, None))
    monkeypatch.setattr(aws_workflow, "_read_state", lambda _sts: {"invocations": []})
    with pytest.raises(RuntimeError, match="No saved invocation IDs"):
        aws_workflow.logs(None, wait_seconds=0)


def test_logs_follows_empty_pages_until_exact_event_on_sixth_page():
    event = {
        "timestamp": 123,
        "message": json.dumps({
            "event": "agent_invocation",
            "invocation_id": "invoke-exact",
            "status": 200,
        }),
    }

    class FakeLogs:
        def __init__(self):
            self.requests = []

        def filter_log_events(self, **kwargs):
            self.requests.append(kwargs)
            page = len(self.requests)
            if page < 6:
                return {"events": [], "nextToken": f"page-{page}"}
            return {"events": [event]}

    fake = FakeLogs()
    found = aws_workflow._find_invocation_events(
        fake, "/aws/bedrock-agentcore/runtimes/runtime-exact-DEFAULT", "invoke-exact"
    )

    assert found == [event]
    assert len(fake.requests) == 6
    assert "nextToken" not in fake.requests[0]
    assert fake.requests[1]["nextToken"] == "page-1"
    assert fake.requests[-1]["nextToken"] == "page-5"
    assert all(request["logGroupName"] == "/aws/bedrock-agentcore/runtimes/runtime-exact-DEFAULT" for request in fake.requests)
    assert all(request["filterPattern"] == '"invoke-exact"' for request in fake.requests)


def test_logs_pagination_returns_missing_after_all_pages_are_exhausted():
    class FakeLogs:
        def __init__(self):
            self.requests = []

        def filter_log_events(self, **kwargs):
            self.requests.append(kwargs)
            if len(self.requests) == 1:
                return {"events": [], "nextToken": "page-1"}
            return {"events": []}

    fake = FakeLogs()
    assert aws_workflow._find_invocation_events(fake, "exact-group", "invoke-exact") == []
    assert len(fake.requests) == 2
    assert fake.requests[1]["nextToken"] == "page-1"


def test_logs_pagination_rejects_repeated_token():
    class FakeLogs:
        def filter_log_events(self, **_kwargs):
            return {"events": [], "nextToken": "stuck-token"}

    with pytest.raises(RuntimeError, match="repeated a token"):
        aws_workflow._find_invocation_events(FakeLogs(), "exact-group", "invoke-exact")


def test_logs_pagination_stops_at_page_safety_limit(monkeypatch):
    class FakeLogs:
        def __init__(self):
            self.calls = 0

        def filter_log_events(self, **_kwargs):
            self.calls += 1
            return {"events": [], "nextToken": f"page-{self.calls}"}

    monkeypatch.setattr(aws_workflow, "LOG_MAX_PAGES_PER_SEARCH", 2)
    fake = FakeLogs()
    with pytest.raises(RuntimeError, match="safety limit"):
        aws_workflow._find_invocation_events(fake, "exact-group", "invoke-exact")
    assert fake.calls == 2


def test_logs_pagination_reports_rejected_or_expired_token():
    class FakeLogs:
        def __init__(self):
            self.calls = 0

        def filter_log_events(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return {"events": [], "nextToken": "temporary-token"}
            raise aws_workflow.ClientError(
                {"Error": {"Code": "InvalidParameterException", "Message": "invalid token"}},
                "FilterLogEvents",
            )

    fake = FakeLogs()
    with pytest.raises(RuntimeError, match="rejected or expired"):
        aws_workflow._find_invocation_events(fake, "exact-group", "invoke-exact")
    assert fake.calls == 2


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (json.dumps({"event": "agent_invocation", "request_id": "invoke-exact"}), False),
        ('{"event":"agent_invocation", "invocation_id":', False),
        (json.dumps([{"event": "agent_invocation", "invocation_id": "invoke-exact"}]), False),
        (json.dumps({"event": "agent_invocation", "invocation_id": "invoke-other"}), False),
        (json.dumps({"event": "agent_invocation", "invocation_id": "invoke-exact"}), True),
    ],
)
def test_agent_invocation_log_match_requires_exact_json_schema_and_id(message, expected):
    assert aws_workflow._matching_agent_invocation({"message": message}, "invoke-exact") is expected
