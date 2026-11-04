from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

S04_DIR = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("s03_reader_workflow", S04_DIR / "s03_reader_workflow.py")
assert SPEC is not None and SPEC.loader is not None
workflow = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = workflow
SPEC.loader.exec_module(workflow)


def test_names_are_per_run_and_runtime_uses_agentcore_name_characters():
    first = workflow.names_for_run("course-run-01")
    second = workflow.names_for_run("course-run-02")
    assert first != second
    assert first["runtime"].startswith("udemy_agentcore_s04r")
    assert first["runtime"].replace("_", "").isalnum()
    assert first["lambda_role"] != second["lambda_role"]


@pytest.mark.parametrize("run_id", ["", "UPPER", "bad/name", "x" * 21, "with_underscore"])
def test_names_reject_unbounded_or_nonportable_run_id(run_id):
    with pytest.raises(ValueError):
        workflow.names_for_run(run_id)


def test_s03_log_group_must_name_exact_default_runtime():
    assert workflow.log_group_runtime_id("/aws/bedrock-agentcore/runtimes/s03-runtime-123-DEFAULT") == "s03-runtime-123"
    for value in ("", "/other/group", "/aws/bedrock-agentcore/runtimes/-DEFAULT",
                  "/aws/bedrock-agentcore/runtimes/a-DEFAULT/extra"):
        with pytest.raises(ValueError):
            workflow.log_group_runtime_id(value)


def test_gateway_endpoint_is_normalized_to_mcp_path():
    assert workflow.mcp_endpoint_url("https://gateway.example") == "https://gateway.example/mcp"
    assert workflow.mcp_endpoint_url("https://gateway.example/") == "https://gateway.example/mcp"
    assert workflow.mcp_endpoint_url("https://gateway.example/mcp") == "https://gateway.example/mcp"
    with pytest.raises(ValueError):
        workflow.mcp_endpoint_url("http://gateway.example")


def test_uncertain_write_is_saved_before_send_and_never_replayed(tmp_path):
    state = {"operations": {}}
    state_path = tmp_path / "state.json"
    calls = []

    def uncertain():
        calls.append("sent")
        raise TimeoutError("response lost")

    with pytest.raises(RuntimeError, match="Do not replay"):
        workflow._write_once(state_path, state, "create", "example.create", {"Name": "exact"}, uncertain)
    assert calls == ["sent"]
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["operations"]["create"]["status"] == "outcome_unknown"
    assert saved["operations"]["create"]["request"] == {"Name": "exact"}
    with pytest.raises(RuntimeError, match="outcome_unknown"):
        workflow._write_once(state_path, state, "create", "example.create", {"Name": "exact"}, uncertain)
    assert calls == ["sent"]


def test_successful_write_response_is_checkpointed_and_reused(tmp_path):
    state = {"operations": {}}
    state_path = tmp_path / "state.json"
    calls = []
    result = workflow._write_once(state_path, state, "create", "example.create", {"Name": "exact"},
                                  lambda: calls.append("sent") or {"id": "resource-1"})
    assert result == {"id": "resource-1"}
    assert json.loads(state_path.read_text(encoding="utf-8"))["operations"]["create"]["status"] == "accepted"
    assert workflow._write_once(state_path, state, "create", "example.create", {"Name": "exact"},
                                 lambda: calls.append("replayed") or {}) == {"id": "resource-1"}
    assert calls == ["sent"]


def test_runtime_and_lambda_policy_do_not_include_s03_or_bucket_mutations():
    statement = workflow._policy(["lambda:InvokeFunction"], ["arn:aws:lambda:ap-northeast-1:123456789012:function:only-this"])
    serialized = json.dumps(statement)
    assert "lambda:InvokeFunction" in serialized
    assert "s3:DeleteObject" not in serialized
    assert "bedrock-agentcore:UpdateAgentRuntime" not in serialized
    assert "AdministratorAccess" not in serialized


