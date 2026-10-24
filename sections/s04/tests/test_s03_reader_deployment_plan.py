from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

S04_DIR = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("s03_reader_deployment_plan", S04_DIR / "s03_reader_deployment_plan.py")
assert SPEC is not None and SPEC.loader is not None
plan_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = plan_module
SPEC.loader.exec_module(plan_module)


def _plan(tmp_path: Path) -> dict:
    lambda_zip = tmp_path / "lambda.zip"
    runtime_zip = tmp_path / "runtime.zip"
    lambda_zip.write_bytes(b"lambda test package")
    runtime_zip.write_bytes(b"runtime test package")
    return plan_module.build_plan(
        account="668602146132", run_id="r-test", bucket="artifact-bucket",
        lambda_zip=lambda_zip, runtime_zip=runtime_zip,
    )


def test_plan_is_explicitly_nonexecuting_and_uses_only_new_resources(tmp_path):
    plan = _plan(tmp_path)
    assert plan["mode"] == "PLAN_ONLY_NO_AWS_CLIENT_NO_EXECUTION"
    assert plan["region"] == "ap-northeast-1"
    assert plan["expected_unchanged"]["s03_runtime_id"] == "udemy_agentcore_s03-RsrJEUHMQ7"
    assert plan["expected_unchanged"]["existing_s04_synthetic_resources"] == "all unchanged"
    assert all("synthetic" not in json.dumps(item).lower() for item in plan["writes"])
    assert len([item for item in plan["writes"] if item["service"] == "lambda" and item["operation"] == "create_function"]) == 1
    assert len([item for item in plan["writes"] if item["operation"] == "create_agent_runtime"]) == 1


def test_plan_minimizes_lambda_and_runtime_permissions(tmp_path):
    plan = _plan(tmp_path)
    lambda_statement = next(
        json.loads(item["request"]["PolicyDocument"])["Statement"]
        for item in plan["writes"]
        if item["service"] == "iam" and item["request"].get("RoleName") == plan_module.LAMBDA_ROLE
        and item["operation"] == "put_role_policy"
    )
    filter_statement = next(item for item in lambda_statement if "logs:FilterLogEvents" in item["Action"])
    assert filter_statement["Resource"] == (
        "arn:aws:logs:ap-northeast-1:668602146132:log-group:"
        "/aws/bedrock-agentcore/runtimes/udemy_agentcore_s03-RsrJEUHMQ7-DEFAULT:*"
    )
    assert "logs:Describe" not in json.dumps(lambda_statement)
    runtime_req = next(item["request"] for item in plan["writes"] if item["operation"] == "create_agent_runtime")
    assert runtime_req["lifecycleConfiguration"] == {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800}
    assert len(runtime_req["clientToken"]) >= 33
    assert runtime_req["environmentVariables"]["S04_GATEWAY_URL"].startswith("<gateway URL")
    runtime_policy = next(
        json.loads(item["request"]["PolicyDocument"])
        for item in plan["writes"]
        if item["operation"] == "put_role_policy" and item["request"].get("RoleName") == plan_module.RUNTIME_ROLE
    )
    log_statement = next(item for item in runtime_policy["Statement"] if "logs:CreateLogGroup" in item["Action"])
    assert set(log_statement["Action"]) == {
        "logs:CreateLogGroup", "logs:DescribeLogStreams",
    }
    assert log_statement["Resource"] == (
        "arn:aws:logs:ap-northeast-1:668602146132:log-group:"
        "/aws/bedrock-agentcore/runtimes/udemy_agentcore_s04r245r1-*"
    )
    assert "logs:PutResourcePolicy" not in json.dumps(runtime_policy)
    assert "logs:DescribeLogGroups" not in json.dumps(runtime_policy)
    assert all(item["Resource"] != "*" for item in runtime_policy["Statement"])


def test_lambda_trust_uses_documented_service_principal_only(tmp_path):
    plan = _plan(tmp_path)
    trust = next(
        json.loads(item["request"]["AssumeRolePolicyDocument"])
        for item in plan["writes"]
        if item["operation"] == "create_role" and item["request"].get("RoleName") == plan_module.LAMBDA_ROLE
    )
    assert trust == {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
                       "Action": "sts:AssumeRole"}],
    }


@pytest.mark.parametrize("account,run_id,bucket", [
    ("not-an-account", "r-test", "bucket"), ("668602146132", "../bad", "bucket"),
    ("668602146132", "r-test", "bucket/path"),
])
def test_plan_rejects_unsafe_identifiers(tmp_path, account, run_id, bucket):
    lambda_zip = tmp_path / "lambda.zip"
    runtime_zip = tmp_path / "runtime.zip"
    lambda_zip.write_bytes(b"lambda")
    runtime_zip.write_bytes(b"runtime")
    with pytest.raises(ValueError):
        plan_module.build_plan(account=account, run_id=run_id, bucket=bucket,
                               lambda_zip=lambda_zip, runtime_zip=runtime_zip)


def test_plan_refuses_missing_artifact(tmp_path):
    with pytest.raises(FileNotFoundError):
        plan_module.build_plan(account="668602146132", run_id="r-test", bucket="bucket",
                               lambda_zip=tmp_path / "missing.zip", runtime_zip=tmp_path / "runtime.zip")
