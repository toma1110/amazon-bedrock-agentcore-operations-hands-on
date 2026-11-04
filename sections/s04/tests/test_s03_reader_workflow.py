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


def test_write_checkpoint_keeps_only_allowlisted_rejection_message(tmp_path):
    from botocore.exceptions import ClientError

    state = {"operations": {}}
    with pytest.raises(RuntimeError, match="Do not replay"):
        workflow._write_once(tmp_path / "state.json", state, "create", "lambda.create_function", {},
            lambda: (_ for _ in ()).throw(ClientError({"Error": {"Code": "InvalidParameterValueException",
                "Message": workflow.LAMBDA_ASSUME_ROLE_REJECTION + " secret=do-not-save"},
                "ResponseMetadata": {"RequestId": "req-1"}}, "CreateFunction")))
    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    error = saved["operations"]["create"]["service_error"]
    assert error == {"code": "InvalidParameterValueException", "request_id": "req-1"}
    assert "secret" not in json.dumps(saved)


def _cloudtrail_event(**overrides):
    event = {"requestID": "req-exact", "eventSource": "lambda.amazonaws.com",
        "eventName": "CreateFunction20150331", "awsRegion": "ap-northeast-1",
        "recipientAccountId": "123456789012", "userIdentity": {"accountId": "123456789012"},
        "requestParameters": {"functionName": "reader-exact", "role": "arn:aws:iam::123456789012:role/role-exact"},
        "errorCode": "InvalidParameterValueException",
        "errorMessage": workflow.LAMBDA_ASSUME_ROLE_REJECTION,
        "eventTime": "2026-11-04T14:19:16Z"}
    event.update(overrides)
    return {"CloudTrailEvent": json.dumps(event)}


class _TrailPages:
    def __init__(self, events): self.events = events
    def paginate(self, **_kwargs): return iter([{"Events": self.events}])


class _Trail:
    def __init__(self, events): self.events = events
    def get_paginator(self, name):
        assert name == "lookup_events"
        return _TrailPages(self.events)


def test_cloudtrail_recovery_requires_one_exact_rejection_event():
    event = _cloudtrail_event()
    event_epoch = int(workflow.datetime.fromisoformat("2026-11-04T14:19:16+00:00").timestamp())
    proof = workflow._cloudtrail_lambda_rejection(_Trail([event]), "req-exact", "2026-11-04T14:19:16Z",
        "reader-exact", "arn:aws:iam::123456789012:role/role-exact", "123456789012", event_epoch)
    assert proof["request_id"] == "req-exact" and proof["error_message"] == workflow.LAMBDA_ASSUME_ROLE_REJECTION
    with pytest.raises(RuntimeError, match="one exact"):
        workflow._cloudtrail_lambda_rejection(_Trail([]), "req-exact", "2026-11-04T14:19:16Z",
            "reader-exact", "arn:aws:iam::123456789012:role/role-exact", "123456789012", event_epoch)
    with pytest.raises(RuntimeError, match="one exact"):
        workflow._cloudtrail_lambda_rejection(_Trail([event, event]), "req-exact", "2026-11-04T14:19:16Z",
            "reader-exact", "arn:aws:iam::123456789012:role/role-exact", "123456789012", event_epoch)


@pytest.mark.parametrize("override,expected_function,expected_role,expected_account", [
    ({"requestParameters": {"functionName": "other", "role": "arn:aws:iam::123456789012:role/role-exact"}}, "reader-exact", "arn:aws:iam::123456789012:role/role-exact", "123456789012"),
    ({"requestParameters": {"functionName": "reader-exact", "role": "arn:aws:iam::123456789012:role/other"}}, "reader-exact", "arn:aws:iam::123456789012:role/role-exact", "123456789012"),
    ({"recipientAccountId": "000000000000"}, "reader-exact", "arn:aws:iam::123456789012:role/role-exact", "123456789012"),
    ({"awsRegion": "us-east-1"}, "reader-exact", "arn:aws:iam::123456789012:role/role-exact", "123456789012"),
    ({"errorMessage": "some other rejection"}, "reader-exact", "arn:aws:iam::123456789012:role/role-exact", "123456789012"),
])
def test_cloudtrail_recovery_rejects_wrong_target_account_region_or_error(override, expected_function, expected_role, expected_account):
    with pytest.raises(RuntimeError, match="one exact"):
        workflow._cloudtrail_lambda_rejection(_Trail([_cloudtrail_event(**override)]), "req-exact", "2026-11-04T14:19:16Z",
            expected_function, expected_role, expected_account,
            int(workflow.datetime.fromisoformat("2026-11-04T14:19:16+00:00").timestamp()))


def test_cloudtrail_event_must_match_saved_original_write_time():
    with pytest.raises(RuntimeError, match="original saved"):
        workflow._cloudtrail_lambda_rejection(_Trail([_cloudtrail_event()]), "req-exact", "2026-11-04T14:19:16Z",
            "reader-exact", "arn:aws:iam::123456789012:role/role-exact", "123456789012", 1793800000)


def _gateway_target_event(**overrides):
    params = {"gatewayIdentifier": "gateway-exact", "name": "target-exact", "description": "target description",
        "clientToken": "token-exact", "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
        "targetConfiguration": {"mcp": {"lambda": {"lambdaArn": "arn:lambda:exact",
            "toolSchema": {"inlinePayload": [{"name": "tool-exact"}]}}}}}
    event = {"requestID": "target-req", "eventSource": "bedrock-agentcore.amazonaws.com",
        "eventName": "CreateGatewayTarget", "awsRegion": "ap-northeast-1",
        "recipientAccountId": "123456789012", "userIdentity": {"accountId": "123456789012"},
        "requestParameters": params,
        "errorCode": "ValidationException", "responseElements": {"message": workflow.GATEWAY_CREATING_REJECTION},
        "eventTime": "2026-11-04T14:19:16Z"}
    event.update(overrides)
    return {"CloudTrailEvent": json.dumps(event)}


def test_gateway_target_cloudtrail_requires_unique_exact_rejection_and_client_token():
    stamp = int(workflow.datetime.fromisoformat("2026-11-04T14:19:16+00:00").timestamp())
    saved_request = _gateway_target_event()["CloudTrailEvent"]
    saved_request = json.loads(saved_request)["requestParameters"]
    redacted_request = json.loads(json.dumps(saved_request))
    redacted_request["name"] = "***"
    redacted_request["description"] = "***"
    redacted_request["targetConfiguration"]["mcp"]["lambda"]["toolSchema"]["inlinePayload"][0] = "***"
    args = ("target-req", "2026-11-04T14:19:16Z", saved_request, "123456789012", stamp)
    redacted_event = _gateway_target_event(requestParameters=redacted_request)
    proof = workflow._cloudtrail_gateway_target_rejection(_Trail([redacted_event]), *args)
    assert proof["request_id"] == "target-req"
    assert proof["error_message"] == workflow.GATEWAY_CREATING_REJECTION
    for events in ([], [_gateway_target_event(), _gateway_target_event()]):
        with pytest.raises(RuntimeError, match="one exact"):
            workflow._cloudtrail_gateway_target_rejection(_Trail(events), *args)
    bad_events = [
        {**redacted_request, "gatewayIdentifier": "other"},
        {**redacted_request, "clientToken": "other"},
        {**redacted_request, "credentialProviderConfigurations": []},
        {**redacted_request, "extra": "unexpected"},
        {**redacted_request, "targetConfiguration": {"mcp": {"lambda": {"lambdaArn": "arn:lambda:other",
            "toolSchema": {"inlinePayload": ["***"]}}}}},
        {**redacted_request, "targetConfiguration": {"mcp": {"lambda": {"lambdaArn": "arn:lambda:exact",
            "toolSchema": {"inlinePayload": ["***"], "extra": "unexpected"}}}}},
        {**redacted_request, "targetConfiguration": {"mcp": {"lambda": {"lambdaArn": "arn:lambda:exact",
            "toolSchema": {"inlinePayload": [{"name": "wrong"}]}}}}},
    ]
    bad_events.extend([_gateway_target_event(**override) for override in (
        {"recipientAccountId": "000000000000"}, {"awsRegion": "us-east-1"},
        {"responseElements": {"message": "another rejection"}},
    )])
    for bad_params in bad_events:
        event = bad_params if "CloudTrailEvent" in bad_params else _gateway_target_event(requestParameters=bad_params)
        with pytest.raises(RuntimeError, match="one exact"):
            workflow._cloudtrail_gateway_target_rejection(_Trail([event]), *args)
    with pytest.raises(RuntimeError, match="original saved"):
        workflow._cloudtrail_gateway_target_rejection(_Trail([_gateway_target_event()]),
            "target-req", "2026-11-04T14:19:16Z", saved_request, "123456789012", stamp + 121)