def test_deploy_bootstraps_from_borrowed_s03_artifact_then_updates_only_new_runtime(tmp_path, monkeypatch):
    import base64
    import hashlib

    monkeypatch.setattr(workflow, "STATE_DIR", tmp_path / ".local" / "s04-reader")
    run_id = "two-stage-01"
    names = workflow.names_for_run(run_id)
    s03_artifact = {"codeConfiguration": {"code": {"s3": {"bucket": "shared-course-bucket", "prefix": "s03/zip"}},
        "runtime": "PYTHON_3_13", "entryPoint": ["runtime_app.py"]}}
    snapshot = {"agentRuntimeVersion": "1", "roleArn": "arn:aws:iam::123456789012:role/s03",
        "platformVersion": "V2", "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"}, "environmentVariables": {},
        "metadataConfiguration": {"requireMMDSV2": True},
        "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
        "agentRuntimeArtifact": s03_artifact}
    state = {"schema_version": 1, "run_id": run_id, "phase": "preflight_complete", "account_id": "123456789012",
        "region": "ap-northeast-1", "s03_runtime_id": "s03-runtime", "s03_log_group": "/aws/bedrock-agentcore/runtimes/s03-runtime-DEFAULT",
        "bucket": "shared-course-bucket", "names": names, "s03_snapshot": snapshot,
        "borrowed_artifact": {"bucket": "shared-course-bucket", "key": "s03/zip", "sha256": "source-hash", "owned": False},
        "ownership_tag": {"S04ReaderRun": run_id}, "operations": {}, "owned": {}, "sessions": [], "invocations": []}
    state_path = workflow._state_path(run_id)
    workflow._save(state_path, state)
    lambda_zip = tmp_path / "reader.zip"; lambda_zip.write_bytes(b"lambda package")
    runtime_zip = tmp_path / "runtime.zip"; runtime_zip.write_bytes(b"runtime package")
    calls = []
    runtime_envs = []

    class Sts:
        def get_caller_identity(self): return {"Account": "123456789012"}

    class S3:
        def put_object(self, **kwargs): calls.append(("s3.put_object", kwargs["Key"])); return {"ETag": "e"}
        def head_object(self, **kwargs): return {"Metadata": {"sha256": "source-hash"}}

    class Iam:
        policies = {}
        trusts = {}
        def create_role(self, **kwargs):
            calls.append(("iam.create_role", kwargs["RoleName"]))
            self.trusts[kwargs["RoleName"]] = json.loads(kwargs["AssumeRolePolicyDocument"])
            return {"Role": {"Arn": f"arn:aws:iam::123456789012:role/{kwargs['RoleName']}"}}
        def put_role_policy(self, **kwargs):
            calls.append(("iam.put_role_policy", kwargs["RoleName"]))
            self.policies[(kwargs["RoleName"], kwargs["PolicyName"])] = json.loads(kwargs["PolicyDocument"])
            return {}
        def update_assume_role_policy(self, **kwargs):
            calls.append(("iam.update_assume_role_policy", kwargs["RoleName"]))
            self.trusts[kwargs["RoleName"]] = json.loads(kwargs["PolicyDocument"])
            return {}
        def get_role(self, **kwargs):
            return {"Role": {"AssumeRolePolicyDocument": self.trusts[kwargs["RoleName"]]}}
        def get_role_policy(self, **kwargs):
            return {"PolicyDocument": self.policies[(kwargs["RoleName"], kwargs["PolicyName"])]}

    class Lambda:
        def create_function(self, **kwargs):
            calls.append(("lambda.create_function", kwargs["FunctionName"]))
            return {"FunctionArn": f"arn:aws:lambda:ap-northeast-1:123456789012:function:{kwargs['FunctionName']}"}
        def get_function(self, **kwargs):
            digest = hashlib.sha256(lambda_zip.read_bytes()).digest()
            return {"Configuration": {"State": "Active", "LastUpdateStatus": "Successful",
                "Role": f"arn:aws:iam::123456789012:role/{names['lambda_role']}",
                "CodeSha256": base64.b64encode(digest).decode("ascii"),
                "Description": f"S04 S03 read-only invocation summary run {run_id}",
                "FunctionArn": f"arn:aws:lambda:ap-northeast-1:123456789012:function:{names['lambda']}"}}
        def list_tags(self, **kwargs): return {"Tags": {"S04ReaderRun": run_id}}

    class Logs:
        def create_log_group(self, **kwargs): calls.append(("logs.create", kwargs["logGroupName"])); return {}
        def tag_log_group(self, **kwargs): calls.append(("logs.tag", kwargs["logGroupName"])); return {}
        def put_retention_policy(self, **kwargs): calls.append(("logs.retention", kwargs["logGroupName"])); return {}

    class Control:
        runtime = None
        def get_agent_runtime(self, **kwargs):
            if kwargs["agentRuntimeId"] == "s03-runtime": return {**snapshot, "status": "READY"}
            return {**self.runtime, "status": "READY"}
        def create_gateway(self, **kwargs):
            calls.append(("gateway.create", kwargs["name"]))
            return {"gatewayId": "gateway-1234567890", "gatewayArn": "arn:aws:bedrock-agentcore:ap-northeast-1:123456789012:gateway/gateway-1234567890", "gatewayUrl": "https://gateway.example/mcp"}
        def get_gateway(self, **kwargs): return {"status": "READY"}
        def create_gateway_target(self, **kwargs): calls.append(("target.create", kwargs["name"])); return {"targetId": "target12345"}
        def get_gateway_target(self, **kwargs): return {"status": "READY"}
        def create_agent_runtime(self, **kwargs):
            calls.append(("runtime.create", kwargs["agentRuntimeArtifact"]))
            self.runtime = {"agentRuntimeId": "s04-reader-id", "agentRuntimeArn": "arn:aws:bedrock-agentcore:ap-northeast-1:123456789012:runtime/s04-reader-id", "agentRuntimeVersion": "1", **kwargs}
            return {key: self.runtime[key] for key in ("agentRuntimeId", "agentRuntimeArn", "agentRuntimeVersion")}
        def update_agent_runtime(self, **kwargs):
            calls.append(("runtime.update", kwargs["agentRuntimeArtifact"]))
            runtime_envs.append(kwargs["environmentVariables"])
            self.runtime.update(kwargs); self.runtime["agentRuntimeVersion"] = "2"
            return {"agentRuntimeVersion": "2"}

    control = Control()
    clients = {"sts": Sts(), "s3": S3(), "iam": Iam(), "lambda": Lambda(), "logs": Logs(),
        "bedrock-agentcore-control": control, "bedrock-agentcore": object()}
    monkeypatch.setattr(workflow, "_clients", lambda _profile, writes=False: clients)
    workflow.deploy(run_id, "123456789012", "ap-northeast-1", lambda_zip, runtime_zip, None)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    runtime_calls = [item for item in calls if item[0].startswith("runtime.")]
    assert [item[0] for item in runtime_calls] == ["runtime.create", "runtime.update"]
    assert runtime_calls[0][1] == s03_artifact
    assert runtime_calls[1][1] != s03_artifact
    assert runtime_envs == [{"S04_GATEWAY_URL": "https://gateway.example/mcp",
        "S04_GATEWAY_TOOL_NAME": f"{names['target']}___read_s03_invocation"}]
    assert state["phase"] == "ready"
    assert state["borrowed_artifact"]["owned"] is False
    assert state["borrowed_artifact"]["key"] not in state["owned"].get("objects", {}).values()
    assert all(item[1] != "s03-runtime" for item in calls if item[0] in {"runtime.delete", "runtime.update"})


