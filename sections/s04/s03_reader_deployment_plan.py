"""Render a bounded, non-executing plan for isolated S04 reader resources.

This module intentionally has no AWS client and cannot deploy anything. It emits
the exact request shapes for action-time review and separate manual execution.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

REGION = "ap-northeast-1"
ACCOUNT_RE = re.compile(r"^[0-9]{12}$")
RUN_RE = re.compile(r"^[a-zA-Z0-9_-]{1,40}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
S03_RUNTIME_ID = "udemy_agentcore_s03-RsrJEUHMQ7"
S03_LOG_GROUP = f"/aws/bedrock-agentcore/runtimes/{S03_RUNTIME_ID}-DEFAULT"
RUNTIME_NAME = "udemy_agentcore_s04r245r1"
GATEWAY_NAME = "udemy-agentcore-s04-245-log-reader-r1"
TARGET_NAME = "udemy-agentcore-s04-245-s03-target-r1"
LAMBDA_NAME = "udemy-agentcore-s04-245-s03-reader-r1"
LAMBDA_ROLE = "udemy-agentcore-s04-245-s03-reader-exec-r1"
GATEWAY_ROLE = "udemy-agentcore-s04-245-gateway-role-r1"
RUNTIME_ROLE = "udemy-agentcore-s04-245-runtime-role-r1"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _role_trust(service: str, account: str, source_arn: str | None = None) -> dict[str, Any]:
    condition: dict[str, Any] = {"StringEquals": {"aws:SourceAccount": account}}
    if source_arn:
        condition["ArnLike"] = {"aws:SourceArn": source_arn}
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow", "Principal": {"Service": service},
            "Action": "sts:AssumeRole", "Condition": condition,
        }],
    }


def _lambda_trust() -> dict[str, Any]:
    # Lambda's documented execution-role trust uses only the service principal;
    # SourceAccount/SourceArn are not added without Lambda-specific support evidence.
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }],
    }


def _policy(statement: dict[str, Any]) -> str:
    return json.dumps({"Version": "2012-10-17", "Statement": [statement]}, separators=(",", ":"))


def build_plan(*, account: str, run_id: str, bucket: str,
               lambda_zip: Path, runtime_zip: Path) -> dict[str, Any]:
    """Return exact request shapes; performing no network or filesystem writes."""
    if not ACCOUNT_RE.fullmatch(account):
        raise ValueError("account must be a 12-digit AWS account ID")
    if not RUN_RE.fullmatch(run_id):
        raise ValueError("run_id must contain 1-40 letters, digits, underscore, or hyphen")
    if not bucket or "/" in bucket:
        raise ValueError("bucket must be one exact S3 bucket name")
    for artifact in (lambda_zip, runtime_zip):
        if not artifact.is_file():
            raise FileNotFoundError(artifact)
    lambda_hash, runtime_hash = sha256_file(lambda_zip), sha256_file(runtime_zip)
    if not SHA_RE.fullmatch(lambda_hash) or not SHA_RE.fullmatch(runtime_hash):
        raise ValueError("could not calculate package SHA-256")

    gateway_arn = f"arn:aws:bedrock-agentcore:{REGION}:{account}:gateway/*"
    lambda_arn = f"arn:aws:lambda:{REGION}:{account}:function:{LAMBDA_NAME}"
    group_arn = f"arn:aws:logs:{REGION}:{account}:log-group:{S03_LOG_GROUP}:*"
    lambda_group_arn = f"arn:aws:logs:{REGION}:{account}:log-group:/aws/lambda/{LAMBDA_NAME}:*"
    runtime_group_arn = f"arn:aws:logs:{REGION}:{account}:log-group:/aws/bedrock-agentcore/runtimes/{RUNTIME_NAME}-*"
    runtime_stream_arn = f"{runtime_group_arn}:log-stream:*"
    model_arn = f"arn:aws:bedrock:{REGION}::foundation-model/amazon.nova-lite-v1:0"
    prefix = f"s04/issue245-s03-reader/{run_id}"
    lambda_key = f"{prefix}/lambda-{lambda_hash}.zip"
    runtime_key = f"{prefix}/runtime-{runtime_hash}.zip"
    client_token = hashlib.sha256(f"issue245|{account}|{run_id}|create-runtime".encode()).hexdigest()

    lambda_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["logs:FilterLogEvents"], "Resource": group_arn},
            {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"], "Resource": lambda_group_arn},
        ],
    }
    gateway_policy = {
        "Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Action": ["lambda:InvokeFunction"], "Resource": lambda_arn,
        }],
    }
    runtime_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["bedrock-agentcore:InvokeGateway"],
             "Resource": "<exact gatewayArn returned by successful create_gateway response>"},
            {"Effect": "Allow", "Action": ["bedrock:InvokeModel"], "Resource": model_arn},
            {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:DescribeLogStreams"], "Resource": runtime_group_arn},
            {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"], "Resource": runtime_stream_arn},
        ],
    }
    lambda_schema = {
        "inlinePayload": [{
            "name": "read_s03_invocation",
            "description": "Read one bounded existing S03 invocation summary by its exact UUID and timestamp.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "target_invocation_id": {"type": "string"},
                    "target_timestamp_ms": {"type": "integer"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 5},
                },
                "required": ["target_invocation_id", "target_timestamp_ms", "max_results"],
            },
        }],
    }
    writes = [
        {"service": "s3", "operation": "put_object", "request": {
            "Bucket": bucket, "Key": lambda_key, "Body": f"<read {lambda_zip}>", "ExpectedBucketOwner": account,
            "IfNoneMatch": "*", "Metadata": {"sha256": lambda_hash},
        }},
        {"service": "s3", "operation": "put_object", "request": {
            "Bucket": bucket, "Key": runtime_key, "Body": f"<read {runtime_zip}>", "ExpectedBucketOwner": account,
            "IfNoneMatch": "*", "Metadata": {"sha256": runtime_hash},
        }},
        {"service": "logs", "operation": "create_log_group", "request": {"logGroupName": f"/aws/lambda/{LAMBDA_NAME}"}},
        {"service": "logs", "operation": "put_retention_policy", "request": {
            "logGroupName": f"/aws/lambda/{LAMBDA_NAME}", "retentionInDays": 7,
        }},
        {"service": "iam", "operation": "create_role", "request": {
            "RoleName": LAMBDA_ROLE, "AssumeRolePolicyDocument": json.dumps(_lambda_trust()),
            "Description": f"Dedicated S04 S03-log reader Lambda execution role for run {run_id}",
        }},
        {"service": "iam", "operation": "put_role_policy", "request": {
            "RoleName": LAMBDA_ROLE, "PolicyName": "S03ExactGroupFilterOnly", "PolicyDocument": json.dumps(lambda_policy),
        }},
        {"service": "lambda", "operation": "create_function", "request": {
            "FunctionName": LAMBDA_NAME, "Runtime": "python3.13", "Role": f"arn:aws:iam::{account}:role/{LAMBDA_ROLE}",
            "Handler": "s03_invocation_reader.lambda_handler", "Code": {"S3Bucket": bucket, "S3Key": lambda_key},
            "Description": f"Issue 245 isolated read-only S03 log reader run {run_id}",
            "Timeout": 30, "MemorySize": 512, "Publish": False, "Architectures": ["x86_64"],
        }},
        {"service": "iam", "operation": "create_role", "request": {
            "RoleName": GATEWAY_ROLE,
            "AssumeRolePolicyDocument": json.dumps(_role_trust("bedrock-agentcore.amazonaws.com", account, gateway_arn)),
            "Description": f"Dedicated S04 reader Gateway role for run {run_id}; tighten SourceArn immediately after create_gateway",
        }},
        {"service": "iam", "operation": "put_role_policy", "request": {
            "RoleName": GATEWAY_ROLE, "PolicyName": "InvokeOnlyNewReader", "PolicyDocument": json.dumps(gateway_policy),
        }},
        {"service": "bedrock-agentcore-control", "operation": "create_gateway", "request": {
            "name": GATEWAY_NAME, "roleArn": f"arn:aws:iam::{account}:role/{GATEWAY_ROLE}",
            "protocolType": "MCP", "authorizerType": "AWS_IAM",
            "description": f"Issue 245 isolated S03 log reader run {run_id}",
        }},
        {"service": "iam", "operation": "update_assume_role_policy", "request": {
            "RoleName": GATEWAY_ROLE,
            "PolicyDocument": json.dumps(_role_trust(
                "bedrock-agentcore.amazonaws.com", account,
                "<exact gatewayArn returned by successful create_gateway response>")),
        }},
        {"service": "bedrock-agentcore-control", "operation": "create_gateway_target", "request": {
            "gatewayIdentifier": "<generated gateway id from successful create_gateway response>",
            "name": TARGET_NAME,
            "targetConfiguration": {"mcp": {"lambda": {"lambdaArn": lambda_arn, "toolSchema": lambda_schema}}},
            "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
        }},
        {"service": "iam", "operation": "create_role", "request": {
            "RoleName": RUNTIME_ROLE,
            "AssumeRolePolicyDocument": json.dumps(_role_trust(
                "bedrock-agentcore.amazonaws.com", account,
                f"arn:aws:bedrock-agentcore:{REGION}:{account}:runtime/{RUNTIME_NAME}-*")),
            "Description": f"Dedicated S04 reader Runtime role for run {run_id}; tighten SourceArn immediately after create_agent_runtime",
        }},
        {"service": "iam", "operation": "put_role_policy", "request": {
            "RoleName": RUNTIME_ROLE, "PolicyName": "InvokeOnlyNewGatewayAndNovaLite", "PolicyDocument": json.dumps(runtime_policy),
        }},
        {"service": "bedrock-agentcore-control", "operation": "create_agent_runtime", "request": {
            "agentRuntimeName": RUNTIME_NAME, "clientToken": client_token,
            "roleArn": f"arn:aws:iam::{account}:role/{RUNTIME_ROLE}",
            "description": f"Issue 245 isolated S03 invocation reader run {run_id}",
            "agentRuntimeArtifact": {"codeConfiguration": {"code": {"s3": {"bucket": bucket, "prefix": runtime_key}},
                "runtime": "PYTHON_3_13", "entryPoint": ["runtime_app.py"]}},
            "platformVersion": "V2", "networkConfiguration": {"networkMode": "PUBLIC"},
            "protocolConfiguration": {"serverProtocol": "HTTP"},
            "environmentVariables": {
                "S04_GATEWAY_URL": "<gateway URL from successful create_gateway response>",
                "S04_GATEWAY_TOOL_NAME": "<exact single exposed MCP tool name from target/list response>",
            },
            "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
        }},
        {"service": "iam", "operation": "update_assume_role_policy", "request": {
            "RoleName": RUNTIME_ROLE,
            "PolicyDocument": json.dumps(_role_trust(
                "bedrock-agentcore.amazonaws.com", account,
                "<exact runtime ARN returned by successful create_agent_runtime response>")),
        }},
        {"service": "logs", "operation": "create_log_group", "conditional_after_runtime_id": True,
         "request": {"logGroupName": "<exact /aws/bedrock-agentcore/runtimes/{agentRuntimeId}-DEFAULT from successful runtime response>"},
         "note": "first describe exact name; skip create if service already created it"},
        {"service": "logs", "operation": "put_retention_policy", "conditional_after_runtime_id": True,
         "request": {"logGroupName": "<same exact new runtime log group>", "retentionInDays": 7}},
    ]
    return {
        "mode": "PLAN_ONLY_NO_AWS_CLIENT_NO_EXECUTION",
        "account": account, "region": REGION, "run_id": run_id,
        "expected_unchanged": {"s03_runtime_id": S03_RUNTIME_ID, "s03_log_group": S03_LOG_GROUP,
                               "existing_s04_synthetic_resources": "all unchanged"},
        "artifacts": {
            "lambda": {"path": str(lambda_zip), "sha256": lambda_hash, "bucket": bucket, "key": lambda_key},
            "runtime": {"path": str(runtime_zip), "sha256": runtime_hash, "bucket": bucket, "key": runtime_key},
        },
        "read_only_preflight": [
            {"service": "sts", "operation": "get_caller_identity", "expected_account": account},
            {"service": "bedrock-agentcore-control", "operation": "get_agent_runtime", "request": {"agentRuntimeId": S03_RUNTIME_ID},
             "expected_unchanged_log_group": S03_LOG_GROUP},
            {"service": "logs", "operation": "describe_log_groups", "request": {"logGroupNamePrefix": S03_LOG_GROUP}},
            {"service": "s3", "operation": "head_object", "bucket": bucket, "keys_must_be_absent": [lambda_key, runtime_key]},
            {"service": "iam", "operation": "get_role", "requests": [{"RoleName": name} for name in [LAMBDA_ROLE, GATEWAY_ROLE, RUNTIME_ROLE]],
             "note": "all must return ResourceNotFound before creating anything"},
            {"service": "lambda", "operation": "get_function", "requests": [{"FunctionName": LAMBDA_NAME}],
             "note": "must return ResourceNotFound before creating anything"},
            {"service": "logs", "operation": "describe_log_groups", "request": {"logGroupNamePrefix": f"/aws/lambda/{LAMBDA_NAME}"},
             "note": "must return no exact dedicated Lambda log group"},
            {"service": "bedrock-agentcore-control", "operation": "list_gateways", "name_must_be_absent": GATEWAY_NAME},
            {"service": "bedrock-agentcore-control", "operation": "list_agent_runtimes", "name_must_be_absent": RUNTIME_NAME},
            {"service": "iam", "operation": "get_role", "request": {
                "RoleName": "<derive exact IAM role name from get_caller_identity assumed-role ARN; do not pass STS assumed-role ARN to PolicySourceArn>",
            }, "note": "read-only; use returned Role.Arn as PolicySourceArn for simulation"},
            {"service": "iam", "operation": "simulate_principal_policy", "requests": [
                {"PolicySourceArn": "<Role.Arn returned by the preceding exact get_role>", "ActionNames": ["s3:PutObject"],
                 "ResourceArns": [f"arn:aws:s3:::{bucket}/{lambda_key}", f"arn:aws:s3:::{bucket}/{runtime_key}"]},
                {"PolicySourceArn": "<same Role.Arn>", "ActionNames": ["s3:GetObject"],
                 "ResourceArns": [f"arn:aws:s3:::{bucket}/{lambda_key}", f"arn:aws:s3:::{bucket}/{runtime_key}"]},
                {"PolicySourceArn": "<same Role.Arn>", "ActionNames": ["iam:PassRole"],
                 "ResourceArns": [f"arn:aws:iam::{account}:role/{LAMBDA_ROLE}", f"arn:aws:iam::{account}:role/{GATEWAY_ROLE}", f"arn:aws:iam::{account}:role/{RUNTIME_ROLE}"]},
            ], "note": "read-only advisory; separately inspect exact live authorization for remaining create/update APIs; simulator does not prove authorization"},
        ],
        "writes": writes,
        "postflight_read_only": [
            "Read back every new resource by returned exact ID/ARN; verify READY, hashes/keys, exact role policies and trust, 7-day dedicated log retention, one Gateway target/tool and exact Runtime env/lifecycle.",
            "Re-read S03 Runtime v16, its role/environment, current S04 synthetic Gateway/target/Lambda and shared log group; compare with recorded before snapshot. Stop immediately if any differs.",
        ],
        "execution_safety": [
            "Root must approve the exact fresh preflight, package hashes, generated request list, cost and rollback at action time before any write or model invocation.",
            "The Gateway role is initially trusted by the same-account AgentCore Gateway service with SourceArn limited to gateway/* because the service generates the Gateway ARN. It is attached only to the new Gateway; tighten SourceArn to the returned exact gateway ARN immediately after creation and before adding a target. Runtime trust is similarly tightened from the exact name prefix to the returned Runtime ARN before model invocation. If either update cannot be verified, stop; do not use the broad interim trust.",
            "No automatic retry of a write with an uncertain outcome. Save each successful response ID/ARN before the next step. If response is lost or ambiguous, do read-only reconciliation by exact name and request metadata; never replay create/put blindly.",
            "Resume only from a saved successful step after read-only verification. If no exact unique resource can be identified, stop and request review; do not adopt or mutate an existing resource.",
            "This plan does not execute writes, invoke a Runtime/model, update S03/shared resources, or provide cleanup authorization.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--lambda-zip", required=True, type=Path)
    parser.add_argument("--runtime-zip", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(build_plan(account=args.account, run_id=args.run_id, bucket=args.bucket,
                                lambda_zip=args.lambda_zip, runtime_zip=args.runtime_zip), indent=2))


if __name__ == "__main__":
    main()