def test_gateway_target_list_reads_every_page_and_preserves_conflicts():
    class Control:
        def __init__(self): self.tokens = []
        def list_gateway_targets(self, **kwargs):
            self.tokens.append(kwargs.get("nextToken"))
            return {"items": ([{"name": "unrelated-target"}] if kwargs.get("nextToken") else []),
                "nextToken": "page-2" if not kwargs.get("nextToken") else None}
    control = Control()
    assert workflow._list_all_gateway_targets(control, "gateway-exact") == [{"name": "unrelated-target"}]
    assert control.tokens == [None, "page-2"]


def _gateway_recovery_state(run_id="recover-target"):
    names = workflow.names_for_run(run_id)
    account = "123456789012"
    gateway_id = "gateway-exact"
    gateway_arn = f"arn:aws:bedrock-agentcore:{workflow.REGION}:{account}:gateway/{gateway_id}"
    request = {"gatewayIdentifier": gateway_id, "name": names["target"],
        "description": f"S04 S03 reader target run {run_id}",
        "clientToken": __import__("hashlib").sha256(f"{run_id}|target".encode()).hexdigest(),
        "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
        "targetConfiguration": {"mcp": {"lambda": {"lambdaArn":
            f"arn:aws:lambda:{workflow.REGION}:{account}:function:{names['lambda']}",
            "toolSchema": {"inlinePayload": [{"name": "read_s03_invocation"}]}}}}}
    original = {"operation": "bedrock-agentcore-control.create_gateway_target", "status": "outcome_unknown",
        "at_epoch": 1793805163, "error_type": "ValidationException", "service_error": {"code": "ValidationException"},
        "request": request}
    return {"schema_version": 1, "run_id": run_id, "account_id": account, "region": workflow.REGION,
        "names": names, "ownership_tag": {"S04ReaderRun": run_id}, "gateway_id": gateway_id,
        "gateway_arn": gateway_arn, "gateway_url": "https://gateway.example/mcp", "target_id": None,
        "operations": {"gateway_target": original}, "owned": {}, "gateway_target_recovery": {
            "evidence": {"request_id": "target-req", "event_id": "event-exact",
                "event_time": "2026-11-04T15:12:43Z", "error_code": "ValidationException",
                "error_message": workflow.GATEWAY_CREATING_REJECTION, "gateway_id": gateway_id,
                "target_name": names["target"], "account_id": account, "region": workflow.REGION}, "attempt": None}}


def _gateway_recovery_clients(state, *, conflict=False, fail=False):
    names, account, gateway_id, gateway_arn = state["names"], state["account_id"], state["gateway_id"], state["gateway_arn"]
    trust = workflow._trust("bedrock-agentcore.amazonaws.com", account, gateway_arn)
    policy = workflow._policy(["lambda:InvokeFunction"],
        [f"arn:aws:lambda:{workflow.REGION}:{account}:function:{names['lambda']}"])
    calls = []
    class Control:
        def get_gateway(self, **_kwargs): return {"status": "READY", "gatewayId": gateway_id,
            "gatewayArn": gateway_arn, "name": names["gateway"],
            "roleArn": f"arn:aws:iam::{account}:role/{names['gateway_role']}", "protocolType": "MCP", "authorizerType": "AWS_IAM"}
        def list_tags_for_resource(self, **_kwargs): return {"tags": state["ownership_tag"]}
        def list_gateway_targets(self, **_kwargs): return {"items": [{"name": "other"}] if conflict else []}
        def create_gateway_target(self, **kwargs):
            calls.append(kwargs)
            if fail: raise TimeoutError("do not save exception detail")
            return {"targetId": "target-123"}
    class Iam:
        def get_role(self, **_kwargs): return {"Role": {"AssumeRolePolicyDocument": trust}}
        def get_role_policy(self, **_kwargs): return {"PolicyDocument": policy}
    event_request = json.loads(json.dumps(state["operations"]["gateway_target"]["request"]))
    event_request["name"] = "***"
    event_request["description"] = "***"
    inline = event_request["targetConfiguration"]["mcp"]["lambda"]["toolSchema"].get("inlinePayload", [])
    if inline:
        inline[0] = "***"
    event = {"requestID": "target-req", "eventID": "event-exact",
        "eventSource": "bedrock-agentcore.amazonaws.com", "eventName": "CreateGatewayTarget",
        "awsRegion": workflow.REGION, "recipientAccountId": account,
        "requestParameters": event_request, "errorCode": "ValidationException",
        "responseElements": {"message": workflow.GATEWAY_CREATING_REJECTION},
        "eventTime": "2026-11-04T15:12:43Z"}
    trail = _Trail([{"CloudTrailEvent": json.dumps(event)}])
    return {"bedrock-agentcore-control": Control(), "iam": Iam(), "cloudtrail": trail}, calls


def test_gateway_target_recovery_is_one_write_preserves_original_and_refuses_conflict(tmp_path):
    state = _gateway_recovery_state()
    original = json.loads(json.dumps(state["operations"]["gateway_target"]))
    clients, calls = _gateway_recovery_clients(state)
    state_path = tmp_path / "state.json"
    workflow._recover_rejected_gateway_target(clients, state_path, state)
    assert len(calls) == 1
    assert calls[0] == original["request"]
    assert state["operations"]["gateway_target"] == original
    assert state["gateway_target_recovery"]["attempt"]["status"] == "accepted"
    assert state["target_id"] == "target-123"
    with pytest.raises(RuntimeError, match="already reconciled|unused CloudTrail"):
        workflow._recover_rejected_gateway_target(clients, state_path, state)
    assert len(calls) == 1


def test_gateway_target_recovery_rejects_forged_or_mismatched_saved_evidence(tmp_path):
    state = _gateway_recovery_state("forged-target")
    clients, calls = _gateway_recovery_clients(state)
    for key, value in (("gateway_id", "other-gateway"), ("account_id", "000000000000"),
                       ("region", "us-east-1"), ("event_time", "2026-11-04T15:30:00Z"),
                       ("error_message", "different rejection"), ("target_name", "other-target"),
                       ("event_id", "forged-event")):
        corrupted = json.loads(json.dumps(state))
        corrupted["gateway_target_recovery"]["evidence"][key] = value
        with pytest.raises(RuntimeError, match="CloudTrail|unused CloudTrail"):
            workflow._recover_rejected_gateway_target(clients, tmp_path / f"{key}.json", corrupted)
    assert calls == []

    conflicted = _gateway_recovery_state("conflict-target")
    bad_clients, bad_calls = _gateway_recovery_clients(conflicted, conflict=True)
    with pytest.raises(RuntimeError, match="target entries"):
        workflow._recover_rejected_gateway_target(bad_clients, tmp_path / "conflict.json", conflicted)
    assert bad_calls == []


def test_gateway_target_recovery_unknown_result_is_terminal_and_not_replayed(tmp_path):
    state = _gateway_recovery_state("unknown-target")
    original = json.loads(json.dumps(state["operations"]["gateway_target"]))
    clients, calls = _gateway_recovery_clients(state, fail=True)
    state_path = tmp_path / "state.json"
    with pytest.raises(RuntimeError, match="outcome is unknown"):
        workflow._recover_rejected_gateway_target(clients, state_path, state)
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["operations"]["gateway_target"] == original
    assert saved["gateway_target_recovery"]["attempt"]["status"] == "outcome_unknown"
    with pytest.raises(RuntimeError, match="unused CloudTrail"):
        workflow._recover_rejected_gateway_target(clients, state_path, state)
    assert len(calls) == 1