def test_cleanup_never_deletes_borrowed_s03_artifact_or_shared_bucket(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "STATE_DIR", tmp_path / ".local" / "s04-reader")
    run_id = "borrowed-01"
    state = {"schema_version": 1, "run_id": run_id, "phase": "preflight_complete", "account_id": "123456789012",
        "region": "ap-northeast-1", "bucket": "shared-course-bucket", "borrowed_artifact": {
            "bucket": "shared-course-bucket", "key": "s03/zip", "sha256": "source", "owned": False},
        "operations": {}, "owned": {}, "sessions": [], "names": {}}
    workflow._save(workflow._state_path(run_id), state)
    class Sts:
        def get_caller_identity(self): return {"Account": "123456789012"}
    clients = {"sts": Sts()}
    monkeypatch.setattr(workflow, "_clients", lambda *_args, **_kwargs: clients)
    workflow.cleanup(run_id, "123456789012", "ap-northeast-1", None)
    saved = json.loads(workflow._state_path(run_id).read_text(encoding="utf-8"))
    assert saved["borrowed_artifact"]["owned"] is False
    assert saved["owned"] == {}


def test_cleanup_stops_before_any_delete_when_same_name_runtime_lacks_ownership_tag(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "STATE_DIR", tmp_path / ".local" / "s04-reader")
    run_id = "partial-clean-01"
    runtime_arn = "arn:aws:bedrock-agentcore:ap-northeast-1:123456789012:runtime/owned-runtime"
    state = {"schema_version": 1, "run_id": run_id, "phase": "ready", "account_id": "123456789012",
        "region": "ap-northeast-1", "operations": {}, "owned": {"runtime": {"id": "runtime-id", "arn": runtime_arn}},
        "sessions": [], "runtime_version": "2", "ownership_tag": {"S04ReaderRun": run_id},
        "names": {}, "borrowed_artifact": {"owned": False}}
    workflow._save(workflow._state_path(run_id), state)
    deletes = []
    class Sts:
        def get_caller_identity(self): return {"Account": "123456789012"}
    class Control:
        def get_agent_runtime(self, **_kwargs): return {"agentRuntimeArn": runtime_arn, "agentRuntimeVersion": "2", "agentRuntimeArtifact": {"code": "candidate"}}
        def list_tags_for_resource(self, **_kwargs): return {"tags": {"S04ReaderRun": "different-run"}}
        def delete_agent_runtime(self, **kwargs): deletes.append(kwargs); return {}
    clients = {"sts": Sts(), "bedrock-agentcore-control": Control()}
    monkeypatch.setattr(workflow, "_clients", lambda *_args, **_kwargs: clients)
    with pytest.raises(RuntimeError, match="mismatch"):
        workflow.cleanup(run_id, "123456789012", "ap-northeast-1", None)
    assert deletes == []


