"""Create only the S08 training Runtime resources from one exact ARM64 ZIP."""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

REGION = "ap-northeast-1"
RUNTIME_NAME = "udemy_agentcore_s08"
ROLE_NAME = "udemy-agentcore-s08-runtime"
ENDPOINT_NAME = "s08-training"
OBJECT_PREFIX = "s08"
STATE_PATH = Path(__file__).resolve().parent / ".agentcore-s08-state.json"
SDK_CONFIG = Config(connect_timeout=5, read_timeout=45, retries={"mode": "standard", "total_max_attempts": 1})
S3_CONFIG = Config(connect_timeout=5, read_timeout=120, retries={"mode": "standard", "total_max_attempts": 1})
POLL_SECONDS = 10
READY_TIMEOUT_SECONDS = 20 * 60


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_expected_account(expected_account: str) -> None:
    if not expected_account.isdigit() or len(expected_account) != 12:
        raise RuntimeError("Expected account must be a 12-digit account ID; no AWS write attempted")


def _save_state(state: dict[str, Any]) -> None:
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary.replace(STATE_PATH)


def runtime_role_documents(account: str) -> tuple[dict[str, Any], dict[str, Any]]:
    runtime_arn_prefix = f"arn:aws:bedrock-agentcore:{REGION}:{account}:runtime/{RUNTIME_NAME}-*"
    log_group = f"arn:aws:logs:{REGION}:{account}:log-group:/aws/bedrock-agentcore/runtimes/{RUNTIME_NAME}-*"
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {"aws:SourceAccount": account},
                "ArnLike": {"aws:SourceArn": runtime_arn_prefix},
            },
        }],
    }
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "S08RuntimeLogGroup",
                "Effect": "Allow",
                "Action": ["logs:DescribeLogStreams", "logs:CreateLogGroup"],
                "Resource": log_group,
            },
            {
                "Sid": "S08DescribeLogGroupsInTokyo",
                "Effect": "Allow",
                "Action": "logs:DescribeLogGroups",
                "Resource": "*",
                "Condition": {"StringEquals": {"aws:RequestedRegion": REGION}},
            },
            {
                "Sid": "S08RuntimeLogStreams",
                "Effect": "Allow",
                "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": f"{log_group}:log-stream:*",
            },
            {
                "Sid": "S08NovaLiteOnly",
                "Effect": "Allow",
                "Action": "bedrock:InvokeModel",
                "Resource": f"arn:aws:bedrock:{REGION}::foundation-model/amazon.nova-lite-v1:0",
            },
        ],
    }
    return trust, policy


def create_request(bucket: str, key: str, role_arn: str, client_token: str) -> dict[str, Any]:
    return {
        "agentRuntimeName": RUNTIME_NAME,
        "description": "S08 synthetic prompt-version comparison",
        "roleArn": role_arn,
        "platformVersion": "V2",
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 60, "maxLifetime": 1800},
        "environmentVariables": {"S08_PROMPT_VARIANT": "baseline"},
        "agentRuntimeArtifact": {
            "codeConfiguration": {
                "code": {"s3": {"bucket": bucket, "prefix": key}},
                "entryPoint": ["runtime_app.py"],
                "runtime": "PYTHON_3_13",
            }
        },
        "tags": {"Course": "amazon-bedrock-agentcore-operations", "Section": "s08"},
        "clientToken": client_token,
    }