def _recovery_state(tmp_path, package):
    run_id = "recover-01"
    role_arn = "arn:aws:iam::123456789012:role/lambda-role"
    state = {"schema_version": 1, "run_id": run_id, "account_id": "123456789012", "region": "ap-northeast-1",
        "bucket": "shared", "ownership_tag": {"S04ReaderRun": run_id},
        "operations": {"lambda_function": {"operation": "lambda.create_function", "status": "outcome_unknown",
            "request": {"FunctionName": "reader-exact", "Role": role_arn,
                "Code": {"S3Bucket": "shared", "S3Key": "s04/lambda.zip"}}}},
        "packages": {"lambda": {"sha256": workflow._sha256(package)}},
        "owned": {"objects": {"lambda": "s04/lambda.zip"}},
        "lambda_recovery": {"evidence": {"request_id": "req-exact"}, "attempt": None}}
    return state


def _recovery_clients(state, package, *, exists=False, fail=False):
    from botocore.exceptions import ClientError
    calls = []
    class Lambda:
        def get_function(self, **_kwargs):
            if exists:
                return {"Configuration": {"FunctionName": "reader-exact"}}
            raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "GetFunction")
        def create_function(self, **_kwargs):
            calls.append("create")
            if fail: raise TimeoutError("secret must not be recorded")
            return {"FunctionName": "reader-exact", "FunctionArn": "arn:lambda:reader-exact"}
    role_arn = state["operations"]["lambda_function"]["request"]["Role"]
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
        "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    class Iam:
        def get_role(self, **_kwargs): return {"Role": {"Arn": role_arn,
            "Tags": [{"Key": "S04ReaderRun", "Value": state["run_id"]}], "AssumeRolePolicyDocument": trust}}
    class S3:
        def head_object(self, **_kwargs): return {"Metadata": {"sha256": state["packages"]["lambda"]["sha256"]},
            "ContentLength": package.stat().st_size}
    return {"lambda": Lambda(), "iam": Iam(), "s3": S3()}, calls


def test_recovery_creates_once_and_rejects_second_attempt(tmp_path, monkeypatch):
    package = tmp_path / "lambda.zip"; package.write_bytes(b"candidate")
    state = _recovery_state(tmp_path, package)
    clients, calls = _recovery_clients(state, package)
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(workflow.time, "sleep", lambda _seconds: None)
    workflow._recover_rejected_lambda_create(clients, state_path, state, package)
    assert calls == ["create"]
    assert state["operations"]["lambda_function"]["status"] == "outcome_unknown"
    assert state["lambda_recovery"]["attempt"]["status"] == "accepted"
    with pytest.raises(RuntimeError, match="unused CloudTrail-confirmed"):
        workflow._recover_rejected_lambda_create(clients, state_path, state, package)
    assert calls == ["create"]


def test_initial_lambda_create_waits_until_role_has_minimum_age_and_reads_it_back(monkeypatch):
    role_name = "lambda-role"
    now = 2_000
    sleeps = []
    monkeypatch.setattr(workflow.time, "time", lambda: now)
    monkeypatch.setattr(workflow.time, "sleep", lambda seconds: sleeps.append(seconds))
    role_arn = "arn:aws:iam::123456789012:role/lambda-role"
    expected_trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
        "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    expected_policy = {"Version": "2012-10-17", "Statement": []}
    class Iam:
        def get_role(self, **_kwargs): return {"Role": {"Arn": role_arn,
            "Tags": [{"Key": "S04ReaderRun", "Value": "run-1"}], "AssumeRolePolicyDocument": expected_trust}}
        def get_role_policy(self, **_kwargs): return {"PolicyDocument": expected_policy}
    state = {"operations": {"role_" + role_name: {"status": "accepted", "at_epoch": 1995}},
        "ownership_tag": {"S04ReaderRun": "run-1"}}
    workflow._wait_lambda_role_min_age(Iam(), state, role_name, "123456789012", expected_policy)
    assert sleeps == [5]


def test_initial_lambda_role_age_gate_does_not_sleep_when_minimum_already_elapsed(monkeypatch):
    sleeps = []
    monkeypatch.setattr(workflow.time, "time", lambda: 2_000)
    monkeypatch.setattr(workflow.time, "sleep", lambda seconds: sleeps.append(seconds))
    role_name = "lambda-role"
    role_arn = "arn:aws:iam::123456789012:role/lambda-role"
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
        "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    policy = {"Version": "2012-10-17", "Statement": []}
    class Iam:
        def get_role(self, **_kwargs): return {"Role": {"Arn": role_arn,
            "Tags": [{"Key": "S04ReaderRun", "Value": "run-1"}], "AssumeRolePolicyDocument": trust}}
        def get_role_policy(self, **_kwargs): return {"PolicyDocument": policy}
    workflow._wait_lambda_role_min_age(Iam(), {"operations": {"role_" + role_name: {"status": "accepted", "at_epoch": 1980}},
        "ownership_tag": {"S04ReaderRun": "run-1"}}, role_name, "123456789012", policy)
    assert sleeps == []


def test_recovery_preserves_unknown_failure_and_existing_lambda(tmp_path, monkeypatch):
    package = tmp_path / "lambda.zip"; package.write_bytes(b"candidate")
    state = _recovery_state(tmp_path, package)
    clients, calls = _recovery_clients(state, package, fail=True)
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(workflow.time, "sleep", lambda _seconds: None)
    with pytest.raises(RuntimeError, match="outcome is unknown"):
        workflow._recover_rejected_lambda_create(clients, state_path, state, package)
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["lambda_recovery"]["attempt"]["status"] == "outcome_unknown"
    assert "secret" not in json.dumps(saved)
    with pytest.raises(RuntimeError, match="unused CloudTrail-confirmed"):
        workflow._recover_rejected_lambda_create(clients, state_path, state, package)
    assert calls == ["create"]

    existing_state = _recovery_state(tmp_path, package)
    existing_clients, existing_calls = _recovery_clients(existing_state, package, exists=True)
    with pytest.raises(RuntimeError, match="already exists"):
        workflow._recover_rejected_lambda_create(existing_clients, tmp_path / "existing.json", existing_state, package)
    assert existing_calls == []


def _resume_recovery_state(run_id="resume-recovery"):
    role_arn = "arn:aws:iam::123456789012:role/lambda-role"
    request = {"FunctionName": "reader-exact", "Role": role_arn,
        "Description": "S04 S03 read-only invocation summary run " + run_id}
    return {"schema_version": 1, "run_id": run_id, "account_id": "123456789012", "region": "ap-northeast-1",
        "ownership_tag": {"S04ReaderRun": run_id}, "owned": {},
        "packages": {"lambda": {"sha256": "00" * 32}},
        "operations": {"lambda_function": {"operation": "lambda.create_function", "status": "outcome_unknown",
            "request": request}},
        "lambda_recovery": {"evidence": {"request_id": "req-1", "event_time": "2026-11-04T14:19:16Z",
            "error_code": "InvalidParameterValueException", "error_message": workflow.LAMBDA_ASSUME_ROLE_REJECTION,
            "function_name": request["FunctionName"], "role_arn": role_arn,
            "account_id": "123456789012", "region": "ap-northeast-1"},
            "attempt": {"status": "outcome_unknown"}}}