def test_resume_does_not_adopt_same_name_role_without_exact_run_tag(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "STATE_DIR", tmp_path / ".local" / "s04-reader")
    run_id = "resume-01"
    role_name = "udemy-agentcore-s04-lambda-abcdef1234"
    request = {"RoleName": role_name, "Description": f"S04 reader {run_id} dedicated role"}
    state = {"schema_version": 1, "run_id": run_id, "account_id": "123456789012", "region": "ap-northeast-1",
        "ownership_tag": {"S04ReaderRun": run_id}, "operations": {"role_create": {
            "operation": "iam.create_role", "request": request, "status": "outcome_unknown"}}, "owned": {}}
    workflow._save(workflow._state_path(run_id), state)
    class Sts:
        def get_caller_identity(self): return {"Account": "123456789012"}
    class Iam:
        def get_role(self, **_kwargs): return {"Role": {"Description": request["Description"], "Tags": [{"Key": "S04ReaderRun", "Value": "another-run"}]}}
    monkeypatch.setattr(workflow, "_clients", lambda *_args, **_kwargs: {"sts": Sts(), "iam": Iam()})
    with pytest.raises(RuntimeError, match="does not match"):
        workflow.resume(run_id, "123456789012", "ap-northeast-1", None)
    saved = json.loads(workflow._state_path(run_id).read_text(encoding="utf-8"))
    assert saved["operations"]["role_create"]["status"] == "outcome_unknown"
    assert saved["owned"] == {}


