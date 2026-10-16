from __future__ import annotations

import json
import importlib.util
import sys
from pathlib import Path

import pytest

S04_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(S04_DIR))

_SPEC = importlib.util.spec_from_file_location("s04_aws_workflow", S04_DIR / "aws_workflow.py")
assert _SPEC is not None and _SPEC.loader is not None
aws_workflow = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = aws_workflow
_SPEC.loader.exec_module(aws_workflow)


def test_preflight_requires_exact_account_and_effective_stop_permission(monkeypatch, capsys, tmp_path):
    class Sts:
        def get_caller_identity(self):
            return {"Account": "111122223333", "Arn": "arn:aws:iam::111122223333:user/example"}

    class Iam:
        def simulate_principal_policy(self, **kwargs):
            assert kwargs["ActionNames"] == ["bedrock-agentcore:StopRuntimeSession"]
            assert kwargs["ResourceArns"] == [f"arn:aws:bedrock-agentcore:{aws_workflow.REGION}:111122223333:runtime/runtime-from-s03-state"]
            return {"EvaluationResults": [{"EvalDecision": "allowed", "OrganizationsDecisionDetail": {"AllowedByOrganizations": True}}]}

    class Control:
        def get_agent_runtime(self, **_kwargs):
            return {"status": "READY"}

    monkeypatch.setattr(aws_workflow, "_clients", lambda _profile: {"sts": Sts(), "iam": Iam(), "bedrock-agentcore-control": Control()})
    monkeypatch.setattr(aws_workflow, "_s03_state", lambda _clients, _account: {"runtime_id": "runtime-from-s03-state", "runtime_arn": "arn:aws:bedrock-agentcore:ap-northeast-1:111122223333:runtime/runtime-from-s03-state"})
    monkeypatch.setattr(aws_workflow, "_inventory_exact_names", lambda _clients: ["checked exact names"])
    monkeypatch.setattr(aws_workflow, "PREFLIGHT_PATH", tmp_path / "preflight-test.json")
    aws_workflow.preflight("111122223333", aws_workflow.REGION, "runtime-from-s03-state", None)
    output = capsys.readouterr().out
    result = json.loads(output[output.index("{\n"):])
    assert result["stop_permission"] == "allowed"
    assert result["inventory_checked_targets"] == ["checked exact names"]
    saved = json.loads((tmp_path / "preflight-test.json").read_text(encoding="utf-8"))
    assert saved["inventory_checked_targets"] == ["checked exact names"]
    assert "all_exact_s04_names_absent" not in saved


def test_preflight_fails_closed_when_stop_permission_is_not_confirmed(monkeypatch):
    class Sts:
        def get_caller_identity(self):
            return {"Account": "111122223333", "Arn": "arn:aws:iam::111122223333:user/example"}

    class Iam:
        def simulate_principal_policy(self, **_kwargs):
            return {"EvaluationResults": [{"EvalDecision": "implicitDeny", "OrganizationsDecisionDetail": {"AllowedByOrganizations": True}}]}

    class Control:
        def get_agent_runtime(self, **_kwargs):
            return {"status": "READY"}

    monkeypatch.setattr(aws_workflow, "_clients", lambda _profile: {"sts": Sts(), "iam": Iam(), "bedrock-agentcore-control": Control()})
    monkeypatch.setattr(aws_workflow, "_s03_state", lambda _clients, _account: {"runtime_id": "runtime-from-s03-state", "runtime_arn": "arn:aws:bedrock-agentcore:ap-northeast-1:111122223333:runtime/runtime-from-s03-state"})
    with pytest.raises(RuntimeError, match="NO-GO"):
        aws_workflow.preflight("111122223333", aws_workflow.REGION, "runtime-from-s03-state", None)


def test_gateway_target_and_tool_names_match_the_approved_target():
    assert aws_workflow.GATEWAY_TARGET_NAME == "udemy-agentcore-s04-cw-target"
    assert aws_workflow.GATEWAY_TOOL_NAME == "udemy-agentcore-s04-cw-target___read_synthetic_cloudwatch"