def test_resume_reconciles_lambda_recovery_without_rewriting_original_and_continues_other_unknown(tmp_path, monkeypatch):
    run_id = "resume-recovery"
    state = _resume_recovery_state(run_id)
    state["operations"]["put_lambda"] = {"operation": "s3.put_object", "status": "outcome_unknown",
        "request": {"Bucket": "shared", "Key": "s04/lambda.zip", "Metadata": {"sha256": "zip-hash"}}}
    monkeypatch.setattr(workflow, "STATE_DIR", tmp_path)
    workflow._save(workflow._state_path(run_id), state)
    class Sts:
        def get_caller_identity(self): return {"Account": "123456789012"}
    class Lambda:
        def get_function(self, **_kwargs): return {"Configuration": {"FunctionName": "reader-exact",
            "FunctionArn": "arn:lambda:reader-exact", "Role": state["operations"]["lambda_function"]["request"]["Role"],
            "Description": state["operations"]["lambda_function"]["request"]["Description"],
            "CodeSha256": __import__("base64").b64encode(bytes.fromhex("00" * 32)).decode("ascii")}}
        def list_tags(self, **_kwargs): return {"Tags": state["ownership_tag"]}
    class S3:
        def head_object(self, **_kwargs): return {"Metadata": {"sha256": "zip-hash"}, "ContentLength": 1}
    monkeypatch.setattr(workflow, "_clients", lambda *_args, **_kwargs: {"sts": Sts(), "lambda": Lambda(), "s3": S3()})
    workflow.resume(run_id, "123456789012", "ap-northeast-1", None)
    saved = json.loads(workflow._state_path(run_id).read_text(encoding="utf-8"))
    assert saved["operations"]["lambda_function"]["status"] == "outcome_unknown"
    assert saved["lambda_recovery"]["attempt"]["status"] == "accepted"
    assert saved["operations"]["put_lambda"]["status"] == "accepted"


def test_resume_keeps_absent_unknown_recovery_blocked_without_replay(tmp_path, monkeypatch):
    run_id = "resume-absent"
    state = _resume_recovery_state(run_id)
    monkeypatch.setattr(workflow, "STATE_DIR", tmp_path)
    workflow._save(workflow._state_path(run_id), state)
    class Sts:
        def get_caller_identity(self): return {"Account": "123456789012"}
    class Lambda:
        def get_function(self, **_kwargs):
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "GetFunction")
    monkeypatch.setattr(workflow, "_clients", lambda *_args, **_kwargs: {"sts": Sts(), "lambda": Lambda()})
    workflow.resume(run_id, "123456789012", "ap-northeast-1", None)
    saved = json.loads(workflow._state_path(run_id).read_text(encoding="utf-8"))
    assert saved["operations"]["lambda_function"]["status"] == "outcome_unknown"
    assert saved["lambda_recovery"]["attempt"]["status"] == "outcome_unknown"


def test_runtime_and_lambda_policy_do_not_include_s03_or_bucket_mutations():
    statement = workflow._policy(["lambda:InvokeFunction"], ["arn:aws:lambda:ap-northeast-1:123456789012:function:only-this"])
    serialized = json.dumps(statement)
    assert "lambda:InvokeFunction" in serialized
    assert "s3:DeleteObject" not in serialized
    assert "bedrock-agentcore:UpdateAgentRuntime" not in serialized
    assert "AdministratorAccess" not in serialized


def test_deploy_trust_readback_uses_only_the_saved_exact_accepted_narrowing():
    account = "123456789012"
    gateway_id = "udemy-agentcore-s04-reader-f8d7257221-xe2kmcsqgt"
    gateway_arn = f"arn:aws:bedrock-agentcore:{workflow.REGION}:{account}:gateway/{gateway_id}"
    role = "udemy-agentcore-s04-gateway-f8d7257221"
    exact = workflow._trust("bedrock-agentcore.amazonaws.com", account, gateway_arn)
    state = {"gateway_id": gateway_id, "gateway_arn": gateway_arn, "operations": {
        "gateway_trust_narrow": {"operation": "iam.update_assume_role_policy", "status": "accepted",
            "request": {"RoleName": role, "PolicyDocument": json.dumps(exact)}}}}
    bootstrap = workflow._trust("bedrock-agentcore.amazonaws.com", account,
        f"arn:aws:bedrock-agentcore:{workflow.REGION}:{account}:gateway/*")
    assert workflow._gateway_role_trust_for_deploy(state, account, role, bootstrap) == exact
    for mutation in (
        lambda record: record.update(operation="iam.create_role"),
        lambda record: record.update(status="outcome_unknown"),
        lambda record: record["request"].update(RoleName="other-role"),
        lambda record: record["request"].update(PolicyDocument=json.dumps(bootstrap)),
    ):
        corrupted = json.loads(json.dumps(state))
        mutation(corrupted["operations"]["gateway_trust_narrow"])
        with pytest.raises(RuntimeError):
            workflow._gateway_role_trust_for_deploy(corrupted, account, role, bootstrap)


@pytest.mark.parametrize("use_recovery,use_gateway_recovery,bootstrap_metadata", [
    (False, False, {"requireMMDSV2": True}), (True, False, {"requireMMDSV2": True}),
    (False, True, None), (False, True, {"requireMMDSV2": False}), (False, True, "missing"),
    (True, True, None), (True, True, {"requireMMDSV2": False}), (True, True, "missing")])