@pytest.mark.parametrize("bucket_status", ["Enabled", "Suspended"])
def test_preflight_rejects_versioned_bucket_before_any_resource_write(tmp_path, monkeypatch, bucket_status):
    monkeypatch.setattr(workflow, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(workflow, "_s03_local", lambda: {"runtime_id": "s03-runtime", "bucket": "shared-bucket",
        "account_id": "123456789012", "region": "ap-northeast-1"})
    calls = []
    class Sts:
        def get_caller_identity(self): return {"Account": "123456789012"}
    class S3:
        def get_bucket_versioning(self, **kwargs):
            calls.append(("versioning.read", kwargs))
            return {"Status": bucket_status}
    clients = {"sts": Sts(), "s3": S3()}
    monkeypatch.setattr(workflow, "_clients", lambda *_args, **_kwargs: clients)
    with pytest.raises(RuntimeError, match=f"versioning is {bucket_status}"):
        workflow.preflight("version-01", "123456789012", "ap-northeast-1",
            "/aws/bedrock-agentcore/runtimes/s03-runtime-DEFAULT", None)
    assert [call[0] for call in calls] == ["versioning.read"]


def test_log_group_preflight_pages_and_rejects_only_exact_collision():
    class Logs:
        def __init__(self, pages): self.pages = iter(pages); self.calls = []
        def describe_log_groups(self, **kwargs): self.calls.append(kwargs); return next(self.pages)
    exact = "/aws/lambda/reader"
    neighbor = Logs([{"logGroups": [{"logGroupName": exact + "-other"}], "nextToken": "next"},
                     {"logGroups": []}])
    workflow._require_log_group_absent(neighbor, exact)
    assert len(neighbor.calls) == 2 and neighbor.calls[1]["nextToken"] == "next"
    collision = Logs([{"logGroups": [{"logGroupName": exact}], "nextToken": "next"}])
    with pytest.raises(RuntimeError, match="already exists"):
        workflow._require_log_group_absent(collision, exact)
    assert len(collision.calls) == 1


def test_stop_accepts_documented_http_metadata_only_success(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "STATE_DIR", tmp_path / "state")
    run_id = "stop-response"
    state = {"schema_version": 1, "run_id": run_id, "account_id": "123456789012",
        "runtime_arn": "arn:aws:bedrock-agentcore:ap-northeast-1:123456789012:runtime/reader",
        "sessions": ["saved-session"], "operations": {}}
    workflow._save(workflow._state_path(run_id), state)
    class Sts:
        def get_caller_identity(self): return {"Account": "123456789012"}
    class Runtime:
        def stop_runtime_session(self, **kwargs):
            assert kwargs["runtimeSessionId"] == "saved-session"
            return {"ResponseMetadata": {"HTTPStatusCode": 200, "RequestId": "request-1"}}
    monkeypatch.setattr(workflow, "_clients", lambda *_args, **_kwargs: {"sts": Sts(), "bedrock-agentcore": Runtime()})
    workflow.stop(run_id, "123456789012", "ap-northeast-1", None)
    saved = json.loads(workflow._state_path(run_id).read_text(encoding="utf-8"))
    assert saved["stop_attempts"][0]["status"] == "stopped"
    assert saved["stop_attempts"][0]["http_status_code"] == 200


@pytest.mark.parametrize("positive", [None, {"status": "outcome_unknown", "target_timestamp_ms": 123},
                                       {"status": "response_received"}])
def test_negative_requires_saved_positive_response_and_exact_timestamp(tmp_path, monkeypatch, positive):
    monkeypatch.setattr(workflow, "STATE_DIR", tmp_path / "state")
    run_id = "negative-prereq"
    state = {"schema_version": 1, "run_id": run_id, "phase": "ready", "account_id": "123456789012",
        "gateway_url": "https://gateway.example/mcp", "positive_attempts": [positive] if positive else [],
        "negative_attempts": []}
    workflow._save(workflow._state_path(run_id), state)
    sends = []
    monkeypatch.setattr(workflow, "_clients", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(workflow, "_post_gateway_jsonrpc", lambda *_args, **_kwargs: sends.append("post"))
    with pytest.raises(RuntimeError):
        workflow.negative(run_id, "123456789012", "ap-northeast-1", None)
    assert sends == []


def test_negative_sends_exactly_two_gateway_requests_without_mcp_handshake(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "STATE_DIR", tmp_path / "state")
    run_id = "negative-two"
    state = {"schema_version": 1, "run_id": run_id, "phase": "ready", "account_id": "123456789012",
        "gateway_url": "https://gateway.example/mcp", "names": workflow.names_for_run(run_id),
        "positive_attempts": [{"status": "response_received",
            "target_timestamp_ms": 1700000000123}], "negative_attempts": []}
    workflow._save(workflow._state_path(run_id), state)
    class Sts:
        def get_caller_identity(self): return {"Account": "123456789012"}
    monkeypatch.setattr(workflow, "_clients", lambda *_args, **_kwargs: {"sts": Sts()})
    monkeypatch.setattr(workflow, "_signed_gateway_auth", lambda _profile: "sigv4-auth")
    sent = []
    def post(endpoint, payload, auth=None):
        sent.append((endpoint, payload, auth))
        return {"http_status": 400 if auth else 401, "content_type": "application/json", "body": "{}"}
    monkeypatch.setattr(workflow, "_post_gateway_jsonrpc", post)
    workflow.negative(run_id, "123456789012", "ap-northeast-1", None)
    assert len(sent) == 2
    assert sent[0][1]["method"] == "initialize" and sent[0][2] is None
    assert sent[1][1]["method"] == "tools/call" and sent[1][2] == "sigv4-auth"
    args = sent[1][1]["params"]["arguments"]
    assert args == {"target_invocation_id": "not-a-uuid", "target_timestamp_ms": 1700000000123, "max_results": 1}
    assert sent[1][1]["id"] == "s04-reader-invalid-uuid"
    assert sent[1][1]["params"]["name"] == f"{workflow.names_for_run(run_id)['target']}___read_s03_invocation"


def test_gateway_http_post_has_bounded_timeout_no_redirect_and_exact_mcp_headers(monkeypatch):
    captured = {}
    class Response:
        status_code = 200
        content = b'{"jsonrpc":"2.0","id":"probe"}'
        headers = {"content-type": "application/json"}
    class Client:
        def __init__(self, **kwargs): captured["client"] = kwargs
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def post(self, endpoint, **kwargs): captured["post"] = (endpoint, kwargs); return Response()
    monkeypatch.setattr(workflow.httpx, "Client", Client)
    payload = {"jsonrpc": "2.0", "id": "probe", "method": "initialize", "params": {}}
    result = workflow._post_gateway_jsonrpc("https://gateway.example/mcp", payload)
    assert result["http_status"] == 200 and result["body"] == Response.content.decode()
    assert captured["client"]["follow_redirects"] is False
    assert captured["client"]["timeout"].connect == 5
    assert captured["client"]["headers"]["MCP-Protocol-Version"] == "2024-11-05"
    assert captured["post"] == ("https://gateway.example/mcp", {"json": payload})


def test_status_reads_owned_log_group_objects_and_versioning_without_mutation(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(workflow, "STATE_DIR", tmp_path / "state")
    run_id = "status-owned"
    state = {"schema_version": 1, "run_id": run_id, "phase": "cleanup_requested", "account_id": "123456789012",
        "region": "ap-northeast-1", "bucket": "shared-bucket", "owned": {
            "lambda_log_group": "/aws/lambda/reader-owned", "objects": {"lambda": "s04/lambda.zip", "runtime": "s04/runtime.zip"}}}
    workflow._save(workflow._state_path(run_id), state)
    class Sts:
        def get_caller_identity(self): return {"Account": "123456789012"}
    class Logs:
        def describe_log_groups(self, **kwargs): return {"logGroups": [{"logGroupName": "/aws/lambda/reader-owned"}]}
        def list_tags_log_group(self, **kwargs): return {"tags": {"S04ReaderRun": run_id}}
    class S3:
        def get_bucket_versioning(self, **kwargs): return {"Status": "Enabled"}
        def head_object(self, **kwargs): return {"Metadata": {"sha256": kwargs["Key"]}, "ContentLength": 10}
    monkeypatch.setattr(workflow, "_clients", lambda *_args, **_kwargs: {"sts": Sts(), "logs": Logs(), "s3": S3()})
    workflow.status(run_id, "123456789012", "ap-northeast-1", None)
    report = json.loads(capsys.readouterr().out)
    assert report["live"]["lambda_log_group"]["tags"] == {"S04ReaderRun": run_id}
    assert report["live"]["s3_object:lambda"]["status"] == "present"
    assert report["live"]["s3_object:runtime"]["sha256"] == "s04/runtime.zip"
    assert report["live"]["unverified_s3_version_residue"] is True


def test_cleanup_stops_before_any_delete_if_bucket_versioning_changed(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "STATE_DIR", tmp_path / "state")
    run_id = "cleanup-versioned"
    state = {"schema_version": 1, "run_id": run_id, "phase": "cleanup_requested", "account_id": "123456789012",
        "region": "ap-northeast-1", "bucket": "shared-bucket", "sessions": [], "operations": {},
        "owned": {"objects": {"lambda": "s04/lambda.zip"}}, "packages": {"lambda": {"sha256": "digest"}}}
    workflow._save(workflow._state_path(run_id), state)
    deletes = []
    class Sts:
        def get_caller_identity(self): return {"Account": "123456789012"}
    class S3:
        def get_bucket_versioning(self, **kwargs): return {"Status": "Suspended"}
        def delete_object(self, **kwargs): deletes.append(kwargs)
    monkeypatch.setattr(workflow, "_clients", lambda *_args, **_kwargs: {"sts": Sts(), "s3": S3()})
    with pytest.raises(RuntimeError, match="versioning is Suspended"):
        workflow.cleanup(run_id, "123456789012", "ap-northeast-1", None)
    assert deletes == []