@pytest.mark.parametrize("gate_case", ["http_200", "missing_stop", "latest_unconfirmed", "unresolved_inflight"])
def test_update_runtime_artifact_is_explicit_and_preserves_v3_and_s03_history(monkeypatch, tmp_path, capsys, gate_case):
    import hashlib

    runtime_zip = tmp_path / "runtime.zip"
    runtime_zip.write_bytes(b"candidate runtime")
    digest = hashlib.sha256(runtime_zip.read_bytes()).hexdigest()
    old_artifact = {"codeConfiguration": {"code": {"s3": {"bucket": "bucket", "prefix": "s04/runtime-old.zip"}}}}
    request = {"agentRuntimeId": "runtime-1", "agentRuntimeArtifact": old_artifact,
        "roleArn": "role", "platformVersion": "V2", "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "MCP"}, "environmentVariables": {"S04_GATEWAY_URL": "url"},
        "metadataConfiguration": {"requireMMDSV2": True}, "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
        "clientToken": "old-token"}
    state = {"schema_version": 1, "account_id": "111122223333", "region": aws_workflow.REGION,
        "phase": "ready", "runtime_id": "runtime-1", "runtime_version": "3", "runtime_update_request": request,
        "artifact_bucket": "bucket", "artifact_key": "s04/runtime-old.zip", "s03_bucket": "bucket",
        "prior_runtime": {"agentRuntimeId": "runtime-1", "version": "S03"}, "sessions": [], "runtime_sha256": "old-hash"}
    if gate_case != "missing_stop":
        state["sessions"] = ["prior-session"]
        state["stop_attempts"] = [{"session_id": "prior-session", "status": "stopped", "status_code": 200,
                                    "http_status_code": 200, "request_id": "stop-request-1"}]
    else:
        state["sessions"] = ["prior-session"]
        state["stop_attempts"] = []
    if gate_case == "latest_unconfirmed":
        state["stop_attempts"].append({"session_id": "prior-session", "status": "unconfirmed",
                                        "status_code": 500, "http_status_code": 500, "request_id": "stop-request-2"})
    if gate_case in {"http_200", "unresolved_inflight"}:
        original_attempt = {"session_id": "prior-session", "status": "requested", "step": 1, "at_epoch": 123}
        state["positive_attempts"] = [original_attempt]
        if gate_case == "http_200":
            state["reconciliations"] = [{"session_id": "prior-session", "runtime_status": 500,
                "error_type": "RuntimeError", "tool_call_count": 0, "tool_names": [],
                "stop_response": {"status": "stopped", "status_code": 200, "http_status_code": 200,
                                  "request_id": "stop-request-1"}}]
    live = {"status": "READY", "agentRuntimeVersion": "3", "agentRuntimeArtifact": old_artifact,
        "roleArn": "role", "platformVersion": "V2", "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "MCP"}, "environmentVariables": {"S04_GATEWAY_URL": "url"},
        "metadataConfiguration": {"requireMMDSV2": True}, "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800}}

    class Sts:
        def get_caller_identity(self): return {"Account": "111122223333"}
    class S3:
        def __init__(self): self.puts = 0
        def head_bucket(self, **kwargs): assert kwargs["Bucket"] == "bucket"
        def head_object(self, **kwargs):
            if kwargs["Key"] == "s04/runtime-old.zip": return {"Metadata": {"sha256": "old-hash"}, "ContentLength": 10}
            raise aws_workflow.ClientError({"Error": {"Code": "NotFound"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject")
        def put_object(self, **kwargs): self.puts += 1
    class Control:
        def __init__(self): self.updates = 0
        def get_agent_runtime(self, **kwargs): return live
        def update_agent_runtime(self, **kwargs):
            self.updates += 1
            assert kwargs["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"] == {"bucket": "bucket", "prefix": f"s04/runtime-{digest}.zip"}
            assert kwargs["roleArn"] == request["roleArn"] and kwargs["lifecycleConfiguration"] == request["lifecycleConfiguration"]
            assert kwargs["clientToken"] != "old-token"
            return {"agentRuntimeVersion": "4"}

    s3, control = S3(), Control()
    clients = {"sts": Sts(), "s3": s3, "bedrock-agentcore-control": control}
    monkeypatch.setattr(aws_workflow, "_clients", lambda _profile: clients)
    monkeypatch.setattr(aws_workflow, "_state", lambda _clients: state)
    monkeypatch.setattr(aws_workflow, "_save", lambda _state: None)
    monkeypatch.setattr(aws_workflow, "_wait_ready", lambda _control, _state: {
        **live, "agentRuntimeVersion": "4", "agentRuntimeArtifact": _state["runtime_artifact_updates"][-1]["update_request"]["agentRuntimeArtifact"]})

    if gate_case in {"missing_stop", "latest_unconfirmed", "unresolved_inflight"}:
        with pytest.raises(RuntimeError):
            aws_workflow.update_runtime_artifact("111122223333", aws_workflow.REGION, "runtime-1", runtime_zip, None)
        assert s3.puts == 0 and control.updates == 0
        return
    aws_workflow.update_runtime_artifact("111122223333", aws_workflow.REGION, "runtime-1", runtime_zip, None)
    assert s3.puts == 1 and control.updates == 1
    assert state["positive_attempts"][0] == original_attempt
    assert state["runtime_update_request"] is request
    assert state["prior_runtime"]["version"] == "S03"
    assert state["active_runtime_artifact"]["sha256"] == digest
    assert state["runtime_artifact_updates"][-1]["status"] == "ready"
    assert "version 4" in capsys.readouterr().out


def test_update_runtime_artifact_rejects_live_drift_before_any_write(monkeypatch, tmp_path):
    runtime_zip = tmp_path / "runtime.zip"
    runtime_zip.write_bytes(b"candidate")
    old_artifact = {"codeConfiguration": {"code": {"s3": {"bucket": "bucket", "prefix": "old"}}}}
    request = {"agentRuntimeArtifact": old_artifact, "roleArn": "role", "platformVersion": "V2",
        "networkConfiguration": {}, "protocolConfiguration": {}, "environmentVariables": {},
        "metadataConfiguration": {"requireMMDSV2": True}, "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800}}
    state = {"account_id": "111122223333", "region": aws_workflow.REGION, "phase": "ready", "runtime_id": "r",
        "runtime_version": "3", "runtime_update_request": request, "artifact_bucket": "bucket", "artifact_key": "old",
        "s03_bucket": "bucket", "sessions": []}
    class Sts:
        def get_caller_identity(self): return {"Account": "111122223333"}
    class Control:
        def get_agent_runtime(self, **_kwargs): return {"status": "UPDATING", "agentRuntimeVersion": "3"}
    class S3:
        def __getattr__(self, name): raise AssertionError(f"unexpected S3 write/read: {name}")
    monkeypatch.setattr(aws_workflow, "_clients", lambda _profile: {"sts": Sts(), "s3": S3(), "bedrock-agentcore-control": Control()})
    monkeypatch.setattr(aws_workflow, "_state", lambda _clients: state)
    with pytest.raises(RuntimeError, match="drifted"):
        aws_workflow.update_runtime_artifact("111122223333", aws_workflow.REGION, "r", runtime_zip, None)


def test_update_runtime_artifact_resume_is_read_only_and_never_repeats_update(monkeypatch, tmp_path, capsys):
    runtime_zip = tmp_path / "runtime.zip"
    runtime_zip.write_bytes(b"candidate")
    digest = __import__("hashlib").sha256(b"candidate").hexdigest()
    artifact = {"codeConfiguration": {"code": {"s3": {"bucket": "bucket", "prefix": "new"}}}}
    request = {"agentRuntimeArtifact": artifact}
    state = {"account_id": "111122223333", "region": aws_workflow.REGION, "phase": "ready", "runtime_id": "r",
        "sessions": [], "runtime_artifact_updates": [{"candidate_sha256": digest, "bucket": "bucket", "key": "new",
            "size": 9, "runtime_version": "4", "update_request": request, "status": "update_submitted"}]}
    class Sts:
        def get_caller_identity(self): return {"Account": "111122223333"}
    class Control:
        def get_agent_runtime(self, **_kwargs): return {"status": "READY", "agentRuntimeVersion": "4", "agentRuntimeArtifact": artifact}
        def update_agent_runtime(self, **_kwargs): raise AssertionError("must not repeat Runtime update")
    class S3:
        def head_object(self, **_kwargs): return {"Metadata": {"sha256": digest}, "ContentLength": 9}
        def put_object(self, **_kwargs): raise AssertionError("must not repeat artifact upload")
    monkeypatch.setattr(aws_workflow, "_clients", lambda _profile: {"sts": Sts(), "s3": S3(), "bedrock-agentcore-control": Control()})
    monkeypatch.setattr(aws_workflow, "_state", lambda _clients: state)
    monkeypatch.setattr(aws_workflow, "_save", lambda _state: None)
    aws_workflow.update_runtime_artifact("111122223333", aws_workflow.REGION, "r", runtime_zip, None)
    assert state["active_runtime_artifact"]["runtime_version"] == "4"
    assert "no AWS write" in capsys.readouterr().out


def test_invoke_runtime_guard_checks_active_candidate_version_and_artifact():
    request = {"lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
        "agentRuntimeArtifact": {"codeConfiguration": {"code": {"s3": {"bucket": "bucket", "prefix": "candidate"}}}},
        "roleArn": "role", "environmentVariables": {"S04_GATEWAY_URL": "url"}}
    state = {"runtime_id": "runtime-1", "runtime_version": "3", "runtime_update_request": request,
        "active_runtime_artifact": {"runtime_version": "4", "update_request": request}}
    class Control:
        def get_agent_runtime(self, **_kwargs):
            return {"status": "READY", "agentRuntimeVersion": "3", "agentRuntimeArtifact": request["agentRuntimeArtifact"],
                "roleArn": "role", "environmentVariables": request["environmentVariables"],
                "lifecycleConfiguration": request["lifecycleConfiguration"]}
    with pytest.raises(RuntimeError, match="saved S04 deployment"):
        aws_workflow._check_deployed_lifecycle(Control(), state)


@pytest.mark.parametrize("data_type", ["logs", "metrics"])
def test_positive_trace_requires_success_for_exact_seeded_query(data_type):
    start = "2026-01-01T00:00:00Z"
    end = "2026-01-01T00:01:00Z"
    result = {"trace": [{
        "name": "read_synthetic_cloudwatch",
        "arguments": {"data_type": data_type, "service": "catalog-api", "start_time": start,
                      "end_time": end, "max_results": 50},
        "result": {"ok": True, "data": {"source": {"service": "catalog-api"}}},
    }]}
    assert aws_workflow._has_expected_positive_trace(result, data_type, start, end)
    assert not aws_workflow._has_expected_positive_trace({"trace": []}, data_type, start, end)
    result["trace"][0]["result"]["ok"] = False
    assert not aws_workflow._has_expected_positive_trace(result, data_type, start, end)


def test_positive_query_window_is_derived_from_retained_seed_in_utc():
    from datetime import datetime, timezone

    seed = {"status": "metric_written", "region": aws_workflow.REGION,
            "service": "catalog-api", "request_id": "s04-demo-123",
            "timestamp": "2026-01-01T00:00:00Z"}
    assert aws_workflow._positive_query_window(seed, datetime(2026, 1, 1, 0, 0, 3, tzinfo=timezone.utc)) == (
        "2025-12-31T23:59:00Z", "2026-01-01T00:00:03Z", "s04-demo-123")

    # Once the seed is more than a minute old, keep the request bounded around
    # its metric minute instead of extending the query window to the current time.
    assert aws_workflow._positive_query_window(seed, datetime(2026, 1, 1, 0, 10, tzinfo=timezone.utc)) == (
        "2025-12-31T23:59:00Z", "2026-01-01T00:02:00Z", "s04-demo-123")

    # The saved seed remains usable for the documented CloudWatch retention period.
    seven_days = datetime(2026, 1, 8, 0, 0, tzinfo=timezone.utc)
    assert aws_workflow._positive_query_window(seed, seven_days)[2] == "s04-demo-123"
    with pytest.raises(RuntimeError, match="7-day retention"):
        aws_workflow._positive_query_window(seed, datetime(2026, 1, 8, 0, 0, 1, tzinfo=timezone.utc))


@pytest.mark.parametrize(
    ("result", "text", "expected"),
    [
        ({"status": "success"}, '{"ok":false,"error":"service is outside the S04 synthetic dataset"}', True),
        ({"status": "error"}, '{"ok":false,"error":"service is outside the S04 synthetic dataset"}', False),
        ({"status": "success"}, '{"ok":false,"error":"different failure"}', False),
        ({"status": "success"}, '{"ok":true,"data":{}}', False),
        ({"status": "success"}, "not-json", False),
    ],
)
def test_negative_response_requires_exact_successful_service_guard(result, text, expected):
    assert aws_workflow._is_expected_negative_response(result, text) is expected


def test_gateway_tool_schema_matches_installed_botocore_api_model():
    from botocore.exceptions import ParamValidationError
    from botocore.session import get_session
    from botocore.validate import validate_parameters

    schema = aws_workflow._gateway_tool_schema()
    original = aws_workflow.TOOL_SCHEMA["inlinePayload"][0]["inputSchema"]["properties"]
    assert "enum" in original["data_type"]
    assert "minimum" in original["max_results"] and "maximum" in original["max_results"]
    assert "enum" not in schema["inlinePayload"][0]["inputSchema"]["properties"]["data_type"]
    assert "minimum" not in schema["inlinePayload"][0]["inputSchema"]["properties"]["max_results"]
    assert "maximum" not in schema["inlinePayload"][0]["inputSchema"]["properties"]["max_results"]

    service = get_session().get_service_model("bedrock-agentcore-control")
    operation = service.operation_model("CreateGatewayTarget")
    request = {
        "gatewayIdentifier": "udemy-agentcore-s04-gateway-example",
        "name": aws_workflow.GATEWAY_TARGET_NAME,
        "targetConfiguration": {
            "mcp": {
                "lambda": {
                    "lambdaArn": "arn:aws:lambda:ap-northeast-1:123456789012:function:udemy-agentcore-s04-cw-reader",
                    "toolSchema": schema,
                }
            }
        },
        "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
        "clientToken": "00000000-0000-4000-8000-000000000245",
    }
    validate_parameters(request, operation.input_shape)
    request["targetConfiguration"]["mcp"]["lambda"]["toolSchema"] = aws_workflow.TOOL_SCHEMA
    with pytest.raises(ParamValidationError) as error:
        validate_parameters(request, operation.input_shape)
    assert "Unknown parameter" in str(error.value)
    assert "enum" in str(error.value)
    assert "minimum" in str(error.value)
    assert "maximum" in str(error.value)


def test_gateway_recovery_resolves_summary_without_arn_and_validates_details():
    role_arn = "arn:aws:iam::123456789012:role/udemy-agentcore-s04-gateway-role"
    gateway_arn = "arn:aws:bedrock-agentcore:ap-northeast-1:123456789012:gateway/gw-example"
    summary = {"gatewayId": "gw-example", "name": aws_workflow.GATEWAY_NAME, "status": "READY"}

    class Control:
        def get_gateway(self, **kwargs):
            assert kwargs == {"gatewayIdentifier": "gw-example"}
            return {
                "gatewayId": "gw-example",
                "gatewayArn": gateway_arn,
                "name": aws_workflow.GATEWAY_NAME,
                "roleArn": role_arn,
                "protocolType": "MCP",
                "authorizerType": "AWS_IAM",
                "status": "READY",
            }

    recovered = aws_workflow._recover_gateway_from_summary(Control(), summary, role_arn)
    assert recovered["gatewayArn"] == gateway_arn
    assert "gatewayArn" not in summary


def test_gateway_recovery_rejects_summary_whose_resolved_gateway_is_out_of_scope():
    class Control:
        def get_gateway(self, **_kwargs):
            return {
                "gatewayId": "gw-other",
                "gatewayArn": "arn:aws:bedrock-agentcore:ap-northeast-1:123456789012:gateway/gw-other",
                "name": aws_workflow.GATEWAY_NAME,
                "roleArn": "arn:aws:iam::123456789012:role/other",
                "protocolType": "MCP",
                "authorizerType": "AWS_IAM",
            }

    with pytest.raises(RuntimeError, match="Recovered Gateway does not match"):
        aws_workflow._recover_gateway_from_summary(Control(), {"gatewayId": "gw-example"}, "arn:aws:iam::123456789012:role/expected")


def test_create_function_accepts_direct_sdk_response_shape():
    response = {
        "FunctionArn": "arn:aws:lambda:ap-northeast-1:111122223333:function:udemy-agentcore-s04-cw-reader",
        "FunctionName": "udemy-agentcore-s04-cw-reader",
        "Role": "arn:aws:iam::111122223333:role/udemy-agentcore-s04-cw-reader-role",
        "Runtime": "python3.13",
        "Handler": "cloudwatch_reader.lambda_handler",
    }

    assert aws_workflow._created_lambda_configuration(response) is response
    assert "Configuration" not in response


def test_create_function_rejects_response_without_arn():
    with pytest.raises(RuntimeError, match="missing FunctionArn"):
        aws_workflow._created_lambda_configuration({"FunctionName": "unverified"})


@pytest.mark.parametrize(
    ("gateway_url", "expected"),
    [
        ("https://example.gateway", "https://example.gateway/mcp"),
        ("https://example.gateway/mcp", "https://example.gateway/mcp"),
        ("https://example.gateway/mcp/", "https://example.gateway/mcp"),
    ],
)
def test_mcp_endpoint_url_adds_one_suffix(gateway_url, expected):
    assert aws_workflow._mcp_endpoint_url(gateway_url) == expected


def test_create_function_client_error_records_sanitized_diagnostic(monkeypatch, capsys):
    from botocore.exceptions import ClientError

    state = {"operations": {"lambda": {"status": "requested"}}}
    monkeypatch.setattr(aws_workflow, "_save", lambda _state: None)
    exc = ClientError(
        {
            "Error": {
                "Code": "InvalidParameterValueException",
                "Message": "Function role 123456789012 at arn:aws:lambda:ap-northeast-1:123456789012:function:demo cannot be used.",
            },
            "ResponseMetadata": {"RequestId": "request-123"},
        },
        "CreateFunction",
    )

    aws_workflow._record_client_error(state, "lambda", "lambda.CreateFunction", exc)

    diagnostic = state["operations"]["lambda"]["error"]
    assert state["operations"]["lambda"]["status"] == "failed"
    assert diagnostic["operation"] == "lambda.CreateFunction"
    assert diagnostic["code"] == "InvalidParameterValueException"
    assert diagnostic["request_id"] == "request-123"
    assert "123456789012" not in diagnostic["message"]
    assert "arn:aws:" not in diagnostic["message"]
    printed = json.loads(capsys.readouterr().err)
    assert printed["aws_operation"] == "lambda.CreateFunction"
    assert printed["request_id"] == "request-123"


def test_sso_assumed_role_resolves_preserved_iam_path_and_checks_account():
    class Iam:
        def get_role(self, **kwargs):
            assert kwargs == {"RoleName": "AWSReservedSSO_Engineer_abc123"}
            return {"Role": {"Arn": "arn:aws:iam::111122223333:role/aws-reserved/sso.amazonaws.com/ap-northeast-1/AWSReservedSSO_Engineer_abc123"}}

    assert aws_workflow._effective_principal_arn(
        "arn:aws:sts::111122223333:assumed-role/AWSReservedSSO_Engineer_abc123/alice",
        "111122223333", Iam()) == "arn:aws:iam::111122223333:role/aws-reserved/sso.amazonaws.com/ap-northeast-1/AWSReservedSSO_Engineer_abc123"


def test_sso_role_resolution_rejects_wrong_account_or_role():
    class Iam:
        def get_role(self, **_kwargs):
            return {"Role": {"Arn": "arn:aws:iam::999900001111:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_Engineer_other"}}

    with pytest.raises(RuntimeError, match="exact caller account"):
        aws_workflow._effective_principal_arn(
            "arn:aws:sts::111122223333:assumed-role/AWSReservedSSO_Engineer_abc123/alice",
            "111122223333", Iam())


def test_runtime_snapshot_excludes_and_rejects_environment_values():
    live = {"agentRuntimeId": "runtime", "roleArn": "role", "environmentVariables": {},
            "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
            "secretExtraField": "must-not-be-saved"}
    snapshot = aws_workflow._safe_runtime_snapshot(live)
    assert snapshot["environmentVariables"] == {}
    assert "secretExtraField" not in snapshot
    with pytest.raises(RuntimeError, match="non-empty environment variables"):
        aws_workflow._safe_runtime_snapshot({**live, "environmentVariables": {"TOKEN": "do-not-print"}})


def test_runtime_lifecycle_is_preserved_and_out_of_bound_fails_closed():
    expected = {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800}
    assert aws_workflow._runtime_lifecycle({"lifecycleConfiguration": expected}) == expected
    with pytest.raises(RuntimeError, match="15-minute idle bound"):
        aws_workflow._runtime_lifecycle({"lifecycleConfiguration": {"idleRuntimeSessionTimeout": 1200, "maxLifetime": 28800}})


def test_positive_invoke_records_same_session_and_never_exceeds_two_calls(monkeypatch, capsys, tmp_path):
    state = {"account_id": "111122223333", "runtime_id": "runtime-from-s03-state", "runtime_arn": "arn:aws:bedrock-agentcore:ap-northeast-1:111122223333:runtime/runtime-from-s03-state",
             "sessions": [], "invocations": [], "positive_attempts": [],
             "runtime_update_request": {"lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800}},
             "first_invoke_started_epoch": 1, "stop_deadline_epoch": 9999999999}

    class Sts:
        def get_caller_identity(self):
            return {"Account": "111122223333"}

    class Control:
        def get_agent_runtime(self, **kwargs):
            assert kwargs == {"agentRuntimeId": "runtime-from-s03-state"}
            return {"status": "READY", "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
                    "environmentVariables": {"S04_GATEWAY_URL": "not logged or examined"}}

    runtime = object()
    clients = {"sts": Sts(), "bedrock-agentcore": runtime, "bedrock-agentcore-control": Control()}
    monkeypatch.setattr(aws_workflow, "_clients", lambda _profile: clients)
    monkeypatch.setattr(aws_workflow, "_state", lambda _clients: state)
    monkeypatch.setattr(aws_workflow, "_save", lambda value: None)
    from datetime import datetime, timezone
    current_seed = datetime.now(timezone.utc).replace(microsecond=0)
    seed_file = tmp_path / "seed.json"
    seed_file.write_text(json.dumps({
        "status": "metric_written", "region": aws_workflow.REGION, "service": "catalog-api",
        "request_id": "s04-demo-test", "timestamp": current_seed.isoformat().replace("+00:00", "Z"),
    }), encoding="utf-8")
    monkeypatch.setattr(aws_workflow, "SEED_STATE_PATH", seed_file)
    seen = []

    def fake_invoke(_runtime, _state, session, prompt):
        seen.append((session, prompt))
        data_type = "logs" if len(seen) == 1 else "metrics"
        start, end, _request_id = aws_workflow._positive_query_window(json.loads(seed_file.read_text(encoding="utf-8")))
        return {
            "invocation_id": f"invoke-{len(seen)}", "result": "synthetic",
            "trace": [{
                "name": "read_synthetic_cloudwatch",
                "arguments": {"data_type": data_type, "service": "catalog-api", "start_time": start,
                              "end_time": end, "max_results": 50},
                "result": {"ok": True},
            }],
        }

    monkeypatch.setattr(aws_workflow, "_invoke", fake_invoke)
    aws_workflow.invoke("111122223333", aws_workflow.REGION, "runtime-from-s03-state", None)
    assert len(seen) == 2
    assert seen[0][0] == seen[1][0]
    assert [item["status"] for item in state["positive_attempts"]] == ["succeeded", "succeeded"]
    assert len(state["invocations"]) == 2
    assert "service=catalog-api" in seen[0][1] and "start_time=" in seen[0][1] and "end_time=" in seen[0][1]
    assert "service=catalog-api" in seen[1][1] and "start_time=" in seen[1][1] and "end_time=" in seen[1][1]
    with pytest.raises(RuntimeError, match="Two positive call attempts"):
        aws_workflow.invoke("111122223333", aws_workflow.REGION, "runtime-from-s03-state", None)
    assert len(seen) == 2


def test_reconciled_failed_call_allows_exactly_one_fresh_validation_run(monkeypatch, capsys, tmp_path):
    from datetime import datetime, timezone

    old_session = "00000000-0000-4000-8000-000000000245-s04-positive-session-0001"
    original = {"status": "requested", "at_epoch": 100, "session_id": old_session, "step": 1}
    state = {
        "account_id": "111122223333", "runtime_id": "runtime-from-s03-state",
        "runtime_arn": "arn:aws:bedrock-agentcore:ap-northeast-1:111122223333:runtime/runtime-from-s03-state",
        "positive_session": old_session, "first_invoke_started_epoch": 100, "stop_deadline_epoch": 400,
        "sessions": [old_session], "invocations": [], "positive_attempts": [original.copy()],
        "stop_attempts": [{"session_id": old_session, "status": "stopped", "status_code": 200, "http_status_code": 200, "request_id": "stop-req"}],
        "reconciliations": [{"kind": "runtime_failure_reconciled", "session_id": old_session, "attempt_step": 1,
                             "invocation_id": "cfc87ec1-3b00-4e9d-8fbe-b11d66257c6b"}],
        "runtime_update_request": {"lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800}},
    }

    class Sts:
        def get_caller_identity(self):
            return {"Account": "111122223333"}

    class Control:
        def get_agent_runtime(self, **_kwargs):
            return {"status": "READY", "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800}}

    clients = {"sts": Sts(), "bedrock-agentcore": object(), "bedrock-agentcore-control": Control()}
    monkeypatch.setattr(aws_workflow, "_clients", lambda _profile: clients)
    monkeypatch.setattr(aws_workflow, "_state", lambda _clients: state)
    monkeypatch.setattr(aws_workflow, "_save", lambda _value: None)
    current_seed = datetime.now(timezone.utc).replace(microsecond=0)
    seed_file = tmp_path / "seed.json"
    seed_file.write_text(json.dumps({"status": "metric_written", "region": aws_workflow.REGION,
        "service": "catalog-api", "request_id": "s04-demo-test",
        "timestamp": current_seed.isoformat().replace("+00:00", "Z")}), encoding="utf-8")
    monkeypatch.setattr(aws_workflow, "SEED_STATE_PATH", seed_file)
    seen = []

    def fake_invoke(_runtime, _state, session, prompt):
        seen.append((session, prompt))
        data_type = "logs" if len(seen) == 1 else "metrics"
        start, end, _ = aws_workflow._positive_query_window(json.loads(seed_file.read_text(encoding="utf-8")))
        return {"invocation_id": f"recovery-{len(seen)}", "result": "synthetic", "trace": [{
            "name": "read_synthetic_cloudwatch",
            "arguments": {"data_type": data_type, "service": "catalog-api", "start_time": start,
                          "end_time": end, "max_results": 50},
            "result": {"ok": True},
        }]}

    monkeypatch.setattr(aws_workflow, "_invoke", fake_invoke)
    aws_workflow.invoke("111122223333", aws_workflow.REGION, "runtime-from-s03-state", None)
    assert len(seen) == 2 and seen[0][0] == seen[1][0] != old_session
    assert state["positive_attempts"][0] == original  # the failed original record is immutable
    assert len(state["validation_runs"]) == 2
    with pytest.raises(RuntimeError, match="Two positive call attempts"):
        aws_workflow.invoke("111122223333", aws_workflow.REGION, "runtime-from-s03-state", None)


def test_reconcile_invocation_requires_exact_failed_log_and_confirmed_stop(monkeypatch):
    old_session = "00000000-0000-4000-8000-000000000245-s04-positive-session-0001"
    invocation_id = "cfc87ec1-3b00-4e9d-8fbe-b11d66257c6b"
    state = {
        "runtime_id": "runtime-from-s03-state", "positive_session": old_session,
        "first_invoke_started_epoch": 100, "stop_deadline_epoch": 400,
        "sessions": [old_session], "positive_attempts": [{"status": "requested", "session_id": old_session, "step": 1}],
        "stop_attempts": [{"session_id": old_session, "status": "stopped", "status_code": 200,
                           "http_status_code": 200, "request_id": "stop-req", "at_epoch": 200}],
    }
    state["positive_attempts"][0]["at_epoch"] = 100
    event = {"eventId": "cw-event-1", "timestamp": 150000, "message": json.dumps({"event": "agent_invocation",
        "invocation_id": invocation_id, "status": 500, "tool_call_count": 0, "tool_names": [], "error_type": "RuntimeError"})}

    class Logs:
        def filter_log_events(self, **kwargs):
            assert kwargs["filterPattern"] == f'"{invocation_id}"'
            assert kwargs["startTime"] == 100000
            assert kwargs["endTime"] == 200000
            return {"events": [event]}

    clients = {"logs": Logs()}
    monkeypatch.setattr(aws_workflow, "_confirmed_state", lambda *_args: (clients, state))
    monkeypatch.setattr(aws_workflow, "_save", lambda _value: None)
    before = json.dumps(state["positive_attempts"], sort_keys=True)
    aws_workflow.reconcile_invocation("111122223333", aws_workflow.REGION,
                                      "runtime-from-s03-state", invocation_id, None)
    assert json.dumps(state["positive_attempts"], sort_keys=True) == before
    assert state["reconciliations"][0]["runtime_status"] == 500
    assert state["reconciliations"][0]["tool_call_count"] == 0


def test_reconcile_paginates_through_empty_page_with_next_token(monkeypatch):
    old_session = "00000000-0000-4000-8000-000000000245-s04-positive-session-0001"
    invocation_id = "cfc87ec1-3b00-4e9d-8fbe-b11d66257c6b"
    state = {
        "runtime_id": "runtime-from-s03-state",
        "positive_session": old_session,
        "sessions": [old_session],
        "positive_attempts": [{"status": "requested", "session_id": old_session, "step": 1, "at_epoch": 100}],
        "stop_attempts": [{"session_id": old_session, "status": "stopped", "status_code": 200,
                           "http_status_code": 200, "request_id": "stop-req", "at_epoch": 200}],
    }
    event = {"eventId": "cw-event-1", "timestamp": 150000, "message": json.dumps({"event": "agent_invocation",
        "invocation_id": invocation_id, "status": 500, "tool_call_count": 0, "tool_names": [], "error_type": "RuntimeError"})}
    calls = []

    class Logs:
        def filter_log_events(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                assert "nextToken" not in kwargs
                return {"events": [event], "nextToken": "page-2"}
            assert kwargs["nextToken"] == "page-2"
            return {"events": []}

    monkeypatch.setattr(aws_workflow, "_confirmed_state", lambda *_args: ({"logs": Logs()}, state))
    monkeypatch.setattr(aws_workflow, "_save", lambda _value: None)
    aws_workflow.reconcile_invocation("111122223333", aws_workflow.REGION,
                                      "runtime-from-s03-state", invocation_id, None)
    assert len(calls) == 2
    assert all(call["logGroupName"] == calls[0]["logGroupName"] for call in calls)
    assert all(call["filterPattern"] == f'"{invocation_id}"' for call in calls)
    assert all(call["startTime"] == 100000 and call["endTime"] == 200000 for call in calls)
    assert state["reconciliations"][0]["invocation_id"] == invocation_id


@pytest.mark.parametrize("pages, error", [
    ([{"events": [], "nextToken": "repeat"}, {"events": [], "nextToken": "repeat"}], "repeated"),
    ([{"events": [], "nextToken": ""}], "invalid token"),
    ([{"events": [], "nextToken": f"page-{index}"} for index in range(1, 11)], "ten-page"),
])
def test_reconcile_fails_closed_on_repeated_token_or_page_cap(monkeypatch, pages, error):
    old_session = "00000000-0000-4000-8000-000000000245-s04-positive-session-0001"
    invocation_id = "cfc87ec1-3b00-4e9d-8fbe-b11d66257c6b"
    state = {
        "runtime_id": "runtime-from-s03-state", "sessions": [old_session],
        "positive_attempts": [{"status": "requested", "session_id": old_session, "step": 1, "at_epoch": 100}],
        "stop_attempts": [{"session_id": old_session, "status": "stopped", "status_code": 200,
                           "http_status_code": 200, "request_id": "stop-req", "at_epoch": 200}],
    }
    calls = []

    class Logs:
        def filter_log_events(self, **kwargs):
            calls.append(kwargs)
            return pages[min(len(calls) - 1, len(pages) - 1)]

    monkeypatch.setattr(aws_workflow, "_confirmed_state", lambda *_args: ({"logs": Logs()}, state))
    monkeypatch.setattr(aws_workflow, "_save", lambda _value: None)
    with pytest.raises(RuntimeError, match=error):
        aws_workflow.reconcile_invocation("111122223333", aws_workflow.REGION,
                                          "runtime-from-s03-state", invocation_id, None)
    assert state.get("reconciliations", []) == []
    assert len(calls) <= 10


def test_reconcile_rejects_multiple_exact_events_and_mixed_foreign_window(monkeypatch):
    old_session = "00000000-0000-4000-8000-000000000245-s04-positive-session-0001"
    invocation_id = "cfc87ec1-3b00-4e9d-8fbe-b11d66257c6b"
    state = {
        "runtime_id": "runtime-from-s03-state", "sessions": [old_session],
        "positive_attempts": [{"status": "requested", "session_id": old_session, "step": 1, "at_epoch": 100}],
        "stop_attempts": [{"session_id": old_session, "status": "stopped", "status_code": 200,
                           "http_status_code": 200, "request_id": "stop-req", "at_epoch": 200}],
    }
    good_doc = {"event": "agent_invocation", "invocation_id": invocation_id, "status": 500,
                "tool_call_count": 0, "tool_names": [], "error_type": "RuntimeError"}
    foreign = {"eventId": "foreign", "timestamp": 200001, "message": json.dumps(good_doc)}
    first = {"eventId": "event-1", "timestamp": 150000, "message": json.dumps(good_doc)}
    second = {"eventId": "event-2", "timestamp": 151000, "message": json.dumps(good_doc)}

    class Logs:
        def __init__(self, pages): self.pages = iter(pages)
        def filter_log_events(self, **_kwargs): return next(self.pages)

    monkeypatch.setattr(aws_workflow, "_save", lambda _value: None)
    monkeypatch.setattr(aws_workflow, "_confirmed_state", lambda *_args: ({"logs": Logs([{"events": [first, foreign]}])}, state))
    with pytest.raises(RuntimeError, match="outside the recorded attempt-to-stop window"):
        aws_workflow.reconcile_invocation("111122223333", aws_workflow.REGION,
                                          "runtime-from-s03-state", invocation_id, None)
    monkeypatch.setattr(aws_workflow, "_confirmed_state", lambda *_args: ({"logs": Logs([
        {"events": [first], "nextToken": "page-2"}, {"events": [second]}])}, state))
    with pytest.raises(RuntimeError, match="conflicting exact events"):
        aws_workflow.reconcile_invocation("111122223333", aws_workflow.REGION,
                                          "runtime-from-s03-state", invocation_id, None)
    assert state.get("reconciliations", []) == []


def test_reconcile_rejects_exact_event_without_event_id(monkeypatch):
    old_session = "00000000-0000-4000-8000-000000000245-s04-positive-session-0001"
    invocation_id = "cfc87ec1-3b00-4e9d-8fbe-b11d66257c6b"
    state = {
        "runtime_id": "runtime-from-s03-state", "sessions": [old_session],
        "positive_attempts": [{"status": "requested", "session_id": old_session, "step": 1, "at_epoch": 100}],
        "stop_attempts": [{"session_id": old_session, "status": "stopped", "status_code": 200,
                           "http_status_code": 200, "request_id": "stop-req", "at_epoch": 200}],
    }
    event = {"timestamp": 150000, "message": json.dumps({"event": "agent_invocation",
        "invocation_id": invocation_id, "status": 500, "tool_call_count": 0, "tool_names": [], "error_type": "RuntimeError"})}

    class Logs:
        def filter_log_events(self, **_kwargs): return {"events": [event]}

    monkeypatch.setattr(aws_workflow, "_confirmed_state", lambda *_args: ({"logs": Logs()}, state))
    monkeypatch.setattr(aws_workflow, "_save", lambda _value: None)
    with pytest.raises(RuntimeError, match="lacks its CloudWatch event ID"):
        aws_workflow.reconcile_invocation("111122223333", aws_workflow.REGION,
                                          "runtime-from-s03-state", invocation_id, None)
    assert state.get("reconciliations", []) == []


def test_reconcile_requires_nonempty_stop_request_id(monkeypatch):
    old_session = "00000000-0000-4000-8000-000000000245-s04-positive-session-0001"
    state = {
        "runtime_id": "runtime-from-s03-state", "sessions": [old_session],
        "positive_attempts": [{"status": "requested", "session_id": old_session, "step": 1, "at_epoch": 100}],
        "stop_attempts": [{"session_id": old_session, "status": "stopped", "status_code": 200,
                           "http_status_code": 200, "request_id": "  ", "at_epoch": 200}],
    }

    monkeypatch.setattr(aws_workflow, "_confirmed_state", lambda *_args: ({}, state))
    with pytest.raises(RuntimeError, match="explicit HTTP 200/statusCode 200 stop result"):
        aws_workflow.reconcile_invocation("111122223333", aws_workflow.REGION,
                                          "runtime-from-s03-state", "cfc87ec1-3b00-4e9d-8fbe-b11d66257c6b", None)


def test_invoke_fails_before_request_if_live_lifecycle_changed(monkeypatch, tmp_path):
    state = {"account_id": "111122223333", "runtime_id": "runtime-from-s03-state", "runtime_arn": "arn:aws:bedrock-agentcore:ap-northeast-1:111122223333:runtime/runtime-from-s03-state",
             "sessions": [], "invocations": [], "positive_attempts": [],
             "runtime_update_request": {"lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800}},
             "first_invoke_started_epoch": 1, "stop_deadline_epoch": 9999999999}

    class Sts:
        def get_caller_identity(self):
            return {"Account": "111122223333"}

    class Control:
        def get_agent_runtime(self, **_kwargs):
            return {"status": "READY", "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 600, "maxLifetime": 28800}}

    clients = {"sts": Sts(), "bedrock-agentcore": object(), "bedrock-agentcore-control": Control()}
    monkeypatch.setattr(aws_workflow, "_clients", lambda _profile: clients)
    monkeypatch.setattr(aws_workflow, "_state", lambda _clients: state)
    monkeypatch.setattr(aws_workflow, "_save", lambda _value: None)
    from datetime import datetime, timezone
    current_seed = datetime.now(timezone.utc).replace(microsecond=0)
    seed_file = tmp_path / "seed.json"
    seed_file.write_text(json.dumps({"status": "metric_written", "region": aws_workflow.REGION,
        "service": "catalog-api", "request_id": "s04-demo-test",
        "timestamp": current_seed.isoformat().replace("+00:00", "Z")}), encoding="utf-8")
    monkeypatch.setattr(aws_workflow, "SEED_STATE_PATH", seed_file)
    with pytest.raises(RuntimeError, match="differs from the saved S04 deployment"):
        aws_workflow.invoke("111122223333", aws_workflow.REGION, "runtime-from-s03-state", None)
    assert state["positive_attempts"] == []


def test_stop_only_uses_saved_sessions(monkeypatch):
    state = {"account_id": "111122223333", "runtime_id": "runtime-from-s03-state", "runtime_arn": "arn:aws:bedrock-agentcore:ap-northeast-1:111122223333:runtime/runtime-from-s03-state",
             "sessions": ["session-exact"]}

    class Sts:
        def get_caller_identity(self):
            return {"Account": "111122223333"}

    class Runtime:
        def stop_runtime_session(self, **kwargs):
            assert kwargs["runtimeSessionId"] == "session-exact"
            assert kwargs["agentRuntimeArn"].endswith("runtime-from-s03-state")
            return {"statusCode": 200, "ResponseMetadata": {"HTTPStatusCode": 200, "RequestId": "request-123"}}

    monkeypatch.setattr(aws_workflow, "_clients", lambda _profile: {"sts": Sts(), "bedrock-agentcore": Runtime()})
    monkeypatch.setattr(aws_workflow, "_state", lambda _clients: state)
    monkeypatch.setattr(aws_workflow, "_save", lambda value: None)
    aws_workflow.stop("111122223333", aws_workflow.REGION, "runtime-from-s03-state", None)
    assert state["sessions"] == ["session-exact"]
    assert "stop_requested_at_epoch" in state
    assert state["stop_attempts"][0]["status"] == "stopped"
    assert state["stop_attempts"][0]["status_code"] == 200
    assert state["stop_attempts"][0]["http_status_code"] == 200
    assert state["stop_attempts"][0]["request_id"] == "request-123"
    assert aws_workflow._session_stop_confirmed(state, "session-exact")


def test_stop_is_idempotent_and_restore_gate_accepts_confirmed_session(monkeypatch):
    state = {
        "account_id": "111122223333", "runtime_id": "runtime-from-s03-state",
        "runtime_arn": "arn:aws:bedrock-agentcore:ap-northeast-1:111122223333:runtime/runtime-from-s03-state",
        "sessions": ["session-exact"],
        "stop_attempts": [{"session_id": "session-exact", "status": "stopped", "http_status_code": 200}],
    }
    calls = []

    class Sts:
        def get_caller_identity(self):
            return {"Account": "111122223333"}

    class Runtime:
        def stop_runtime_session(self, **_kwargs):
            calls.append("stop")
            raise AssertionError("a confirmed stop must not be replayed")

    monkeypatch.setattr(aws_workflow, "_clients", lambda _profile: {"sts": Sts(), "bedrock-agentcore": Runtime()})
    monkeypatch.setattr(aws_workflow, "_state", lambda _clients: state)
    monkeypatch.setattr(aws_workflow, "_save", lambda _value: None)
    aws_workflow.stop("111122223333", aws_workflow.REGION, "runtime-from-s03-state", None)
    assert calls == []
    assert state["sessions"] == ["session-exact"]  # historical ledger remains intact
    assert aws_workflow._session_stop_confirmed(state, "session-exact")

    prior = {
        "agentRuntimeArtifact": {"codeConfiguration": {"code": {"s3": {"bucket": "bucket", "key": "s03.zip"}}, "runtime": "PYTHON_3_13", "entryPoint": ["python", "runtime_app.py"]}},
        "roleArn": "arn:aws:iam::111122223333:role/udemy-agentcore-s03-runtime",
        "platformVersion": "V2", "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"}, "environmentVariables": {},
        "metadataConfiguration": {"requireMMDSV2": True},
        "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
    }
    state["prior_runtime"] = prior

    class Control:
        def update_agent_runtime(self, **kwargs):
            calls.append("restore")
            assert kwargs["agentRuntimeArtifact"] == prior["agentRuntimeArtifact"]
            return {"agentRuntimeVersion": 4}

    clients = {"bedrock-agentcore-control": Control()}
    monkeypatch.setattr(aws_workflow, "_confirmed_state", lambda *_args: (clients, state))
    monkeypatch.setattr(aws_workflow, "_wait_ready", lambda *_args: {"status": "READY"})
    aws_workflow.restore("111122223333", aws_workflow.REGION, "runtime-from-s03-state", None)
    assert calls == ["restore"]
    assert state["phase"] == "restored"


def test_stop_does_not_accept_not_found_for_inflight_session(monkeypatch):
    from botocore.exceptions import ClientError

    state = {"account_id": "111122223333", "runtime_id": "runtime-from-s03-state", "runtime_arn": "arn:aws:bedrock-agentcore:ap-northeast-1:111122223333:runtime/runtime-from-s03-state",
             "sessions": ["session-exact"], "positive_attempts": [{"session_id": "session-exact", "status": "requested"}]}

    class Sts:
        def get_caller_identity(self):
            return {"Account": "111122223333"}

    class Runtime:
        def stop_runtime_session(self, **_kwargs):
            raise ClientError({"Error": {"Code": "ResourceNotFoundException"}, "ResponseMetadata": {"RequestId": "request-404"}}, "StopRuntimeSession")

    monkeypatch.setattr(aws_workflow, "_clients", lambda _profile: {"sts": Sts(), "bedrock-agentcore": Runtime()})
    monkeypatch.setattr(aws_workflow, "_state", lambda _clients: state)
    monkeypatch.setattr(aws_workflow, "_save", lambda _value: None)
    with pytest.raises(RuntimeError, match="no successful stop was confirmed"):
        aws_workflow.stop("111122223333", aws_workflow.REGION, "runtime-from-s03-state", None)
    assert state["stop_attempts"][0]["status"] == "not_found_unconfirmed"