def test_deploy_bootstraps_from_borrowed_s03_artifact_then_updates_only_new_runtime(
        tmp_path, monkeypatch, use_recovery, use_gateway_recovery, bootstrap_metadata):
    import base64
    import hashlib

    monkeypatch.setattr(workflow, "STATE_DIR", tmp_path / ".local" / "s04-reader")
    run_id = "p245proof-1104a" if use_gateway_recovery else "two-stage-01"
    account_id = "123456789012"
    names = workflow.names_for_run(run_id)
    s03_artifact = {"codeConfiguration": {"code": {"s3": {"bucket": "shared-course-bucket", "prefix": "s03/zip"}},
        "runtime": "PYTHON_3_13", "entryPoint": ["runtime_app.py"]}}
    snapshot = {"agentRuntimeVersion": "1", "roleArn": f"arn:aws:iam::{account_id}:role/s03",
        "platformVersion": "V2", "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"}, "environmentVariables": {},
        "metadataConfiguration": {"requireMMDSV2": True},
        "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
        "agentRuntimeArtifact": s03_artifact}
    state = {"schema_version": 1, "run_id": run_id, "phase": "preflight_complete", "account_id": account_id,
        "region": "ap-northeast-1", "s03_runtime_id": "s03-runtime", "s03_log_group": "/aws/bedrock-agentcore/runtimes/s03-runtime-DEFAULT",
        "bucket": "shared-course-bucket", "names": names, "s03_snapshot": snapshot,
        "borrowed_artifact": {"bucket": "shared-course-bucket", "key": "s03/zip", "sha256": "source-hash", "owned": False},
        "ownership_tag": {"S04ReaderRun": run_id}, "operations": {}, "owned": {}, "sessions": [], "invocations": []}
    lambda_zip = tmp_path / "reader.zip"; lambda_zip.write_bytes(b"lambda package")
    runtime_zip = tmp_path / "runtime.zip"; runtime_zip.write_bytes(b"runtime package")
    lambda_hash = workflow._sha256(lambda_zip)
    runtime_hash = workflow._sha256(runtime_zip)
    lambda_key = f"{names['s3_prefix']}/lambda-{lambda_hash}.zip"
    if use_recovery or use_gateway_recovery:
        request = {"FunctionName": names["lambda"], "Runtime": "python3.13",
            "Role": f"arn:aws:iam::{account_id}:role/{names['lambda_role']}",
            "Handler": "s03_invocation_reader.lambda_handler", "Code": {"S3Bucket": "shared-course-bucket", "S3Key": lambda_key},
            "Description": f"S04 S03 read-only invocation summary run {run_id}", "Timeout": 30, "MemorySize": 512,
            "Publish": False, "Architectures": ["x86_64"], "Tags": {"S04ReaderRun": run_id},
            "Environment": {"Variables": {"S03_LOG_GROUP": "/aws/bedrock-agentcore/runtimes/s03-runtime-DEFAULT"}}}
        state["packages"] = {"lambda": {"path": str(lambda_zip.resolve()), "sha256": lambda_hash},
            "reader": {"path": str(runtime_zip.resolve()), "sha256": runtime_hash}}
        state["operations"]["lambda_function"] = {"operation": "lambda.create_function", "status": "outcome_unknown",
            "at_epoch": 1793801955, "request": request}
        state["lambda_recovery"] = {"evidence": {"request_id": "req-exact", "event_time": "2026-11-04T14:19:16Z",
            "error_code": "InvalidParameterValueException", "error_message": workflow.LAMBDA_ASSUME_ROLE_REJECTION,
                "function_name": names["lambda"], "role_arn": request["Role"], "account_id": account_id, "region": "ap-northeast-1"},
            "attempt": None}
    if use_gateway_recovery:
        gateway_id = "udemy-agentcore-s04-reader-f8d7257221-xe2kmcsqgt"
        gateway_arn = f"arn:aws:bedrock-agentcore:ap-northeast-1:{account_id}:gateway/{gateway_id}"
        gateway_url = "https://gateway.example/mcp"
        gateway_response = {"gatewayId": gateway_id, "gatewayArn": gateway_arn, "gatewayUrl": gateway_url}
        state.update({"gateway_id": gateway_id, "gateway_arn": gateway_arn, "gateway_url": gateway_url,
            "owned": {"gateway": {"id": gateway_id, "arn": gateway_arn}}})
        state["operations"]["gateway"] = {"operation": "bedrock-agentcore-control.create_gateway",
            "status": "accepted", "request": {"name": names["gateway"]}, "response": gateway_response}
        gateway_role_arn = f"arn:aws:iam::{account_id}:role/{names['gateway_role']}"
        bootstrap_trust = workflow._trust("bedrock-agentcore.amazonaws.com", account_id,
            f"arn:aws:bedrock-agentcore:ap-northeast-1:{account_id}:gateway/*")
        narrow_trust = workflow._trust("bedrock-agentcore.amazonaws.com", account_id, gateway_arn)
        gateway_policy = workflow._policy(["lambda:InvokeFunction"],
            [f"arn:aws:lambda:ap-northeast-1:{account_id}:function:{names['lambda']}"])
        state["operations"]["role_" + names["gateway_role"]] = {"operation": "iam.create_role", "status": "accepted",
            "request": {"RoleName": names["gateway_role"], "AssumeRolePolicyDocument": json.dumps(bootstrap_trust)},
            "response": {"Role": {"Arn": gateway_role_arn}}}
        state["operations"]["policy_" + names["gateway_role"]] = {"operation": "iam.put_role_policy", "status": "accepted",
            "request": {"RoleName": names["gateway_role"], "PolicyName": "S03ReaderLeastPrivilege",
                "PolicyDocument": json.dumps(gateway_policy)}}
        state["operations"]["gateway_trust_narrow"] = {"operation": "iam.update_assume_role_policy", "status": "accepted",
            "request": {"RoleName": names["gateway_role"], "PolicyDocument": json.dumps(narrow_trust)}}
        schema = {"inlinePayload": [{"name": "read_s03_invocation",
            "description": "Read one bounded S03 invocation summary by exact UUID and timestamp.",
            "inputSchema": {"type": "object", "properties": {
                "target_invocation_id": {"type": "string"}, "target_timestamp_ms": {"type": "integer"},
                "max_results": {"type": "integer", "description": "Integer from 1 to 5."}},
                "required": ["target_invocation_id", "target_timestamp_ms", "max_results"]}}]}
        target_request = {"gatewayIdentifier": gateway_id, "name": names["target"],
            "description": f"S04 S03 reader target run {run_id}",
            "targetConfiguration": {"mcp": {"lambda": {"lambdaArn": f"arn:aws:lambda:ap-northeast-1:{account_id}:function:{names['lambda']}", "toolSchema": schema}}},
            "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
            "clientToken": hashlib.sha256(f"{run_id}|target".encode()).hexdigest()}
        state["operations"]["gateway_target"] = {"operation": "bedrock-agentcore-control.create_gateway_target",
            "status": "outcome_unknown", "at_epoch": 1793805163, "error_type": "ValidationException",
            "service_error": {"code": "ValidationException"}, "request": target_request}
        state["gateway_target_recovery"] = {"evidence": {"request_id": "target-req", "event_id": "event-exact",
            "event_time": "2026-11-04T15:12:43Z", "error_code": "ValidationException",
            "error_message": workflow.GATEWAY_CREATING_REJECTION, "gateway_id": gateway_id,
            "target_name": names["target"], "account_id": account_id, "region": workflow.REGION},
            "attempt": {"status": "accepted", "response": {"targetId": "target12345"}}}
        state["lambda_recovery"]["attempt"] = {"status": "accepted", "response": {"FunctionName": names["lambda"]}}
        state["owned"]["lambda"] = names["lambda"]

        runtime_role_arn = f"arn:aws:iam::{account_id}:role/{names['runtime_role']}"
        state["operations"]["runtime_role"] = {"operation": "iam.create_role", "status": "accepted",
            "request": {"RoleName": names["runtime_role"], "AssumeRolePolicyDocument": json.dumps(
                workflow._runtime_role_trust(state, account_id)), "Tags": [
                    {"Key": key, "Value": value} for key, value in state["ownership_tag"].items()]}}
        state["operations"]["runtime_policy"] = {"operation": "iam.put_role_policy", "status": "accepted",
            "request": {"RoleName": names["runtime_role"], "PolicyName": "S03ReaderLeastPrivilege",
                "PolicyDocument": json.dumps(workflow._runtime_role_policy(state, account_id))}}
        original_runtime_request = {**workflow._runtime_create_request(state, account_id),
            "metadataConfiguration": snapshot["metadataConfiguration"]}
        state["operations"]["create_runtime"] = {
            "operation": "bedrock-agentcore-control.create_agent_runtime", "status": "outcome_unknown",
            "at_epoch": 1793807888, "error_type": "ParamValidationError", "request": original_runtime_request}
        state["runtime_create_recovery"] = {"evidence": workflow._runtime_create_validation_proof(state, account_id),
            "attempt": None}
    state_path = workflow._state_path(run_id)
    workflow._save(state_path, state)
    calls = []
    runtime_envs = []

    class Sts:
        def get_caller_identity(self): return {"Account": account_id}

    class S3:
        def put_object(self, **kwargs): calls.append(("s3.put_object", kwargs["Key"])); return {"ETag": "e"}
        def head_object(self, **kwargs):
            if kwargs.get("Key") == lambda_key:
                return {"Metadata": {"sha256": lambda_hash}, "ContentLength": lambda_zip.stat().st_size}
            return {"Metadata": {"sha256": "source-hash"}}

    class Iam:
        policies = {}
        trusts = {}
        roles = {}
        def create_role(self, **kwargs):
            calls.append(("iam.create_role", kwargs["RoleName"]))
            if kwargs["RoleName"] == names["runtime_role"]:
                assert kwargs.get("Tags") == [{"Key": key, "Value": value}
                                               for key, value in state["ownership_tag"].items()]
            self.trusts[kwargs["RoleName"]] = json.loads(kwargs["AssumeRolePolicyDocument"])
            role = {"Arn": f"arn:aws:iam::{account_id}:role/{kwargs['RoleName']}",
                "RoleName": kwargs["RoleName"], "Tags": kwargs.get("Tags", [])}
            self.roles[kwargs["RoleName"]] = role
            return {"Role": role}
        def put_role_policy(self, **kwargs):
            calls.append(("iam.put_role_policy", kwargs["RoleName"]))
            self.policies[(kwargs["RoleName"], kwargs["PolicyName"])] = json.loads(kwargs["PolicyDocument"])
            return {}
        def update_assume_role_policy(self, **kwargs):
            calls.append(("iam.update_assume_role_policy", kwargs["RoleName"]))
            self.trusts[kwargs["RoleName"]] = json.loads(kwargs["PolicyDocument"])
            return {}
        def get_role(self, **kwargs):
            return {"Role": {**self.roles[kwargs["RoleName"]],
                "AssumeRolePolicyDocument": self.trusts[kwargs["RoleName"]]}}
        def get_role_policy(self, **kwargs):
            return {"PolicyDocument": self.policies[(kwargs["RoleName"], kwargs["PolicyName"])]}

    if use_gateway_recovery:
        gateway_role = names["gateway_role"]
        gateway_arn = state["gateway_arn"]
        Iam.trusts[gateway_role] = workflow._trust("bedrock-agentcore.amazonaws.com", account_id, gateway_arn)
        Iam.policies[(gateway_role, "S03ReaderLeastPrivilege")] = workflow._policy(["lambda:InvokeFunction"],
            [f"arn:aws:lambda:ap-northeast-1:{account_id}:function:{names['lambda']}"])
        Iam.roles[gateway_role] = {"Arn": f"arn:aws:iam::{account_id}:role/{gateway_role}",
            "RoleName": gateway_role, "Tags": [{"Key": "S04ReaderRun", "Value": run_id}]}
        runtime_role = names["runtime_role"]
        Iam.trusts[runtime_role] = workflow._runtime_role_trust(state, account_id)
        Iam.policies[(runtime_role, "S03ReaderLeastPrivilege")] = workflow._runtime_role_policy(state, account_id)
        Iam.roles[runtime_role] = {"Arn": f"arn:aws:iam::{account_id}:role/{runtime_role}",
            "RoleName": runtime_role, "Tags": state["operations"]["runtime_role"]["request"]["Tags"]}

    class Lambda:
        created = use_gateway_recovery
        def create_function(self, **kwargs):
            calls.append(("lambda.create_function", kwargs["FunctionName"]))
            self.created = True
            return {"FunctionArn": f"arn:aws:lambda:ap-northeast-1:{account_id}:function:{kwargs['FunctionName']}"}
        def get_function(self, **kwargs):
            if not self.created:
                from botocore.exceptions import ClientError
                raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "GetFunction")
            digest = hashlib.sha256(lambda_zip.read_bytes()).digest()
            return {"Configuration": {"State": "Active", "LastUpdateStatus": "Successful",
                "Role": f"arn:aws:iam::{account_id}:role/{names['lambda_role']}",
                "CodeSha256": base64.b64encode(digest).decode("ascii"),
                "Description": f"S04 S03 read-only invocation summary run {run_id}",
                "FunctionArn": f"arn:aws:lambda:ap-northeast-1:{account_id}:function:{names['lambda']}"}}
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
            return {"gatewayId": "gateway-1234567890", "gatewayArn": f"arn:aws:bedrock-agentcore:ap-northeast-1:{account_id}:gateway/gateway-1234567890", "gatewayUrl": "https://gateway.example/mcp"}
        def get_gateway(self, **kwargs):
            calls.append(("gateway.read", kwargs["gatewayIdentifier"]))
            gateway_id = state.get("gateway_id") or "gateway-1234567890"
            gateway_arn = state.get("gateway_arn") or f"arn:aws:bedrock-agentcore:ap-northeast-1:{account_id}:gateway/{gateway_id}"
            return {"status": "READY", "gatewayId": gateway_id,
            "gatewayArn": gateway_arn,
            "name": names["gateway"], "roleArn": f"arn:aws:iam::{account_id}:role/{names['gateway_role']}",
            "protocolType": "MCP", "authorizerType": "AWS_IAM"}
        def list_tags_for_resource(self, **_kwargs): return {"tags": {"S04ReaderRun": run_id}}
        def create_gateway_target(self, **kwargs): calls.append(("target.create", kwargs["name"])); return {"targetId": "target12345"}
        def list_gateway_targets(self, **kwargs): return {"items": []}
        def get_gateway_target(self, **kwargs): return {"status": "READY"}
        def list_agent_runtimes(self, **_kwargs): return {"agentRuntimes": []}
        def list_gateway_targets(self, **_kwargs):
            if use_gateway_recovery:
                return {"items": [{"targetId": "target12345", "name": names["target"]}]}
            return {"items": []}
        def create_agent_runtime(self, **kwargs):
            calls.append(("runtime.create", kwargs["agentRuntimeArtifact"]))
            self.runtime = {"agentRuntimeId": "s04-reader-id", "agentRuntimeArn": f"arn:aws:bedrock-agentcore:ap-northeast-1:{account_id}:runtime/s04-reader-id", "agentRuntimeVersion": "1", **kwargs}
            if bootstrap_metadata != "missing":
                self.runtime["metadataConfiguration"] = bootstrap_metadata
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
    monkeypatch.setattr(workflow.time, "sleep", lambda _seconds: None)
    workflow.deploy(run_id, account_id, "ap-northeast-1", lambda_zip, runtime_zip, None)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    runtime_calls = [item for item in calls if item[0].startswith("runtime.")]
    assert [item[0] for item in runtime_calls] == ["runtime.create", "runtime.update"]
    assert runtime_calls[0][1] == s03_artifact
    assert runtime_calls[1][1] != s03_artifact
    assert state["operations"]["update_runtime"]["request"]["metadataConfiguration"] == {"requireMMDSV2": True}
    assert control.runtime["metadataConfiguration"] == {"requireMMDSV2": True}
    assert runtime_envs == [{"S04_GATEWAY_URL": "https://gateway.example/mcp",
        "S04_GATEWAY_TOOL_NAME": f"{names['target']}___read_s03_invocation"}]
    assert state["phase"] == "ready"
    runtime_iam_policy = Iam.policies[(names["runtime_role"], "S03ReaderLeastPrivilege")]
    assert state["operations"]["runtime_role"]["request"]["Tags"] == [
        {"Key": "S04ReaderRun", "Value": run_id}]
    assert Iam.roles[names["runtime_role"]]["Tags"] == state["operations"]["runtime_role"]["request"]["Tags"]
    put_resource = next(statement["Resource"] for statement in runtime_iam_policy["Statement"]
                        if "logs:PutResourcePolicy" in statement["Action"])
    assert put_resource == (f"arn:aws:logs:ap-northeast-1:{account_id}:log-group:"
                            f"/aws/bedrock-agentcore/runtimes/{names['runtime']}-*")
    assert state["borrowed_artifact"]["owned"] is False
    assert state["borrowed_artifact"]["key"] not in state["owned"].get("objects", {}).values()
    assert all(item[1] != "s03-runtime" for item in calls if item[0] in {"runtime.delete", "runtime.update"})
    if not use_gateway_recovery:
        assert calls.index(("gateway.read", "gateway-1234567890")) < calls.index(("target.create", names["target"]))
    if use_gateway_recovery:
        assert not [item for item in calls if item[0] == "target.create"]
        assert state["target_id"] == "target12345"
        assert state["gateway_target_recovery"]["attempt"]["status"] == "accepted"
        assert calls.index(("gateway.read", state["gateway_id"])) < calls.index(("runtime.create", s03_artifact))
        assert state["operations"]["gateway_target"]["status"] == "outcome_unknown"
        assert state["operations"]["create_runtime"]["status"] == "outcome_unknown"
        assert state["runtime_create_recovery"]["attempt"]["status"] == "accepted"
        assert state["runtime_create_recovery"]["attempt"]["clientToken"] == state["operations"]["create_runtime"]["request"]["clientToken"]
        assert state["runtime_create_recovery"]["attempt"]["request_sha256"] == workflow._request_sha256(
            workflow._runtime_create_request(state, account_id))
        assert len([item for item in calls if item[0] == "runtime.create"]) == 1
        assert [item for item in calls if item[0] == "lambda.create_function"] == (
            [] if use_recovery or use_gateway_recovery else [("lambda.create_function", names["lambda"])])
    if use_recovery:
        assert state["operations"]["lambda_function"]["status"] == "outcome_unknown"
        assert state["lambda_recovery"]["attempt"]["status"] == "accepted"
        expected_lambda_creates = [] if use_gateway_recovery else [("lambda.create_function", names["lambda"])]
        assert [item for item in calls if item[0] == "lambda.create_function"] == expected_lambda_creates