def caller_policy_document(account: str) -> dict[str, Any]:
    """Least-privilege caller policy for S08 setup and the planned L02/L03 operations."""
    runtime_arn = f"arn:aws:bedrock-agentcore:{REGION}:{account}:runtime/{RUNTIME_NAME}-*"
    endpoint_arn = f"{runtime_arn}/runtime-endpoint/{ENDPOINT_NAME}"
    bucket_arn = f"arn:aws:s3:::udemy-agentcore-s08-apne1-{account}-*"
    role_arn = f"arn:aws:iam::{account}:role/{ROLE_NAME}"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "S08Identity", "Effect": "Allow", "Action": "sts:GetCallerIdentity", "Resource": "*"},
            {
                "Sid": "S08ArtifactBucket",
                "Effect": "Allow",
                "Action": ["s3:CreateBucket", "s3:ListBucket", "s3:PutBucketVersioning"],
                "Resource": bucket_arn,
            },
            {
                "Sid": "S08ArtifactReadUpload",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject"],
                "Resource": f"{bucket_arn}/{OBJECT_PREFIX}/*",
            },
            {
                "Sid": "S08ExecutionRole",
                "Effect": "Allow",
                "Action": ["iam:CreateRole", "iam:GetRole", "iam:PutRolePolicy"],
                "Resource": role_arn,
            },
            {
                "Sid": "S08PassOnlyExecutionRole",
                "Effect": "Allow",
                "Action": "iam:PassRole",
                "Resource": role_arn,
                "Condition": {"StringEquals": {"iam:PassedToService": "bedrock-agentcore.amazonaws.com"}},
            },
            {
                "Sid": "S08ListInTokyo",
                "Effect": "Allow",
                "Action": ["bedrock-agentcore:ListAgentRuntimes", "bedrock-agentcore:ListAgentRuntimeVersions"],
                "Resource": "*",
                "Condition": {"StringEquals": {"aws:RequestedRegion": REGION}},
            },
            {
                "Sid": "S08CreateTaggedRuntimeInTokyo",
                "Effect": "Allow",
                "Action": "bedrock-agentcore:CreateAgentRuntime",
                "Resource": "*",
                "Condition": {
                    "StringEquals": {
                        "aws:RequestedRegion": REGION,
                        "aws:RequestTag/Course": "amazon-bedrock-agentcore-operations",
                        "aws:RequestTag/Section": "s08",
                    },
                    "ForAllValues:StringEquals": {
                        "aws:TagKeys": ["Course", "Section"],
                    },
                },
            },
            {
                "Sid": "S08TagOnlyRuntime",
                "Effect": "Allow",
                "Action": "bedrock-agentcore:TagResource",
                "Resource": runtime_arn,
                "Condition": {
                    "StringEquals": {
                        "aws:RequestedRegion": REGION,
                        "aws:RequestTag/Course": "amazon-bedrock-agentcore-operations",
                        "aws:RequestTag/Section": "s08",
                    },
                    "ForAllValues:StringEquals": {
                        "aws:TagKeys": ["Course", "Section"],
                    },
                },
            },
            {
                "Sid": "S08RuntimeOperations",
                "Effect": "Allow",
                "Action": ["bedrock-agentcore:GetAgentRuntime", "bedrock-agentcore:UpdateAgentRuntime"],
                "Resource": runtime_arn,
                "Condition": {"StringEquals": {"aws:RequestedRegion": REGION}},
            },
            {
                "Sid": "S08CreateEndpoint",
                "Effect": "Allow",
                "Action": "bedrock-agentcore:CreateAgentRuntimeEndpoint",
                "Resource": runtime_arn,
                "Condition": {"StringEquals": {"aws:RequestedRegion": REGION}},
            },
            {
                "Sid": "S08EndpointOperations",
                "Effect": "Allow",
                "Action": ["bedrock-agentcore:GetAgentRuntimeEndpoint", "bedrock-agentcore:UpdateAgentRuntimeEndpoint", "bedrock-agentcore:InvokeAgentRuntime"],
                "Resource": [runtime_arn, endpoint_arn],
                "Condition": {"StringEquals": {"aws:RequestedRegion": REGION}},
            },
        ],
    }


def _check_no_target(control: Any, iam: Any) -> None:
    token = None
    while True:
        request = {"maxResults": 100}
        if token:
            request["nextToken"] = token
        response = control.list_agent_runtimes(**request)
        conflicts = [item for item in response.get("agentRuntimes", [])
                     if item.get("agentRuntimeName") == RUNTIME_NAME]
        if conflicts:
            raise RuntimeError("An S08 runtime with the planned name already exists; do not reuse or modify it")
        token = response.get("nextToken")
        if not token:
            break
    try:
        iam.get_role(RoleName=ROLE_NAME)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "NoSuchEntity":
            return
        raise
    raise RuntimeError("The S08 execution role already exists; do not reuse or modify it")


def _bucket_available(s3: Any, bucket: str) -> None:
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as exc:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status == 404:
            return
        raise RuntimeError("The generated artifact bucket name is not confirmed available; stop without retry") from exc
    raise RuntimeError("The generated artifact bucket name already exists; stop without reusing it")


