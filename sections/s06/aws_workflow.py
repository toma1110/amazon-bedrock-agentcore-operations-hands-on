"""Bounded AWS workflow for the isolated S06 AgentCore Runtime exercise."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

REGION = "ap-northeast-1"
RUNTIME_NAME = "udemy_s06_obs_20261105"
ROLE_NAME = "udemy_s06_obs_20261105_runtime"
POLICY_NAME = "S06RuntimeExecution"
NAMESPACE = "Course/S06"
METRIC_NAME = "AgentInvocationLatencyMs"
SECTION = Path(__file__).resolve().parent
STATE_PATH = SECTION / ".agentcore-s06-state.json"
RAW_PATH = SECTION / "artifacts" / "s06-runtime-private.json"
LOG_GROUP_PREFIX = "/aws/bedrock-agentcore/runtimes/"
MAX_RUNTIME_REQUESTS = 3
MIN_REQUEST_INTERVAL_SECONDS = 60
READY_TIMEOUT_SECONDS = 1200
IDLE_SESSION_TIMEOUT_SECONDS = 300
MAX_SESSION_LIFETIME_SECONDS = 900

FIXED_PROMPT = "catalog-api の5xxを調べる順序を教えてください。"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _clients(profile: str | None) -> dict[str, Any]:
    session = boto3.Session(profile_name=profile, region_name=REGION)
    retry_once = Config(retries={"mode": "standard", "total_max_attempts": 1})
    return {
        "session": session,
        "sts": session.client("sts", config=retry_once),
        "s3": session.client("s3", config=retry_once),
        "iam": session.client("iam", config=retry_once),
        "control": session.client("bedrock-agentcore-control", config=retry_once),
        "runtime": session.client("bedrock-agentcore", config=retry_once),
        "logs": session.client("logs", config=retry_once),
        "cloudwatch": session.client("cloudwatch", config=retry_once),
    }


def _guard(clients: dict[str, Any], expected_account: str) -> None:
    identity = clients["sts"].get_caller_identity()
    if identity.get("Account") != expected_account or REGION != "ap-northeast-1":
        raise RuntimeError("Account or Region differs from the confirmed S06 target; stop")


def _load_local_state(expected_account: str) -> dict[str, Any]:
    if not STATE_PATH.is_file():
        raise RuntimeError("S06 state is missing; run preflight and deploy first")
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    if state.get("schema_version") != 1 or state.get("account_id") != expected_account:
        raise RuntimeError("S06 state does not match this account; preserve it and stop")
    if state.get("runtime_name") != RUNTIME_NAME or state.get("role_name") != ROLE_NAME:
        raise RuntimeError("S06 state identifies different resources; stop")
    return state


def _read_state(clients: dict[str, Any], expected_account: str) -> dict[str, Any]:
    state = _load_local_state(expected_account)
    _guard(clients, expected_account)
    return state


def _write_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(STATE_PATH)


def _runtime_role_documents(account: str) -> tuple[dict[str, Any], dict[str, Any]]:
    runtime_arn_pattern = f"arn:aws:bedrock-agentcore:{REGION}:{account}:runtime/{RUNTIME_NAME}-*"
    log_group_arn = f"arn:aws:logs:{REGION}:{account}:log-group:{LOG_GROUP_PREFIX}{RUNTIME_NAME}-*"
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {"aws:SourceAccount": account},
                "ArnLike": {"aws:SourceArn": runtime_arn_pattern},
            },
        }],
    }
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["logs:DescribeLogStreams", "logs:CreateLogGroup"],
                "Resource": log_group_arn,
            },
            {
                "Effect": "Allow",
                "Action": "logs:PutResourcePolicy",
                "Resource": log_group_arn,
            },
            {
                "Effect": "Allow",
                "Action": "logs:DescribeLogGroups",
                "Resource": f"arn:aws:logs:{REGION}:{account}:log-group:*",
            },
            {
                "Effect": "Allow",
                "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": f"{log_group_arn}:log-stream:*",
            },
            {
                "Effect": "Allow",
                "Action": "bedrock:InvokeModel",
                "Resource": f"arn:aws:bedrock:{REGION}::foundation-model/amazon.nova-lite-v1:0",
            },
            {
                "Effect": "Allow",
                "Action": "cloudwatch:PutMetricData",
                "Resource": "*",
                "Condition": {"StringEquals": {
                    "cloudwatch:namespace": NAMESPACE,
                    "aws:RequestedRegion": REGION,
                }},
            },
        ],
    }
    return trust, policy


def _wait_runtime(control: Any, runtime_id: str, version: str | None = None) -> dict[str, Any]:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        request: dict[str, Any] = {"agentRuntimeId": runtime_id}
        if version is not None:
            request["agentRuntimeVersion"] = version
        current = control.get_agent_runtime(**request)
        if current.get("status") == "READY":
            return current
        if current.get("status") not in {"CREATING", "UPDATING"}:
            raise RuntimeError("Dedicated S06 Runtime did not become READY; inspect without retrying")
        time.sleep(10)
    raise RuntimeError("S06 Runtime readiness timed out; inspect exact state before continuing")


def _wait_runtime_absent(control: Any, runtime_id: str) -> None:
    """Read-only poll after a delete request; never send DeleteAgentRuntime again."""
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            control.get_agent_runtime(agentRuntimeId=runtime_id)
        except ClientError as exc:
            if _missing_resource(exc):
                return
            raise
        time.sleep(10)
    raise RuntimeError("S06 Runtime deletion is still pending; do not resend delete or remove dependencies")


def _missing_resource(exc: ClientError) -> bool:
    code = exc.response.get("Error", {}).get("Code")
    return code in {"NoSuchEntity", "ResourceNotFoundException", "ResourceNotFound"}


def _expected_missing_prompt_validation(exc: ClientError) -> tuple[bool, int | None]:
    """Accept only the documented RuntimeClientError/424 wrapping an observed app 400."""
    response = exc.response if isinstance(exc.response, dict) else {}
    error = response.get("Error", {})
    metadata = response.get("ResponseMetadata", {})
    if error.get("Code") != "RuntimeClientError" or metadata.get("HTTPStatusCode") != 424:
        return False, None

    original_status = error.get("originalStatusCode")
    if original_status is None:
        original_status = metadata.get("originalStatusCode", metadata.get("OriginalStatusCode"))
    if original_status is None:
        message = str(error.get("Message", ""))
        match = re.search(r"Received error \((\d{3})\) from runtime", message, re.IGNORECASE)
        original_status = int(match.group(1)) if match else None
    try:
        original_status = int(original_status) if original_status is not None else None
    except (TypeError, ValueError):
        original_status = None
    return original_status == 400, original_status


def _metadata_matches(state: dict[str, Any], runtime: dict[str, Any]) -> bool:
    source_phase = state.get("phase")
    needs_mmdsv2 = state.get("reconcile_expected_metadata")
    if needs_mmdsv2 is None:
        needs_mmdsv2 = source_phase not in {"create_runtime_requested", "runtime_created"}
    actual = runtime.get("metadataConfiguration")
    expected = {"requireMMDSV2": True}
    return actual == expected or (not needs_mmdsv2 and actual in (None, {}))


def _assert_object_absent(s3: Any, bucket: str, key: str, account: str) -> None:
    try:
        s3.head_object(Bucket=bucket, Key=key, ExpectedBucketOwner=account)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}:
            return
        raise
    raise RuntimeError("S06 artifact still exists after delete; preserve state and inspect")


def _assert_role_absent(iam: Any, role_name: str) -> None:
    try:
        iam.get_role(RoleName=role_name)
    except ClientError as exc:
        if _missing_resource(exc):
            return
        raise
    raise RuntimeError("S06 role still exists after delete; preserve state and inspect")


def _assert_inline_policy_absent(iam: Any, role_name: str, policy_name: str) -> None:
    try:
        iam.get_role_policy(RoleName=role_name, PolicyName=policy_name)
    except ClientError as exc:
        if _missing_resource(exc):
            return
        raise
    raise RuntimeError("S06 inline policy still exists after delete; preserve state and inspect")


def _assert_log_group_absent(logs: Any, name: str) -> None:
    groups = logs.describe_log_groups(logGroupNamePrefix=name, limit=2).get("logGroups", [])
    if any(item.get("logGroupName") == name for item in groups):
        raise RuntimeError("S06 log group still exists after delete; preserve state and inspect")


def preflight(profile: str | None, expected_account: str, bucket: str, zip_path: Path) -> None:
    if not re.fullmatch(r"[0-9]{12}", expected_account):
        raise ValueError("--expected-account must be the 12-digit AWS account ID you confirmed")
    zip_path = zip_path.resolve()
    if not zip_path.is_file():
        raise FileNotFoundError("Build the exact ZIP before S06 preflight")
    clients = _clients(profile)
    _guard(clients, expected_account)
    if STATE_PATH.exists():
        raise RuntimeError("An S06 state file already exists; preserve it and inspect before starting another run")
    s3 = clients["s3"]
    s3.head_bucket(Bucket=bucket, ExpectedBucketOwner=expected_account)
    versioning = s3.get_bucket_versioning(Bucket=bucket, ExpectedBucketOwner=expected_account)
    if versioning.get("Status") is not None:
        raise RuntimeError("Artifact bucket versioning is enabled or suspended; stop before uploading")
    iam = clients["iam"]
    try:
        iam.get_role(RoleName=ROLE_NAME)
    except ClientError as exc:
        if not _missing_resource(exc):
            raise
    else:
        raise RuntimeError("Dedicated S06 role name already exists; do not adopt or change it")
    control = clients["control"]
    page = control.list_agent_runtimes(maxResults=100)
    while True:
        for item in page.get("agentRuntimes", []):
            if item.get("agentRuntimeName") == RUNTIME_NAME:
                raise RuntimeError("Dedicated S06 Runtime name already exists; do not adopt or change it")
        token = page.get("nextToken")
        if not token:
            break
        page = control.list_agent_runtimes(maxResults=100, nextToken=token)
    digest = _sha256(zip_path)
    run_id = f"s06-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:8]}"
    key = f"s06/{run_id}/{digest}/s06-runtime-arm64.zip"
    state = {
        "schema_version": 1,
        "account_id": expected_account,
        "region": REGION,
        "runtime_name": RUNTIME_NAME,
        "role_name": ROLE_NAME,
        "policy_name": POLICY_NAME,
        "artifact_bucket": bucket,
        "artifact_key": key,
        "run_id": run_id,
        "zip_path": str(zip_path),
        "zip_sha256": digest,
        "zip_size": zip_path.stat().st_size,
        "phase": "preflight_complete",
        "requests": [],
        # Persist the exact desired identities before the first AWS write. A
        # later reconcile can compare observed resources without replaying a
        # create/update/upload whose response may have been lost.
        "role_trust": _runtime_role_documents(expected_account)[0],
        "role_policy": _runtime_role_documents(expected_account)[1],
        "desired_environment": {
            "S06_TRACE_ENABLED": "true",
            "S06_METRICS_ENABLED": "true",
            "S06_MODEL_STREAMING": "false",
            "S06_RUN_ID": run_id,
        },
        "desired_lifecycle": {
            "idleRuntimeSessionTimeout": IDLE_SESSION_TIMEOUT_SECONDS,
            "maxLifetime": MAX_SESSION_LIFETIME_SECONDS,
        },
        "role_create_request": {
            "RoleName": ROLE_NAME,
            "Description": "Udemy S06 isolated Runtime execution role",
            "Tags": [
                {"Key": "CourseSection", "Value": "S06"},
                {"Key": "Purpose", "Value": "hands-on"},
                {"Key": "RunId", "Value": run_id},
            ],
        },
        "artifact_metadata": {"sha256": digest, "purpose": "s06-hands-on", "run-id": run_id},
    }
    _write_state(state)
    print(json.dumps({key: state[key] for key in (
        "account_id", "region", "runtime_name", "role_name", "artifact_bucket",
        "artifact_key", "zip_sha256", "zip_size", "phase",
    )}, indent=2, ensure_ascii=False))


def deploy(profile: str | None, expected_account: str) -> None:
    clients = _clients(profile)
    state = _read_state(clients, expected_account)
    if state.get("phase") != "preflight_complete":
        raise RuntimeError("S06 state is not at a deploy-ready phase; inspect without retrying")
    zip_path = Path(state["zip_path"])
    if _sha256(zip_path) != state["zip_sha256"] or zip_path.stat().st_size != state["zip_size"]:
        raise RuntimeError("The build ZIP differs from the preflight identity; stop")
    _guard(clients, expected_account)
    state["phase"] = "create_role_requested"
    _write_state(state)
    trust, policy = _runtime_role_documents(expected_account)
    role_create_request = {
        **state["role_create_request"],
        "AssumeRolePolicyDocument": json.dumps(trust),
    }
    state["role_create_request"] = role_create_request
    _write_state(state)
    role = clients["iam"].create_role(**role_create_request)["Role"]
    state.update({"role_arn": role["Arn"], "role_trust": trust, "role_policy": policy, "phase": "role_created"})
    _write_state(state)
    _guard(clients, expected_account)
    role_policy_request = {
        "RoleName": ROLE_NAME, "PolicyName": POLICY_NAME,
        "PolicyDocument": json.dumps(policy),
    }
    state["role_policy_request"] = role_policy_request
    state["phase"] = "put_role_policy_requested"
    _write_state(state)
    clients["iam"].put_role_policy(**role_policy_request)
    state["phase"] = "role_configured"
    _write_state(state)
    time.sleep(10)
    _guard(clients, expected_account)
    state["phase"] = "put_object_requested"
    _write_state(state)
    with zip_path.open("rb") as body:
        clients["s3"].put_object(
            Bucket=state["artifact_bucket"], Key=state["artifact_key"], Body=body,
            ExpectedBucketOwner=expected_account,
            Metadata=state["artifact_metadata"],
        )
    uploaded = clients["s3"].head_object(
        Bucket=state["artifact_bucket"], Key=state["artifact_key"], ExpectedBucketOwner=expected_account
    )
    if uploaded.get("ContentLength") != state["zip_size"] or uploaded.get("Metadata", {}).get("sha256") != state["zip_sha256"]:
        raise RuntimeError("Uploaded candidate identity differs; do not create a Runtime")
    state["phase"] = "object_uploaded"
    _write_state(state)
    _guard(clients, expected_account)
    runtime_create_request = {
        "agentRuntimeName": RUNTIME_NAME,
        "roleArn": state["role_arn"],
        "platformVersion": "V2",
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "lifecycleConfiguration": state["desired_lifecycle"],
        "environmentVariables": state["desired_environment"],
        "agentRuntimeArtifact": {"codeConfiguration": {
            "code": {"s3": {"bucket": state["artifact_bucket"], "prefix": state["artifact_key"]}},
            "runtime": "PYTHON_3_13",
            "entryPoint": ["runtime_app.py"],
        }},
        "clientToken": f"s06-{uuid.uuid4()}-create",
        "tags": {"CourseSection": "S06", "Purpose": "hands-on", "RunId": state["run_id"]},
    }
    state["runtime_create_request"] = runtime_create_request
    state["phase"] = "create_runtime_requested"
    _write_state(state)
    created = clients["control"].create_agent_runtime(**runtime_create_request)
    state.update({
        "runtime_id": created["agentRuntimeId"],
        "runtime_arn": created["agentRuntimeArn"],
        "created_version": created["agentRuntimeVersion"],
        "phase": "runtime_created",
    })
    state["log_group"] = f"{LOG_GROUP_PREFIX}{state['runtime_id']}-DEFAULT"
    _write_state(state)
    first = _wait_runtime(clients["control"], state["runtime_id"], state["created_version"])
    update_request = {
        "agentRuntimeId": state["runtime_id"],
        "agentRuntimeArtifact": first["agentRuntimeArtifact"],
        "roleArn": state["role_arn"],
        "platformVersion": "V2",
        "networkConfiguration": first["networkConfiguration"],
        "protocolConfiguration": first["protocolConfiguration"],
        "environmentVariables": first.get("environmentVariables", {}),
        "metadataConfiguration": {"requireMMDSV2": True},
        "lifecycleConfiguration": {
            "idleRuntimeSessionTimeout": IDLE_SESSION_TIMEOUT_SECONDS,
            "maxLifetime": MAX_SESSION_LIFETIME_SECONDS,
        },
        "clientToken": f"s06-{uuid.uuid4()}-update",
    }
    state["runtime_update_request"] = update_request
    state["phase"] = "update_runtime_requested"
    _write_state(state)
    updated = clients["control"].update_agent_runtime(**update_request)
    state.update({"runtime_version": updated["agentRuntimeVersion"], "phase": "runtime_updating"})
    _write_state(state)
    ready = _wait_runtime(clients["control"], state["runtime_id"], state["runtime_version"])
    endpoint = clients["control"].get_agent_runtime_endpoint(
        agentRuntimeId=state["runtime_id"], endpointName="DEFAULT"
    )
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while endpoint.get("status") in {"CREATING", "UPDATING"} and time.monotonic() < deadline:
        time.sleep(10)
        endpoint = clients["control"].get_agent_runtime_endpoint(
            agentRuntimeId=state["runtime_id"], endpointName="DEFAULT"
        )
    if endpoint.get("status") != "READY" or endpoint.get("liveVersion") != state["runtime_version"]:
        raise RuntimeError("S06 DEFAULT endpoint does not point to the exact READY version; stop")
    state.update({
        "runtime_status": ready.get("status"),
        "endpoint_status": endpoint.get("status"),
        "endpoint_version": endpoint.get("liveVersion"),
        "phase": "ready",
    })
    _write_state(state)
    print(json.dumps({key: state[key] for key in (
        "account_id", "region", "runtime_name", "runtime_id", "runtime_arn",
        "role_name", "role_arn", "artifact_bucket", "artifact_key",
        "zip_sha256", "zip_size", "log_group", "runtime_version", "endpoint_status", "phase",
    )}, indent=2, ensure_ascii=False))


def invoke(profile: str | None, expected_account: str) -> None:
    clients = _clients(profile)
    state = _read_state(clients, expected_account)
    if state.get("phase") != "ready" or len(state.get("requests", [])):
        raise RuntimeError("S06 must be READY with no saved invocation; do not repeat this exercise")
    _guard(clients, expected_account)
    session_id = f"s06-{uuid.uuid4().hex}-runtime-session"
    state.update({"session_id": session_id, "phase": "invoke_started", "requests": []})
    _write_state(state)
    prompt = FIXED_PROMPT
    workload = [({"prompt": prompt}, "valid"), ({"prompt": prompt}, "valid-repeat"), ({}, "missing-prompt")]
    raw: dict[str, Any] = {
        "account_id": expected_account,
        "region": REGION,
        "runtime_id": state["runtime_id"],
        "session_id": session_id,
        "argv": list(sys.argv),
        "fixed_prompt": prompt,
        "requests": [],
    }
    for index, (payload, case) in enumerate(workload, start=1):
        if index > 1:
            time.sleep(MIN_REQUEST_INTERVAL_SECONDS + 1)
        sent_at = datetime.now(timezone.utc)
        record: dict[str, Any] = {
            "sequence": index,
            "case": case,
            "sent_at": sent_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "request_body": payload,
        }
        state["requests"].append({"sequence": index, "case": case, "sent_at": record["sent_at"]})
        state["last_request_started_at"] = record["sent_at"]
        state["phase"] = f"invoke_{index}_requested"
        _write_state(state)
        try:
            response = clients["runtime"].invoke_agent_runtime(
                agentRuntimeArn=state["runtime_arn"],
                qualifier="DEFAULT",
                runtimeSessionId=session_id,
                contentType="application/json",
                accept="application/json",
                payload=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            )
            body = response["response"].read()
            record["outer_status"] = response.get("statusCode")
            record["outer_trace_id"] = response.get("traceId")
            if body:
                record["body"] = json.loads(body.decode("utf-8"))
            record["received_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        except Exception as exc:
            error = getattr(exc, "response", {})
            error_details = error.get("Error", {}) if isinstance(error, dict) else {}
            response_metadata = error.get("ResponseMetadata", {}) if isinstance(error, dict) else {}
            expected_missing_prompt, original_status = (
                _expected_missing_prompt_validation(exc) if isinstance(exc, ClientError) else (False, None)
            )
            record.update({
                "outer_error_type": type(exc).__name__,
                "outer_error_code": error_details.get("Code"),
                "outer_error_message": error_details.get("Message"),
                "outer_http_status": response_metadata.get("HTTPStatusCode"),
                "original_status_code": original_status,
                "received_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            })
            raw["requests"].append(record)
            state["requests"][-1].update({
                "outer_error_type": record["outer_error_type"],
                "outer_error_code": record["outer_error_code"],
                "outer_error_message": record["outer_error_message"],
                "outer_http_status": record["outer_http_status"],
                "original_status_code": record["original_status_code"],
                "received_at": record["received_at"],
            })
            state["last_request_completed_at"] = record["received_at"]
            if case == "missing-prompt" and expected_missing_prompt:
                record["validation_status_evidence"] = "RuntimeClientError/424 with original application status 400"
                state["requests"][-1]["validation_status_evidence"] = record["validation_status_evidence"]
                state["phase"] = f"invoke_{index}_recorded"
                _write_state(state)
                RAW_PATH.parent.mkdir(parents=True, exist_ok=True)
                RAW_PATH.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
                continue
            state["phase"] = f"invoke_{index}_uncertain"
            if case == "missing-prompt" and isinstance(exc, ClientError):
                record["validation_status_evidence"] = "unconfirmed; outer error metadata did not establish application status 400"
                state["requests"][-1]["validation_status_evidence"] = record["validation_status_evidence"]
            _write_state(state)
            RAW_PATH.parent.mkdir(parents=True, exist_ok=True)
            RAW_PATH.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
            raise RuntimeError("Invoke response is uncertain; inspect this exact request in CloudWatch and do not retry") from None
        raw["requests"].append(record)
        RAW_PATH.parent.mkdir(parents=True, exist_ok=True)
        RAW_PATH.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
        state["phase"] = f"invoke_{index}_recorded"
        state["requests"][-1]["outer_status"] = record.get("outer_status")
        state["requests"][-1]["received_at"] = record["received_at"]
        state["last_request_completed_at"] = record["received_at"]
        _write_state(state)
    state["phase"] = "invoke_complete"
    _write_state(state)
    print(json.dumps({"phase": state["phase"], "runtime_id": state["runtime_id"],
                      "session_id": session_id, "raw_record": str(RAW_PATH),
                      "request_count": len(raw["requests"])}, indent=2, ensure_ascii=False))


def readback(profile: str | None, expected_account: str) -> None:
    state = _load_local_state(expected_account)
    if "s06_readback" in state or state.get("readback_started_at"):
        raise RuntimeError("S06 readback was already started; do not query again")
    source_phase = str(state.get("phase"))
    uncertain_invoke = bool(re.fullmatch(r"invoke_[1-3]_(requested|uncertain)", source_phase))
    if not (
        state.get("phase") == "invoke_complete"
        and len(state.get("requests", [])) == MAX_RUNTIME_REQUESTS
    ) and not uncertain_invoke:
        raise RuntimeError("Readback requires completed requests or an uncertain/requested invoke; do not invoke again")

    raw: dict[str, Any] | None = None
    if uncertain_invoke:
        if not state.get("requests") or not RAW_PATH.is_file():
            raise RuntimeError("Uncertain readback needs a saved send intent and received RAW subset; do not search a wider window")
        raw = json.loads(RAW_PATH.read_text(encoding="utf-8"))
        if (
            raw.get("runtime_id") != state.get("runtime_id")
            or raw.get("session_id") != state.get("session_id")
            or raw.get("account_id") != expected_account
        ):
            raise RuntimeError("Saved RAW subset does not match this exact Runtime session; preserve it and stop")
        raw_requests = raw.get("requests")
        state_requests = state.get("requests")
        invoke_match = re.fullmatch(r"invoke_([1-3])_(requested|uncertain)", source_phase)
        if (
            not isinstance(raw_requests, list) or not raw_requests
            or not isinstance(state_requests, list)
            or invoke_match is None
            or len(state_requests) != int(invoke_match.group(1))
            or len(raw_requests) > MAX_RUNTIME_REQUESTS or len(state_requests) > MAX_RUNTIME_REQUESTS
            or raw.get("fixed_prompt") != FIXED_PROMPT
        ):
            raise RuntimeError("No bounded received RAW subset is available; do not search a wider window")
        fixed_workload = [
            ({"prompt": FIXED_PROMPT}, "valid"),
            ({"prompt": FIXED_PROMPT}, "valid-repeat"),
            ({}, "missing-prompt"),
        ]
        for sequence, record in enumerate(raw_requests, start=1):
            expected_body, expected_case = fixed_workload[sequence - 1] if sequence <= len(fixed_workload) else (None, None)
            if (
                not isinstance(record, dict)
                or record.get("sequence") != sequence
                or sequence > len(state_requests)
                or not isinstance(state_requests[sequence - 1], dict)
                or record.get("case") != expected_case
                or state_requests[sequence - 1].get("case") != expected_case
                or record.get("request_body") != expected_body
                or record.get("sent_at") != state_requests[sequence - 1].get("sent_at")
                or not record.get("received_at")
            ):
                raise RuntimeError("Saved RAW request case/body does not match the fixed send intent; stop without querying")
        window_records = raw_requests
    else:
        window_records = state.get("requests", [])
    try:
        start = datetime.fromisoformat(window_records[0]["sent_at"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(window_records[-1]["received_at"].replace("Z", "+00:00"))
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Saved send/receive timestamps do not define a bounded readback window; stop") from exc
    if end < start:
        raise RuntimeError("Saved readback end precedes its send intent; stop")
    if any(
        datetime.fromisoformat(window_records[index]["sent_at"].replace("Z", "+00:00"))
        < datetime.fromisoformat(window_records[index - 1]["received_at"].replace("Z", "+00:00"))
        for index in range(1, len(window_records))
    ):
        raise RuntimeError("Saved request windows overlap or are out of order; stop without widening the query")

    state["readback_source_phase"] = source_phase
    state["readback_mode"] = "read_only_uncertain" if uncertain_invoke else "standard"
    state["phase"] = "readback_started"
    state["readback_started_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    _write_state(state)
    clients = _clients(profile)
    _guard(clients, expected_account)
    start_ms = int((start.timestamp() - 60) * 1000)
    end_ms = int((end.timestamp() + 60) * 1000)
    expected_log_group = f"{LOG_GROUP_PREFIX}{state.get('runtime_id')}-DEFAULT"
    if not state.get("runtime_id") or state.get("log_group") != expected_log_group:
        raise RuntimeError("Readback log group does not match the exact saved S06 Runtime ID; stop")
    groups = clients["logs"].describe_log_groups(
        logGroupNamePrefix=state["log_group"], limit=2
    ).get("logGroups", [])
    exact_groups = [item for item in groups if item.get("logGroupName") == state["log_group"]]
    if len(exact_groups) != 1:
        raise RuntimeError("The exact S06 Runtime log group is not uniquely present; stop")
    if not uncertain_invoke:
        clients["logs"].put_retention_policy(
            logGroupName=state["log_group"], retentionInDays=1
        )
    page = clients["logs"].filter_log_events(
        logGroupName=state["log_group"], startTime=start_ms, endTime=end_ms, limit=100
    )
    events = list(page.get("events", []))
    if page.get("nextToken"):
        raise RuntimeError("More than one log page is present; stop without fetching more than the bounded window")
    raw_bytes = sum(len(item.get("message", "").encode("utf-8")) for item in events)
    if len(events) > 100 or raw_bytes > 1_048_576:
        raise RuntimeError("Exact log window exceeds the S06 readback bound; stop")
    metric = clients["cloudwatch"].get_metric_data(
        StartTime=start.replace(second=0, microsecond=0),
        EndTime=end.replace(second=0, microsecond=0) + timedelta(minutes=1),
        MetricDataQueries=[{
            "Id": "s06latency",
            "MetricStat": {
                "Metric": {
                    "Namespace": NAMESPACE,
                    "MetricName": METRIC_NAME,
                    "Dimensions": [
                        {"Name": "Section", "Value": "S06"},
                        {"Name": "RunId", "Value": state["run_id"]},
                    ],
                },
                "Period": 60,
                "Stat": "SampleCount",
            },
            "ReturnData": True,
        }],
        MaxDatapoints=3,
    )
    metric_results = metric.get("MetricDataResults", [])
    if (
        metric.get("NextToken")
        or len(metric_results) > 1
        or sum(len(item.get("Values", [])) for item in metric_results) > 3
    ):
        raise RuntimeError("Metric readback exceeded one S06 RunId series or three datapoints; stop")
    readback = {
        "log_group": state["log_group"],
        "source_phase": source_phase,
        "read_only": uncertain_invoke,
        "invoke_status_confirmed": not uncertain_invoke,
        "next_review": (
            "manual independent QA; application status remains unconfirmed"
            if uncertain_invoke else "standard post-upload QA"
        ),
        "captured_request_count": len(window_records),
        "utc_window_start": start.isoformat().replace("+00:00", "Z"),
        "utc_window_end": end.isoformat().replace("+00:00", "Z"),
        "event_count": len(events),
        "returned_log_bytes": raw_bytes,
        "events": events,
        "metric_results": metric_results,
    }
    state["s06_readback"] = readback
    state["phase"] = "readback_complete"
    _write_state(state)
    if RAW_PATH.exists():
        raw = json.loads(RAW_PATH.read_text(encoding="utf-8"))
        raw["readback"] = readback
        RAW_PATH.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"phase": state["phase"], "log_group": state["log_group"],
                      "event_count": len(events), "returned_log_bytes": raw_bytes,
                      "metric_datapoints": len(metric.get("MetricDataResults", [{}])[0].get("Values", [])),
                      "raw_record": str(RAW_PATH)}, indent=2, ensure_ascii=False))


def reconcile(profile: str | None, expected_account: str) -> None:
    """Read-only inspection after an uncertain deploy write; never replay it."""
    clients = _clients(profile)
    state = _read_state(clients, expected_account)
    if state.get("phase") not in {
        "create_role_requested", "put_role_policy_requested", "put_object_requested",
        "create_runtime_requested", "update_runtime_requested", "runtime_created",
        "runtime_updating", "runtime_ready", "partial_delete_runtime_requested",
        "partial_delete_object_requested", "partial_delete_role_policy_requested",
        "partial_delete_role_requested",
        "delete_runtime_requested", "runtime_deleting", "delete_object_requested",
        "object_deleted", "delete_role_policy_requested", "role_policy_deleted",
        "delete_role_requested", "role_deleted",
        "readback_started", "invoke_started",
    }:
        if not re.fullmatch(r"invoke_[1-3]_(requested|recorded|uncertain)", str(state.get("phase"))):
            raise RuntimeError("Reconcile is only for a saved uncertain deploy, invoke, or readback phase; inspect without replaying")
    deploy_phases = {
        "create_role_requested", "put_role_policy_requested", "put_object_requested",
        "create_runtime_requested", "update_runtime_requested", "runtime_created", "runtime_updating",
    }
    if state["phase"] in deploy_phases:
        state["reconcile_expected_metadata"] = state["phase"] in {"update_runtime_requested", "runtime_updating"}

    found: dict[str, Any] = {"role": None, "artifact": None, "runtime": None, "log_group": None}
    try:
        role = clients["iam"].get_role(RoleName=ROLE_NAME)["Role"]
    except ClientError as exc:
        if not _missing_resource(exc):
            raise
    else:
        tags = {item["Key"]: item["Value"] for item in role.get("Tags", [])}
        if (
            role.get("Description") != "Udemy S06 isolated Runtime execution role"
            or _canonical(role.get("AssumeRolePolicyDocument")) != _canonical(state["role_trust"])
            or tags.get("RunId") != state["run_id"]
            or tags.get("CourseSection") != "S06"
            or tags.get("Purpose") != "hands-on"
        ):
            raise RuntimeError("Observed role does not match the exact S06 run; preserve it and stop")
        try:
            inline = clients["iam"].get_role_policy(RoleName=ROLE_NAME, PolicyName=POLICY_NAME)["PolicyDocument"]
        except ClientError as exc:
            if not _missing_resource(exc):
                raise
            inline = None
        if inline is not None and _canonical(inline) != _canonical(state["role_policy"]):
            raise RuntimeError("Observed inline policy differs from this S06 run; preserve it and stop")
        attached = clients["iam"].list_attached_role_policies(RoleName=ROLE_NAME).get("AttachedPolicies", [])
        if attached:
            raise RuntimeError("Observed role has an unexpected managed policy; preserve it and stop")
        state["role_arn"] = role["Arn"]
        found["role"] = {"arn": role["Arn"], "inline_policy_present": inline is not None}

    try:
        artifact = clients["s3"].head_object(
            Bucket=state["artifact_bucket"], Key=state["artifact_key"],
            ExpectedBucketOwner=expected_account,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in {"NoSuchKey", "404", "NotFound"}:
            raise
    else:
        metadata = artifact.get("Metadata", {})
        if (
            artifact.get("ContentLength") != state["zip_size"]
            or metadata.get("sha256") != state["zip_sha256"]
            or metadata.get("run-id") != state["run_id"]
            or metadata.get("purpose") != "s06-hands-on"
        ):
            raise RuntimeError("Observed artifact differs from this S06 run; preserve it and stop")
        found["artifact"] = {"bucket": state["artifact_bucket"], "key": state["artifact_key"],
                             "sha256": state["zip_sha256"], "size": state["zip_size"]}

    matches: list[dict[str, Any]] = []
    page = clients["control"].list_agent_runtimes(maxResults=100)
    while True:
        matches.extend(item for item in page.get("agentRuntimes", [])
                       if item.get("agentRuntimeName") == RUNTIME_NAME)
        token = page.get("nextToken")
        if not token:
            break
        page = clients["control"].list_agent_runtimes(maxResults=100, nextToken=token)
    if len(matches) > 1:
        raise RuntimeError("Multiple Runtime objects match the dedicated S06 name; preserve all and stop")
    if matches:
        listed_runtime_id = matches[0]["agentRuntimeId"]
        state.setdefault("runtime_id", listed_runtime_id)
        state.setdefault("log_group", f"{LOG_GROUP_PREFIX}{listed_runtime_id}-DEFAULT")
        try:
            runtime = clients["control"].get_agent_runtime(agentRuntimeId=listed_runtime_id)
        except ClientError as exc:
            if not _missing_resource(exc):
                raise
            runtime = None
    else:
        runtime = None
    if runtime:
        runtime_tags = clients["control"].list_tags_for_resource(
            resourceArn=runtime["agentRuntimeArn"]
        ).get("tags", {})
        code_configuration = (runtime.get("agentRuntimeArtifact") or {}).get("codeConfiguration") or {}
        code = (code_configuration.get("code") or {}).get("s3") or {}
        if (
            runtime.get("agentRuntimeName") != RUNTIME_NAME
            or runtime.get("roleArn") != state.get("role_arn")
            or code.get("bucket") != state["artifact_bucket"]
            or code.get("prefix") != state["artifact_key"]
            or runtime.get("environmentVariables") != state["desired_environment"]
            or runtime.get("lifecycleConfiguration") != state["desired_lifecycle"]
            or runtime.get("platformVersion") != "V2"
            or runtime.get("networkConfiguration") != {"networkMode": "PUBLIC"}
            or runtime.get("protocolConfiguration") != {"serverProtocol": "HTTP"}
            or not _metadata_matches(state, runtime)
            or runtime_tags != {"CourseSection": "S06", "Purpose": "hands-on", "RunId": state["run_id"]}
            or code_configuration != state.get("runtime_create_request", {}).get("agentRuntimeArtifact", {}).get("codeConfiguration")
        ):
            raise RuntimeError("Observed Runtime does not match the exact S06 run; preserve it and stop")
        runtime_id = runtime["agentRuntimeId"]
        state.update({"runtime_id": runtime_id, "runtime_arn": runtime["agentRuntimeArn"]})
        state["log_group"] = f"{LOG_GROUP_PREFIX}{runtime_id}-DEFAULT"
        if runtime.get("status") == "DELETING":
            _wait_runtime_absent(clients["control"], runtime_id)
        else:
            found["runtime"] = {"id": runtime_id, "arn": runtime["agentRuntimeArn"],
                                 "status": runtime.get("status")}

    runtime_id = state.get("runtime_id")
    if runtime_id:
        if runtime is None:
            try:
                saved_runtime = clients["control"].get_agent_runtime(agentRuntimeId=runtime_id)
            except ClientError as exc:
                if not _missing_resource(exc):
                    raise
            else:
                if saved_runtime.get("status") != "DELETING":
                    raise RuntimeError("Saved Runtime ID is absent from the name listing but still exists; preserve it and stop")
                _wait_runtime_absent(clients["control"], runtime_id)
        expected_log_group = f"{LOG_GROUP_PREFIX}{runtime_id}-DEFAULT"
        if state.get("log_group") not in (None, expected_log_group):
            raise RuntimeError("Saved S06 log group does not match its exact Runtime ID; stop")
        state["log_group"] = expected_log_group
        groups = clients["logs"].describe_log_groups(
            logGroupNamePrefix=expected_log_group, limit=2
        ).get("logGroups", [])
        exact_groups = [item for item in groups if item.get("logGroupName") == expected_log_group]
        if len(exact_groups) > 1:
            raise RuntimeError("Multiple exact S06 log groups were returned; preserve them and stop")
        if exact_groups:
            found["log_group"] = {"name": expected_log_group, "arn": exact_groups[0].get("arn")}

    state["reconcile_from_phase"] = state["phase"]
    state["reconciled_resources"] = found
    state["phase"] = "partial_reconciled"
    _write_state(state)
    print(json.dumps({"phase": state["phase"], "run_id": state["run_id"],
                      "resources": found,
                      "next": "Review exact findings, then run cleanup if all present resources are expected."},
                     indent=2, ensure_ascii=False))


def _cleanup_reconciled_partial(clients: dict[str, Any], state: dict[str, Any], expected_account: str) -> None:
    """Delete only exact resources recorded by reconcile, checking each anew."""
    found = state.get("reconciled_resources")
    if not isinstance(found, dict):
        raise RuntimeError("Partial cleanup requires a saved read-only reconcile; stop")

    runtime_id = (found.get("runtime") or {}).get("id") or state.get("runtime_id")
    if (found.get("runtime") or {}).get("id"):
        runtime = clients["control"].get_agent_runtime(agentRuntimeId=runtime_id)
        runtime_tags = clients["control"].list_tags_for_resource(
            resourceArn=runtime["agentRuntimeArn"]
        ).get("tags", {})
        code_configuration = (runtime.get("agentRuntimeArtifact") or {}).get("codeConfiguration") or {}
        code = (code_configuration.get("code") or {}).get("s3") or {}
        if (
            runtime.get("agentRuntimeArn") != state.get("runtime_arn")
            or runtime.get("agentRuntimeName") != RUNTIME_NAME
            or runtime.get("roleArn") != state.get("role_arn")
            or code.get("bucket") != state["artifact_bucket"]
            or code.get("prefix") != state["artifact_key"]
            or runtime.get("environmentVariables") != state["desired_environment"]
            or runtime.get("lifecycleConfiguration") != state["desired_lifecycle"]
            or runtime.get("platformVersion") != "V2"
            or runtime.get("networkConfiguration") != {"networkMode": "PUBLIC"}
            or runtime.get("protocolConfiguration") != {"serverProtocol": "HTTP"}
            or not _metadata_matches(state, runtime)
            or runtime_tags != {"CourseSection": "S06", "Purpose": "hands-on", "RunId": state["run_id"]}
            or code_configuration != state.get("runtime_create_request", {}).get("agentRuntimeArtifact", {}).get("codeConfiguration")
        ):
            raise RuntimeError("Runtime changed after reconcile; preserve it and stop")
        if runtime.get("status") == "DELETING":
            _wait_runtime_absent(clients["control"], runtime_id)
        else:
            delete_token = state.get("runtime_delete_client_token") or f"s06-{uuid.uuid4()}-delete"
            state["runtime_delete_client_token"] = delete_token
            state["phase"] = "partial_delete_runtime_requested"
            _write_state(state)
            clients["control"].delete_agent_runtime(agentRuntimeId=runtime_id, clientToken=delete_token)
            _wait_runtime_absent(clients["control"], runtime_id)
    elif runtime_id:
        try:
            saved_runtime = clients["control"].get_agent_runtime(agentRuntimeId=runtime_id)
        except ClientError as exc:
            if not _missing_resource(exc):
                raise
        else:
            if saved_runtime.get("status") != "DELETING":
                raise RuntimeError("Runtime still exists after reconcile; stop before dependency cleanup")
            _wait_runtime_absent(clients["control"], runtime_id)

    if found.get("artifact"):
        bucket_versioning = clients["s3"].get_bucket_versioning(
            Bucket=state["artifact_bucket"], ExpectedBucketOwner=expected_account
        )
        if bucket_versioning.get("Status") is not None:
            raise RuntimeError("Artifact bucket versioning changed; preserve resources and stop")
        artifact = clients["s3"].head_object(
            Bucket=state["artifact_bucket"], Key=state["artifact_key"],
            ExpectedBucketOwner=expected_account,
        )
        metadata = artifact.get("Metadata", {})
        if (
            artifact.get("ContentLength") != state["zip_size"]
            or metadata.get("sha256") != state["zip_sha256"]
            or metadata.get("run-id") != state["run_id"]
            or metadata.get("purpose") != "s06-hands-on"
        ):
            raise RuntimeError("Artifact changed after reconcile; preserve it and stop")
        state["phase"] = "partial_delete_object_requested"
        _write_state(state)
        clients["s3"].delete_object(
            Bucket=state["artifact_bucket"], Key=state["artifact_key"],
            ExpectedBucketOwner=expected_account,
        )
        _assert_object_absent(clients["s3"], state["artifact_bucket"], state["artifact_key"], expected_account)

    if found.get("role"):
        role = clients["iam"].get_role(RoleName=ROLE_NAME)["Role"]
        tags = {item["Key"]: item["Value"] for item in role.get("Tags", [])}
        if (
            role.get("Arn") != state.get("role_arn")
            or role.get("Description") != "Udemy S06 isolated Runtime execution role"
            or _canonical(role.get("AssumeRolePolicyDocument")) != _canonical(state["role_trust"])
            or tags.get("RunId") != state["run_id"]
        ):
            raise RuntimeError("Role changed after reconcile; preserve it and stop")
        attached = clients["iam"].list_attached_role_policies(RoleName=ROLE_NAME).get("AttachedPolicies", [])
        if attached:
            raise RuntimeError("Unexpected managed policy is attached; preserve the role and stop")
        try:
            current_policy = clients["iam"].get_role_policy(
                RoleName=ROLE_NAME, PolicyName=POLICY_NAME
            )["PolicyDocument"]
        except ClientError as exc:
            if not _missing_resource(exc):
                raise
            current_policy = None
        if current_policy is not None and _canonical(current_policy) != _canonical(state["role_policy"]):
            raise RuntimeError("Role policy changed after reconcile; preserve it and stop")
        if current_policy is not None:
            state["phase"] = "partial_delete_role_policy_requested"
            _write_state(state)
            clients["iam"].delete_role_policy(RoleName=ROLE_NAME, PolicyName=POLICY_NAME)
            _assert_inline_policy_absent(clients["iam"], ROLE_NAME, POLICY_NAME)
        state["phase"] = "partial_delete_role_requested"
        _write_state(state)
        clients["iam"].delete_role(RoleName=ROLE_NAME)
        _assert_role_absent(clients["iam"], ROLE_NAME)

    if runtime_id and state.get("log_group") == f"{LOG_GROUP_PREFIX}{runtime_id}-DEFAULT":
        groups = clients["logs"].describe_log_groups(
            logGroupNamePrefix=state["log_group"], limit=2
        ).get("logGroups", [])
        exact = [item for item in groups if item.get("logGroupName") == state["log_group"]]
        if len(exact) > 1:
            raise RuntimeError("S06 log group is ambiguous; preserve it and stop")
        if exact:
            clients["logs"].delete_log_group(logGroupName=state["log_group"])
            _assert_log_group_absent(clients["logs"], state["log_group"])

    state["phase"] = "resources_deleted"
    _write_state(state)
    print(json.dumps({
        "phase": state["phase"], "run_id": state["run_id"],
        "runtime_id": runtime_id,
        "artifact_deleted": bool(found.get("artifact")),
        "role_deleted": bool(found.get("role")),
        "log_group_deleted": bool(runtime_id),
        "metric_series_retained_until_service_expiry": True,
    }, indent=2, ensure_ascii=False))


def cleanup(profile: str | None, expected_account: str) -> None:
    clients = _clients(profile)
    state = _read_state(clients, expected_account)
    if state.get("phase") == "partial_reconciled":
        _cleanup_reconciled_partial(clients, state, expected_account)
        return
    if state.get("phase") not in {"ready", "invoke_complete", "readback_complete", "partial_reconciled"}:
        raise RuntimeError("S06 is not in a cleanup-ready phase; inspect saved request/write state first")
    runtime_id = state.get("runtime_id")
    if not runtime_id:
        raise RuntimeError("Exact dedicated Runtime ID is missing; do not clean up")
    current_role = clients["iam"].get_role(RoleName=ROLE_NAME)["Role"]
    if (
        current_role.get("Description") != "Udemy S06 isolated Runtime execution role"
        or current_role.get("Arn") != state.get("role_arn")
        or _canonical(current_role.get("AssumeRolePolicyDocument")) != _canonical(state["role_trust"])
        or {item["Key"]: item["Value"] for item in current_role.get("Tags", [])}
        != {"CourseSection": "S06", "Purpose": "hands-on", "RunId": state["run_id"]}
    ):
        raise RuntimeError("S06 execution role identity differs; preserve resources and stop")
    attached = clients["iam"].list_attached_role_policies(RoleName=ROLE_NAME).get("AttachedPolicies", [])
    if attached:
        raise RuntimeError("Unexpected managed policy is attached to the S06 role; do not delete it")
    current_policy = clients["iam"].get_role_policy(RoleName=ROLE_NAME, PolicyName=POLICY_NAME)["PolicyDocument"]
    if _canonical(current_policy) != _canonical(state["role_policy"]):
        raise RuntimeError("S06 policy differs from the saved exact policy; preserve resources and stop")
    artifact = clients["s3"].head_object(
        Bucket=state["artifact_bucket"], Key=state["artifact_key"], ExpectedBucketOwner=expected_account
    )
    if artifact.get("ContentLength") != state["zip_size"] or artifact.get("Metadata", {}).get("sha256") != state["zip_sha256"]:
        raise RuntimeError("S06 candidate object identity differs; preserve resources and stop")
    current = clients["control"].get_agent_runtime(agentRuntimeId=runtime_id)
    runtime_tags = clients["control"].list_tags_for_resource(
        resourceArn=current["agentRuntimeArn"]
    ).get("tags", {})
    if current.get("agentRuntimeArn") != state.get("runtime_arn") or current.get("agentRuntimeName") != RUNTIME_NAME:
        raise RuntimeError("Dedicated Runtime identity differs; preserve resources and stop")
    code_configuration = (current.get("agentRuntimeArtifact") or {}).get("codeConfiguration") or {}
    code = (code_configuration.get("code") or {}).get("s3") or {}
    if (
        code.get("bucket") != state["artifact_bucket"]
        or code.get("prefix") != state["artifact_key"]
        or current.get("roleArn") != state["role_arn"]
        or current.get("environmentVariables") != state["desired_environment"]
        or current.get("lifecycleConfiguration") != state["desired_lifecycle"]
        or current.get("platformVersion") != "V2"
        or current.get("networkConfiguration") != {"networkMode": "PUBLIC"}
        or current.get("protocolConfiguration") != {"serverProtocol": "HTTP"}
        or current.get("metadataConfiguration") != {"requireMMDSV2": True}
        or runtime_tags != {"CourseSection": "S06", "Purpose": "hands-on", "RunId": state["run_id"]}
        or code_configuration != state.get("runtime_create_request", {}).get("agentRuntimeArtifact", {}).get("codeConfiguration")
    ):
        raise RuntimeError("Runtime configuration no longer matches this exact S06 run; do not delete it")
    endpoint = clients["control"].get_agent_runtime_endpoint(
        agentRuntimeId=runtime_id, endpointName="DEFAULT"
    )
    if endpoint.get("status") != "READY" or endpoint.get("liveVersion") != state.get("endpoint_version"):
        raise RuntimeError("DEFAULT endpoint differs from the saved S06 version; do not delete it")
    bucket_versioning = clients["s3"].get_bucket_versioning(
        Bucket=state["artifact_bucket"], ExpectedBucketOwner=expected_account
    )
    if bucket_versioning.get("Status") is not None:
        raise RuntimeError("Artifact bucket versioning changed; do not delete or claim full cleanup")
    state["phase"] = "delete_runtime_requested"
    delete_token = state.get("runtime_delete_client_token") or f"s06-{uuid.uuid4()}-delete"
    state["runtime_delete_client_token"] = delete_token
    _write_state(state)
    clients["control"].delete_agent_runtime(agentRuntimeId=runtime_id, clientToken=delete_token)
    state["phase"] = "runtime_deleting"
    _write_state(state)
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            clients["control"].get_agent_runtime(agentRuntimeId=runtime_id)
        except ClientError as exc:
            if not _missing_resource(exc):
                raise
            break
        time.sleep(10)
    else:
        raise RuntimeError("S06 Runtime deletion is not confirmed; stop before deleting dependencies")
    state["phase"] = "runtime_deleted"
    _write_state(state)
    state["phase"] = "delete_object_requested"
    _write_state(state)
    clients["s3"].delete_object(
        Bucket=state["artifact_bucket"], Key=state["artifact_key"], ExpectedBucketOwner=expected_account
    )
    _assert_object_absent(clients["s3"], state["artifact_bucket"], state["artifact_key"], expected_account)
    state["phase"] = "object_deleted"
    _write_state(state)
    state["phase"] = "delete_role_policy_requested"
    _write_state(state)
    clients["iam"].delete_role_policy(RoleName=ROLE_NAME, PolicyName=POLICY_NAME)
    _assert_inline_policy_absent(clients["iam"], ROLE_NAME, POLICY_NAME)
    state["phase"] = "role_policy_deleted"
    _write_state(state)
    state["phase"] = "delete_role_requested"
    _write_state(state)
    clients["iam"].delete_role(RoleName=ROLE_NAME)
    _assert_role_absent(clients["iam"], ROLE_NAME)
    state["phase"] = "role_deleted"
    _write_state(state)
    groups = clients["logs"].describe_log_groups(
        logGroupNamePrefix=state["log_group"], limit=2
    ).get("logGroups", [])
    exact_groups = [item for item in groups if item.get("logGroupName") == state["log_group"]]
    if len(exact_groups) == 1:
        clients["logs"].delete_log_group(logGroupName=state["log_group"])
        _assert_log_group_absent(clients["logs"], state["log_group"])
    elif exact_groups:
        raise RuntimeError("S06 log group is ambiguous; do not delete")
    state["phase"] = "resources_deleted"
    _write_state(state)
    print(json.dumps({
        "phase": state["phase"],
        "runtime_id": runtime_id,
        "artifact_bucket": state["artifact_bucket"],
        "artifact_key": state["artifact_key"],
        "role_name": ROLE_NAME,
        "log_group_deleted_after_private_record": state.get("log_group"),
        "metric_series_retained_until_service_expiry": True,
        "local_private_record": str(RAW_PATH),
    }, indent=2, ensure_ascii=False))


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    parser.add_argument("--expected-account", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    preflight_parser = commands.add_parser("preflight")
    preflight_parser.add_argument("--artifact-bucket", required=True)
    preflight_parser.add_argument("--zip", type=Path, required=True, dest="zip_path")
    commands.add_parser("deploy")
    commands.add_parser("invoke")
    commands.add_parser("readback")
    commands.add_parser("reconcile")
    commands.add_parser("cleanup")
    args = parser.parse_args()
    try:
        if args.command == "preflight":
            preflight(args.profile, args.expected_account, args.artifact_bucket, args.zip_path)
        elif args.command == "deploy":
            deploy(args.profile, args.expected_account)
        elif args.command == "invoke":
            invoke(args.profile, args.expected_account)
        elif args.command == "readback":
            readback(args.profile, args.expected_account)
        elif args.command == "reconcile":
            reconcile(args.profile, args.expected_account)
        else:
            cleanup(args.profile, args.expected_account)
    except (BotoCoreError, ClientError) as exc:
        response = getattr(exc, "response", {})
        code = response.get("Error", {}).get("Code", type(exc).__name__)
        print(f"AWS operation failed ({code}); stop and inspect exact current state without retrying.", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