def test_runtime_create_validation_proof_matches_installed_sdk_and_rejects_mutated_request(tmp_path):
    import botocore.session
    from botocore.validate import validate_parameters

    run_id = "sdk-proof-01"
    account_id = "123456789012"
    names = workflow.names_for_run(run_id)
    state = {"run_id": run_id, "account_id": account_id, "region": workflow.REGION,
        "names": names, "ownership_tag": {"S04ReaderRun": run_id},
        "s03_snapshot": {"agentRuntimeArtifact": {"codeConfiguration": {
                "code": {"s3": {"bucket": "shared-course-bucket", "prefix": "s03/zip"}},
                "runtime": "PYTHON_3_13", "entryPoint": ["runtime_app.py"]}},
            "metadataConfiguration": {"requireMMDSV2": True}},
        "operations": {}}
    corrected = workflow._runtime_create_request(state, account_id)
    original = {**corrected, "metadataConfiguration": state["s03_snapshot"]["metadataConfiguration"]}
    state["operations"]["create_runtime"] = {
        "operation": "bedrock-agentcore-control.create_agent_runtime", "status": "outcome_unknown",
        "error_type": "ParamValidationError", "service_error": None, "at_epoch": 1793807888,
        "request": original}

    proof = workflow._runtime_create_validation_proof(state, account_id)
    service = botocore.session.get_session().get_service_model("bedrock-agentcore-control")
    create_shape = service.operation_model("CreateAgentRuntime").input_shape
    update_shape = service.operation_model("UpdateAgentRuntime").input_shape
    assert "metadataConfiguration" not in create_shape.members
    assert "metadataConfiguration" in update_shape.members
    with pytest.raises(Exception, match="metadataConfiguration"):
        validate_parameters(original, create_shape)
    validate_parameters(corrected, create_shape)
    validate_parameters({"agentRuntimeId": "runtime-id", "metadataConfiguration": {"requireMMDSV2": True},
        "roleArn": "arn:aws:iam::123456789012:role/runtime",
        "agentRuntimeArtifact": state["s03_snapshot"]["agentRuntimeArtifact"]}, update_shape)
    assert proof["invalid_parameter"] == "metadataConfiguration"
    assert proof["corrected_request_sha256"] == workflow._request_sha256(corrected)

    state["operations"]["create_runtime"]["request"]["roleArn"] += "/unexpected"
    with pytest.raises(RuntimeError, match="exact known local metadataConfiguration"):
        workflow._runtime_create_validation_proof(state, account_id)