def _wait_runtime(control: Any, runtime_id: str, expected_version: str) -> dict[str, Any]:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    started = time.monotonic()
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = control.get_agent_runtime(agentRuntimeId=runtime_id)
        status = last.get("status")
        version = str(last.get("agentRuntimeVersion", ""))
        print(f"Runtime status={status} version={version} elapsed={int(time.monotonic()-started)}s", flush=True)
        if status == "READY" and version == expected_version:
            return last
        if status in {"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED", "DELETING"}:
            break
        time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
    raise RuntimeError(f"Runtime did not reach READY/V{expected_version}; last response={last}")


def _wait_endpoint(control: Any, runtime_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    started = time.monotonic()
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = control.get_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName=ENDPOINT_NAME)
        status = last.get("status")
        live = str(last.get("liveVersion", ""))
        print(f"Endpoint status={status} liveVersion={live} elapsed={int(time.monotonic()-started)}s", flush=True)
        if status == "READY" and live == "1":
            return last
        if status in {"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED", "DELETING"}:
            break
        time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
    raise RuntimeError(f"Endpoint did not reach READY/liveVersion=1; last response={last}")


def setup(zip_path: Path, profile: str | None, expected_account: str) -> None:
    if STATE_PATH.exists():
        raise RuntimeError(f"S08 state already exists at {STATE_PATH}; inspect exact AWS state, do not rerun setup")
    validate_expected_account(expected_account)
    zip_path = zip_path.resolve()
    if not zip_path.is_file() or zip_path.stat().st_size > 250 * 1024 * 1024:
        raise RuntimeError("Choose the exact ARM64 deployment ZIP (maximum 250 MB)")

    session = boto3.Session(profile_name=profile, region_name=REGION)
    sts = session.client("sts", config=SDK_CONFIG)
    s3 = session.client("s3", config=S3_CONFIG)
    iam = session.client("iam", config=SDK_CONFIG)
    control = session.client("bedrock-agentcore-control", config=SDK_CONFIG)
    actual_account = sts.get_caller_identity()["Account"]
    if actual_account != expected_account:
        raise RuntimeError("STS account does not match the confirmed S08 training account; no AWS write attempted")
    _check_no_target(control, iam)

    bucket = f"udemy-agentcore-s08-apne1-{actual_account}-{secrets.token_hex(3)}".lower()
    _bucket_available(s3, bucket)
    zip_hash = _sha256(zip_path)
    key = f"{OBJECT_PREFIX}/{zip_hash}/s08-runtime-arm64.zip"
    role_arn = f"arn:aws:iam::{actual_account}:role/{ROLE_NAME}"
    trust, role_policy = runtime_role_documents(actual_account)
    runtime_token = f"s08-{uuid.uuid4()}-create"
    endpoint_token = f"s08-{uuid.uuid4()}-endpoint"
    runtime_request = create_request(bucket, key, role_arn, runtime_token)
    endpoint_request: dict[str, Any] = {
        "name": ENDPOINT_NAME,
        "agentRuntimeVersion": "1",
        "description": "S08 course comparison endpoint",
        "clientToken": endpoint_token,
    }
    state: dict[str, Any] = {
        "schema_version": 1, "region": REGION, "account_id": actual_account,
        "stage": "planned", "zip_path": str(zip_path), "zip_sha256": zip_hash,
        "zip_size": zip_path.stat().st_size, "bucket": bucket, "bucket_versioning": "planned",
        "object_key": key, "object_version_id": None, "role_name": ROLE_NAME,
        "role_arn": role_arn, "role_trust": trust, "role_policy": role_policy,
        "runtime_create_request": runtime_request, "runtime_id": None, "runtime_arn": None,
        "runtime_version": None, "runtime_status": "planned", "endpoint_create_request": endpoint_request,
        "endpoint_arn": None, "endpoint_status": "planned",
    }
    _save_state(state)

    try:
        state["stage"] = "create_bucket_requested"
        _save_state(state)
        s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": REGION})
        state["stage"] = "bucket_created"
        _save_state(state)

        state["stage"] = "enable_bucket_versioning_requested"
        _save_state(state)
        s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
        state["bucket_versioning"] = "enabled"
        _save_state(state)

        state["stage"] = "upload_requested"
        _save_state(state)
        with zip_path.open("rb") as body:
            uploaded = s3.put_object(Bucket=bucket, Key=key, Body=body,
                                     ExpectedBucketOwner=actual_account, Metadata={"sha256": zip_hash})
        state["object_version_id"] = uploaded.get("VersionId")
        if not state["object_version_id"]:
            raise RuntimeError("S3 did not return a version ID; stop and inspect the exact object")
        state["stage"] = "artifact_uploaded"
        _save_state(state)

        state["stage"] = "create_role_requested"
        _save_state(state)
        role = iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=json.dumps(trust),
                               Description="S08 AgentCore Runtime execution role for course exercises")
        state["role_arn"] = role["Role"]["Arn"]
        state["stage"] = "role_created"
        _save_state(state)

        state["stage"] = "put_role_policy_requested"
        _save_state(state)
        iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="S08RuntimeExecution",
                            PolicyDocument=json.dumps(role_policy))
        state["stage"] = "role_policy_configured"
        _save_state(state)
        time.sleep(10)  # IAM propagation before the first Runtime create request.

        state["runtime_status"] = "create_requested"
        state["stage"] = "create_runtime_requested"
        _save_state(state)
        created = control.create_agent_runtime(**runtime_request)
        if str(created.get("agentRuntimeVersion")) != "1":
            raise RuntimeError(f"First created agent runtime version was not V1: {created}")
        state["runtime_id"] = created["agentRuntimeId"]
        state["runtime_arn"] = created["agentRuntimeArn"]
        state["runtime_version"] = str(created["agentRuntimeVersion"])
        state["runtime_status"] = created.get("status")
        state["stage"] = "runtime_create_accepted"
        _save_state(state)

        ready = _wait_runtime(control, state["runtime_id"], "1")
        if ready.get("agentRuntimeArn") != state["runtime_arn"] or ready.get("roleArn") != role_arn:
            raise RuntimeError("READY Runtime ARN or execution role differs from the planned exact target")
        state["runtime_status"] = "READY"
        state["stage"] = "runtime_ready_v1"
        _save_state(state)

        endpoint_request["agentRuntimeId"] = state["runtime_id"]
        state["stage"] = "create_endpoint_requested"
        state["endpoint_status"] = "create_requested"
        _save_state(state)
        endpoint = control.create_agent_runtime_endpoint(**endpoint_request)
        state["endpoint_arn"] = endpoint.get("agentRuntimeEndpointArn")
        state["endpoint_status"] = endpoint.get("status")
        state["stage"] = "endpoint_create_accepted"
        _save_state(state)

        ready_endpoint = _wait_endpoint(control, state["runtime_id"])
        state["endpoint_status"] = "READY"
        state["endpoint_arn"] = ready_endpoint.get("agentRuntimeEndpointArn", state["endpoint_arn"])
        state["endpoint_live_version"] = str(ready_endpoint.get("liveVersion"))
        state["stage"] = "setup_ready"
        _save_state(state)
        print(f"S08 Runtime READY V1: {state['runtime_arn']}")
        print(f"Endpoint READY/liveVersion=1: {state['endpoint_arn']}")
        print(f"Bucket/object/version: {bucket}/{key}/{state['object_version_id']}")
        print(f"Retain all S08 resources through S09; cleanup is owned by Section s10.")
    except Exception as exc:
        state["last_error"] = {"type": type(exc).__name__, "message": str(exc), "utc_epoch": time.time()}
        _save_state(state)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup_parser = commands.add_parser("setup", help="create the S08 V1 Runtime and named endpoint")
    setup_parser.add_argument("--zip", required=True, type=Path, dest="zip_path")
    setup_parser.add_argument("--expected-account", required=True)
    setup_parser.add_argument("--profile")
    policy_parser = commands.add_parser("print-caller-policy", help="print the S08-only caller IAM policy")
    policy_parser.add_argument("--expected-account", required=True)
    status_parser = commands.add_parser("status", help="read the local setup state without AWS calls")
    status_parser.add_argument("--state", type=Path, default=STATE_PATH)
    args = parser.parse_args()
    try:
        if args.command == "setup":
            setup(args.zip_path, args.profile, args.expected_account)
        elif args.command == "print-caller-policy":
            validate_expected_account(args.expected_account)
            print(json.dumps(caller_policy_document(args.expected_account), indent=2))
        else:
            if not args.state.is_file():
                raise RuntimeError("S08 setup state does not exist; setup has not completed")
            print(args.state.read_text(encoding="utf-8"))
    except (BotoCoreError, ClientError, OSError, ValueError, RuntimeError, KeyError) as exc:
        code = exc.response.get("Error", {}).get("Code", type(exc).__name__) if isinstance(exc, ClientError) else type(exc).__name__
        print(f"AWS setup stopped ({code}): {exc}. Preserve local state and inspect; do not rerun or broaden permissions.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