def test_runtime_create_unknown_recovery_attempt_is_never_replayed(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "STATE_DIR", tmp_path)
    run_id = "runtime-no-replay-01"
    account_id = "123456789012"
    names = workflow.names_for_run(run_id)
    state = {"run_id": run_id, "account_id": account_id, "region": workflow.REGION,
        "names": names, "ownership_tag": {"S04ReaderRun": run_id},
        "s03_snapshot": {"agentRuntimeArtifact": {"codeConfiguration": {
                "code": {"s3": {"bucket": "shared-course-bucket", "prefix": "s03/zip"}},
                "runtime": "PYTHON_3_13", "entryPoint": ["runtime_app.py"]}},
            "metadataConfiguration": {"requireMMDSV2": True}},
        "owned": {}, "operations": {}}
    corrected = workflow._runtime_create_request(state, account_id)
    state["operations"]["create_runtime"] = {
        "operation": "bedrock-agentcore-control.create_agent_runtime", "status": "outcome_unknown",
        "error_type": "ParamValidationError", "at_epoch": 1793807888,
        "request": {**corrected, "metadataConfiguration": state["s03_snapshot"]["metadataConfiguration"]}}
    state["runtime_create_recovery"] = {
        "evidence": workflow._runtime_create_validation_proof(state, account_id), "attempt": None}
    path = workflow._state_path(run_id)
    workflow._save(path, state)

    class Control:
        calls = 0
        def create_agent_runtime(self, **_kwargs):
            self.calls += 1
            raise TimeoutError("response lost")

    control = Control()
    clients = {"bedrock-agentcore-control": control}
    monkeypatch.setattr(workflow, "_runtime_create_recovery_prerequisites", lambda *_args: None)
    with pytest.raises(RuntimeError, match="outcome is unknown"):
        workflow._recover_rejected_runtime_create(clients, path, state, account_id)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["runtime_create_recovery"]["attempt"]["status"] == "outcome_unknown"
    assert saved["operations"]["create_runtime"]["status"] == "outcome_unknown"
    with pytest.raises(RuntimeError, match="No unused"):
        workflow._recover_rejected_runtime_create(clients, path, saved, account_id)
    assert control.calls == 1


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
                                       {"status": "response_received"},
                                       {"status": "response_received", "target_timestamp_ms": 1700000000123},
                                       {"status": "response_received", "target_timestamp_ms": 1700000000123,
                                        "target_invocation_id": "not-a-uuid"}])
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
            "target_timestamp_ms": 1700000000123,
            "target_invocation_id": "c53fc3cc-5ba9-49a0-9b89-a0aad5ca6c6b"}], "negative_attempts": []}
    workflow._save(workflow._state_path(run_id), state)
    class Sts:
        def get_caller_identity(self): return {"Account": "123456789012"}
    monkeypatch.setattr(workflow, "_clients", lambda *_args, **_kwargs: {"sts": Sts()})
    monkeypatch.setattr(workflow, "_signed_gateway_auth", lambda _profile: "sigv4-auth")
    monkeypatch.setattr(workflow.time, "time", lambda: 1700000000.123)
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
    assert args == {"target_invocation_id": "c53fc3cc-5ba9-49a0-9b89-a0aad5ca6c6b",
                    "target_timestamp_ms": 1699308800123, "max_results": 1}
    assert sent[1][1]["id"] == "s04-reader-outside-window"
    assert sent[1][1]["params"]["name"] == f"{workflow.names_for_run(run_id)['target']}___read_s03_invocation"
    saved = json.loads(workflow._state_path(run_id).read_text(encoding="utf-8"))
    assert saved["negative_attempts"][1]["request"] == sent[1][1]
    from datetime import datetime, timezone
    monkeypatch.syspath_prepend(str(S04_DIR))
    from s03_invocation_reader import RequestRejected, validate_request
    with pytest.raises(RequestRejected, match="within the last 7 days"):
        validate_request(args, now=datetime.fromtimestamp(1700000000.123, tz=timezone.utc))
    with pytest.raises(RuntimeError, match="do not replay"):
        workflow.negative(run_id, "123456789012", "ap-northeast-1", None)
    assert len(sent) == 2


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


def test_runtime_log_policy_lookup_stops_on_unexpected_scope_or_arn():
    arn = workflow.runtime_log_group_arn("reader-id-123", "123456789012")
    exact = {"policyName": "service-made-name", "policyScope": "RESOURCE", "resourceArn": arn,
             "revisionId": "rev-1", "policyDocument": "{}"}
    class Logs:
        def __init__(self): self.calls = []
        def describe_resource_policies(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return {"resourcePolicies": [{**exact, "resourceArn": arn + ":log-stream:*"}], "nextToken": "page2"}
            return {"resourcePolicies": [exact]}
    logs = Logs()
    with pytest.raises(RuntimeError, match="outside the exact Runtime log group scope"):
        workflow._runtime_log_policies(logs, arn)
    assert len(logs.calls) == 1
    assert logs.calls[0] == {"policyScope": "RESOURCE", "resourceArn": arn}


def test_runtime_log_policy_lookup_returns_exact_paged_policy():
    arn = workflow.runtime_log_group_arn("reader-id-123", "123456789012")
    policy = {"policyName": "service-made-name", "policyScope": "RESOURCE", "resourceArn": arn,
              "revisionId": "rev-1", "policyDocument": "{}"}
    class Logs:
        def __init__(self): self.calls = 0
        def describe_resource_policies(self, **_kwargs):
            self.calls += 1
            return {"resourcePolicies": [policy] if self.calls == 2 else [],
                    "nextToken": "page2" if self.calls == 1 else None}
    logs = Logs()
    assert workflow._runtime_log_policies(logs, arn) == [policy]
    assert logs.calls == 2


def test_runtime_default_log_group_lookup_checks_later_pages_and_absence():
    exact_name = workflow.runtime_log_group("reader-id-123")
    class Logs:
        def __init__(self, pages): self.pages = iter(pages); self.calls = []
        def describe_log_groups(self, **kwargs): self.calls.append(kwargs); return next(self.pages)
    found = Logs([{"logGroups": [{"logGroupName": exact_name + "-neighbor"}], "nextToken": "later"},
                  {"logGroups": [{"logGroupName": exact_name}]}])
    assert workflow._exact_log_group(found, exact_name) == {"logGroupName": exact_name}
    assert len(found.calls) == 2 and found.calls[1]["nextToken"] == "later"
    absent = Logs([{"logGroups": [], "nextToken": "later"}, {"logGroups": []}])
    assert workflow._exact_log_group(absent, exact_name) is None
    assert len(absent.calls) == 2


def test_cleanup_deletes_runtime_then_exact_resource_policy_then_default_group(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "STATE_DIR", tmp_path / "state")
    run_id = "logs-cleanup-01"
    runtime_id = "reader-runtime-id-123"
    runtime_arn = f"arn:aws:bedrock-agentcore:ap-northeast-1:123456789012:runtime/{runtime_id}"
    group = workflow.runtime_log_group(runtime_id)
    group_arn = workflow.runtime_log_group_arn(runtime_id, "123456789012")
    policy = {"policyName": "service-policy-name", "policyScope": "RESOURCE", "resourceArn": group_arn,
              "revisionId": "revision-7", "policyDocument": "{\"Version\":\"2012-10-17\"}"}
    state = {"schema_version": 1, "run_id": run_id, "phase": "ready", "account_id": "123456789012",
        "region": "ap-northeast-1", "operations": {"update_runtime": {"request": {"agentRuntimeArtifact": {"code": "reader"}}}},
        "owned": {"runtime": {"id": runtime_id, "arn": runtime_arn}}, "sessions": [], "runtime_version": "2",
        "ownership_tag": {"S04ReaderRun": run_id}}
    workflow._save(workflow._state_path(run_id), state)
    events = []
    class Sts:
        def get_caller_identity(self): return {"Account": "123456789012"}
    class Control:
        exists = True
        def get_agent_runtime(self, **_kwargs):
            if not self.exists: return {"status": "ABSENT"}
            return {"agentRuntimeArn": runtime_arn, "agentRuntimeVersion": "2",
                    "agentRuntimeArtifact": {"code": "reader"}}
        def list_tags_for_resource(self, **_kwargs): return {"tags": {"S04ReaderRun": run_id}}
        def delete_agent_runtime(self, **_kwargs): events.append("runtime.delete"); self.exists = False; return {}
    class Logs:
        policy_exists = True
        group_exists = True
        def describe_resource_policies(self, **kwargs):
            assert kwargs == {"policyScope": "RESOURCE", "resourceArn": group_arn}
            return {"resourcePolicies": [policy] if self.policy_exists else []}
        def describe_log_groups(self, **kwargs):
            assert kwargs["logGroupNamePrefix"] == group
            return {"logGroups": [{"logGroupName": group}] if self.group_exists else []}
        def delete_resource_policy(self, **kwargs):
            assert kwargs == {"policyName": policy["policyName"], "resourceArn": group_arn,
                              "expectedRevisionId": policy["revisionId"]}
            events.append("policy.delete"); self.policy_exists = False; return {}
        def delete_log_group(self, **kwargs):
            assert kwargs == {"logGroupName": group}
            events.append("group.delete"); self.group_exists = False; return {}
    control, logs = Control(), Logs()
    monkeypatch.setattr(workflow, "_clients", lambda *_args, **_kwargs: {
        "sts": Sts(), "bedrock-agentcore-control": control, "logs": logs})
    monkeypatch.setattr(workflow.time, "sleep", lambda *_args: None)
    workflow.cleanup(run_id, "123456789012", "ap-northeast-1", None)
    assert events == ["runtime.delete", "policy.delete", "group.delete"]
    saved = json.loads(workflow._state_path(run_id).read_text(encoding="utf-8"))
    assert saved["owned"]["runtime_log_group"] == group
    assert saved["owned"]["runtime_log_policy"] == policy
    assert saved["operations"]["delete_runtime_log_policy"]["status"] == "accepted"
    assert saved["operations"]["delete_runtime_log_group"]["status"] == "accepted"


@pytest.mark.parametrize("change_point", ["before_policy_delete", "other_policy_remains"])
def test_cleanup_stops_if_runtime_log_policy_changes_or_another_policy_remains(tmp_path, monkeypatch, change_point):
    monkeypatch.setattr(workflow, "STATE_DIR", tmp_path / "state")
    run_id = "logs-policy-change"
    runtime_id = "reader-runtime-id-456"
    runtime_arn = f"arn:aws:bedrock-agentcore:ap-northeast-1:123456789012:runtime/{runtime_id}"
    group = workflow.runtime_log_group(runtime_id)
    group_arn = workflow.runtime_log_group_arn(runtime_id, "123456789012")
    saved_policy = {"policyName": "service-policy-name", "policyScope": "RESOURCE", "resourceArn": group_arn,
                    "revisionId": "revision-7", "policyDocument": "{}"}
    other_policy = {**saved_policy, "policyName": "unexpected-policy", "revisionId": "revision-8"}
    state = {"schema_version": 1, "run_id": run_id, "phase": "ready", "account_id": "123456789012",
        "region": "ap-northeast-1", "operations": {"update_runtime": {"request": {"agentRuntimeArtifact": {"code": "reader"}}}},
        "owned": {"runtime": {"id": runtime_id, "arn": runtime_arn}}, "sessions": [], "runtime_version": "2",
        "ownership_tag": {"S04ReaderRun": run_id}}
    workflow._save(workflow._state_path(run_id), state)
    events = []
    class Sts:
        def get_caller_identity(self): return {"Account": "123456789012"}
    class Control:
        exists = True
        def get_agent_runtime(self, **_kwargs):
            if not self.exists: return {"status": "ABSENT"}
            return {"agentRuntimeArn": runtime_arn, "agentRuntimeVersion": "2", "agentRuntimeArtifact": {"code": "reader"}}
        def list_tags_for_resource(self, **_kwargs): return {"tags": {"S04ReaderRun": run_id}}
        def delete_agent_runtime(self, **_kwargs): events.append("runtime.delete"); self.exists = False; return {}
    class Logs:
        policy = saved_policy
        group_exists = True
        calls = 0
        def describe_resource_policies(self, **kwargs):
            assert kwargs == {"policyScope": "RESOURCE", "resourceArn": group_arn}
            self.calls += 1
            if change_point == "before_policy_delete" and self.calls >= 2:
                return {"resourcePolicies": [{**saved_policy, "revisionId": "revision-changed"}]}
            return {"resourcePolicies": [self.policy] if self.policy else []}
        def describe_log_groups(self, **kwargs):
            assert kwargs["logGroupNamePrefix"] == group
            return {"logGroups": [{"logGroupName": group}] if self.group_exists else []}
        def delete_resource_policy(self, **_kwargs):
            events.append("policy.delete")
            if change_point == "other_policy_remains": self.policy = other_policy
            else: self.policy = None
            return {}
        def delete_log_group(self, **_kwargs): events.append("group.delete"); self.group_exists = False; return {}
    control, logs = Control(), Logs()
    monkeypatch.setattr(workflow, "_clients", lambda *_args, **_kwargs: {
        "sts": Sts(), "bedrock-agentcore-control": control, "logs": logs})
    monkeypatch.setattr(workflow.time, "sleep", lambda *_args: None)
    with pytest.raises(RuntimeError, match="policy"):
        workflow.cleanup(run_id, "123456789012", "ap-northeast-1", None)
    assert events[0] == "runtime.delete"
    assert "group.delete" not in events
    if change_point == "before_policy_delete":
        assert events == ["runtime.delete"]
    else:
        assert events == ["runtime.delete", "policy.delete"]
