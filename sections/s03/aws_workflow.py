"""Small boto3 workflow for the S03 AgentCore Runtime demonstration."""

from __future__ import annotations

import argparse
import hashlib
import getpass
import json
import os
import secrets
import sys
import time
import uuid
import re
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

REGION = "ap-northeast-1"
RUN_TAG = "default"
RUNTIME_NAME = "udemy_agentcore_s03"
ROLE_NAME = "udemy-agentcore-s03-runtime"
BUCKET_PREFIX = "udemy-agentcore-s03-apne1-"
OBJECT_KEY = "s03/runtime.zip"
STATE_PATH = Path(__file__).resolve().parent / ".agentcore-s03-state.json"
LOG_GROUP_PREFIX = "/aws/bedrock-agentcore/runtimes/"
LOG_POLL_SECONDS = 5
LOG_WAIT_SECONDS = 120
LOG_MAX_PAGES_PER_SEARCH = 50


def _configure_run_tag(run_tag: str) -> None:
    """Select a bounded, isolated resource and local-state namespace."""
    if not re.fullmatch(r"[a-z0-9]{1,8}", run_tag):
        raise ValueError("Run tag must be 1-8 lowercase letters or digits")
    global RUN_TAG, RUNTIME_NAME, ROLE_NAME, BUCKET_PREFIX, OBJECT_KEY, STATE_PATH
    RUN_TAG = run_tag
    if run_tag == "default":
        RUNTIME_NAME = "udemy_agentcore_s03"
        ROLE_NAME = "udemy-agentcore-s03-runtime"
        BUCKET_PREFIX = "udemy-agentcore-s03-apne1-"
        OBJECT_KEY = "s03/runtime.zip"
        STATE_PATH = Path(__file__).resolve().parent / ".agentcore-s03-state.json"
        return
    RUNTIME_NAME = f"udemy_s03_{run_tag}"
    ROLE_NAME = f"udemy-s03-{run_tag}-runtime"
    BUCKET_PREFIX = f"udemy-s03-{run_tag}-apne1-"
    OBJECT_KEY = f"s03/{run_tag}/runtime.zip"
    STATE_PATH = Path(__file__).resolve().parent / f".agentcore-s03-state-{run_tag}.json"


def _configure_output_encoding() -> None:
    """Keep CLI output UTF-8 even when Windows redirects it through a pipe."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name)
        if stream is None:
            raise RuntimeError(f"Cannot configure {stream_name} for UTF-8 output")
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            raise RuntimeError(f"Cannot configure {stream_name} for UTF-8 output")
        reconfigure(encoding="utf-8", errors="backslashreplace")


def _clients(profile: str | None, region: str = REGION) -> tuple[Any, Any, Any, Any, Any]:
    session = boto3.Session(profile_name=profile, region_name=region)
    return (
        session.client("sts"),
        session.client("s3"),
        session.client("iam"),
        session.client("bedrock-agentcore-control"),
        session.client("bedrock-agentcore"),
    )


def _identity(sts: Any) -> str:
    return str(sts.get_caller_identity()["Account"])


def _guard(sts: Any, expected: str) -> None:
    if _identity(sts) != expected:
        raise RuntimeError("AWS account guard failed; no further AWS action was taken")


def _write_state(state: dict[str, Any]) -> None:
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary.replace(STATE_PATH)


def _read_state(sts: Any) -> dict[str, Any]:
    if not STATE_PATH.is_file():
        raise RuntimeError("Local S03 state is missing; deploy this demonstration first")
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    if state.get("schema_version") != 1:
        raise RuntimeError("Local S03 state version is unsupported; preserve it and inspect its exact resources")
    if state.get("run_tag", "default") != RUN_TAG:
        raise RuntimeError("Local S03 state belongs to a different run tag; preserve it and inspect")
    _guard(sts, str(state["account_id"]))
    return state


def _canonical(document: Any) -> str:
    if isinstance(document, str):
        document = json.loads(document)
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def _error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", "Unknown"))


def _zip_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _runtime_role_documents(account: str) -> tuple[dict[str, Any], dict[str, Any]]:
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {"aws:SourceAccount": account},
                "ArnLike": {
                    "aws:SourceArn": (
                        f"arn:aws:bedrock-agentcore:{REGION}:{account}:"
                        f"runtime/{RUNTIME_NAME}-*"
                    )
                },
            },
        }],
    }
    log_arn = (
        f"arn:aws:logs:{REGION}:{account}:log-group:"
        f"/aws/bedrock-agentcore/runtimes/{RUNTIME_NAME}-*"
    )
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["logs:DescribeLogStreams", "logs:CreateLogGroup", "logs:PutResourcePolicy"],
                "Resource": log_arn,
            },
            {
                "Effect": "Allow",
                "Action": "logs:DescribeLogGroups",
                "Resource": "*",
                "Condition": {"StringEquals": {"aws:RequestedRegion": REGION}},
            },
            {
                "Effect": "Allow",
                "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": f"{log_arn}:log-stream:*",
            },
            {
                "Effect": "Allow",
                "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                "Resource": "arn:aws:bedrock:ap-northeast-1::foundation-model/amazon.nova-lite-v1:0",
            },
        ],
    }
    return trust, policy


def _wait_runtime(control: Any, runtime_id: str, version: str | None = None) -> dict[str, Any]:
    deadline = time.monotonic() + 20 * 60
    while time.monotonic() < deadline:
        args: dict[str, Any] = {"agentRuntimeId": runtime_id}
        if version:
            args["agentRuntimeVersion"] = version
        state = control.get_agent_runtime(**args)
        status = state.get("status")
        if status == "READY":
            return state
        if status not in {"CREATING", "UPDATING"}:
            raise RuntimeError(f"Runtime did not become READY (status={status}); stop and inspect")
        time.sleep(10)
    raise RuntimeError("Runtime readiness timed out; stop and inspect before continuing")


def _create_or_recover_role(iam: Any, state: dict[str, Any], sts: Any) -> None:
    trust = state["role_trust"]
    expected_description = state["role_description"]
    role_name = state["role_name"]
    try:
        role = iam.get_role(RoleName=role_name)["Role"]
    except ClientError as exc:
        if _error_code(exc) != "NoSuchEntity":
            raise
        role = None

    if role is not None:
        if (
            role.get("Description") != expected_description
            or _canonical(role.get("AssumeRolePolicyDocument")) != _canonical(trust)
        ):
            raise RuntimeError("A role with the planned name exists but does not match this run; do not reuse or modify it")
        state["role_arn"] = role["Arn"]
        state["role_status"] = "created"
        _write_state(state)
        return

    state["role_status"] = "create_requested"
    _write_state(state)
    _guard(sts, state["account_id"])
    try:
        role = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description=expected_description,
        )["Role"]
    except ClientError as exc:
        if _error_code(exc) != "EntityAlreadyExists":
            raise
        # A previous CreateRole may have succeeded even if its response was lost.
        role = iam.get_role(RoleName=role_name)["Role"]
        if (
            role.get("Description") != expected_description
            or _canonical(role.get("AssumeRolePolicyDocument")) != _canonical(trust)
        ):
            raise RuntimeError("Role creation response was ambiguous and the role does not match this run") from None
    state["role_arn"] = role["Arn"]
    state["role_status"] = "created"
    _write_state(state)


def _ensure_role_policy(iam: Any, state: dict[str, Any], sts: Any) -> None:
    expected = state["role_policy"]
    try:
        existing = iam.get_role_policy(
            RoleName=state["role_name"], PolicyName="S03RuntimeExecution"
        )["PolicyDocument"]
    except ClientError as exc:
        if _error_code(exc) != "NoSuchEntity":
            raise
        existing = None
    if existing is not None and _canonical(existing) != _canonical(expected):
        raise RuntimeError("The planned role already has a different inline policy; do not overwrite it")
    if existing is None:
        state["role_policy_status"] = "put_requested"
        _write_state(state)
        _guard(sts, state["account_id"])
        iam.put_role_policy(
            RoleName=state["role_name"],
            PolicyName="S03RuntimeExecution",
            PolicyDocument=json.dumps(expected),
        )
    state["role_policy_status"] = "configured"
    _write_state(state)


def _apply_runtime_plan(control: Any, sts: Any, state: dict[str, Any]) -> None:
    if state.get("runtime_status") not in {"created", "ready"}:
        config = state["runtime_create_request"]
        # The same persisted token and request body recover a lost Create response.
        _guard(sts, state["account_id"])
        created = control.create_agent_runtime(**config)
        state["runtime_id"] = created["agentRuntimeId"]
        state["runtime_arn"] = created["agentRuntimeArn"]
        state["runtime_version"] = created["agentRuntimeVersion"]
        state["runtime_status"] = "created"
        _write_state(state)

    if state["runtime_status"] == "created":
        _wait_runtime(control, state["runtime_id"])
        update = state.get("runtime_update_request")
        if update is None:
            update = {
                "agentRuntimeId": state["runtime_id"],
                "agentRuntimeArtifact": state["runtime_create_request"]["agentRuntimeArtifact"],
                "roleArn": state["role_arn"],
                "platformVersion": "V2",
                "networkConfiguration": {"networkMode": "PUBLIC"},
                "protocolConfiguration": {"serverProtocol": "HTTP"},
                "environmentVariables": {},
                "metadataConfiguration": {"requireMMDSV2": True},
                "clientToken": f"s03-{uuid.uuid4()}-update",
            }
            state["runtime_update_request"] = update
            state["runtime_update_status"] = "requested"
            _write_state(state)
        if state.get("runtime_update_status") != "ready":
            # Reusing the saved token makes a retry safe if Update's response was lost.
            _guard(sts, state["account_id"])
            updated = control.update_agent_runtime(**state["runtime_update_request"])
            state["runtime_version"] = updated["agentRuntimeVersion"]
            state["runtime_update_status"] = "updating"
            _write_state(state)
            ready = _wait_runtime(control, state["runtime_id"], state["runtime_version"])
            if ready.get("metadataConfiguration", {}).get("requireMMDSV2") is not True:
                raise RuntimeError("Runtime is not confirmed with MMDSv2 enabled; do not invoke")
            state["runtime_update_status"] = "ready"
            state["runtime_status"] = "ready"
            _write_state(state)


def _resume_deploy(zip_path: Path, sts: Any, s3: Any, iam: Any, control: Any, state: dict[str, Any]) -> None:
    if zip_path.resolve() != Path(state["zip_path"]).resolve() or _zip_sha256(zip_path) != state["zip_sha256"]:
        raise RuntimeError("Use the exact ZIP saved in local state; do not replace the planned artifact during recovery")
    if state["bucket_status"] == "create_requested":
        raise RuntimeError(
            f"Bucket creation response is unresolved for the exact planned bucket {state['bucket']}. "
            "Inspect this name in the confirmed account/region and recover or clean up only that empty bucket; "
            "do not retry with a new name or delete local state yet."
        )
    if state["bucket_status"] == "planned":
        # The full intent, including exact bucket name and artifact hash, is saved before this first write.
        state["bucket_status"] = "create_requested"
        _write_state(state)
        _guard(sts, state["account_id"])
        s3.create_bucket(
            Bucket=state["bucket"],
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )
        state["bucket_status"] = "created"
        _write_state(state)

    if state["bucket_status"] in {"created", "upload_requested"}:
        head = None
        if state["bucket_status"] == "upload_requested":
            try:
                head = s3.head_object(
                    Bucket=state["bucket"], Key=state["object_key"],
                    ExpectedBucketOwner=state["account_id"],
                )
            except ClientError as exc:
                if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
                    raise
            if head and (
                head.get("Metadata", {}).get("sha256") != state["zip_sha256"]
                or head.get("ContentLength") != state["zip_size"]
            ):
                raise RuntimeError("The target ZIP object exists but does not match this run; do not overwrite it")
        if head is None:
            state["bucket_status"] = "upload_requested"
            _write_state(state)
            _guard(sts, state["account_id"])
            with zip_path.open("rb") as body:
                s3.put_object(
                    Bucket=state["bucket"],
                    Key=state["object_key"],
                    Body=body,
                    ExpectedBucketOwner=state["account_id"],
                    Metadata={"sha256": state["zip_sha256"]},
                )
        state["bucket_status"] = "uploaded"
        _write_state(state)

    if state["role_status"] != "created":
        trust, policy = _runtime_role_documents(state["account_id"])
        state["role_trust"] = trust
        state["role_policy"] = policy
        state["role_description"] = f"Udemy S03 Runtime role run {state['run_id']}"
        _write_state(state)
        _create_or_recover_role(iam, state, sts)
    _ensure_role_policy(iam, state, sts)
    # IAM role and inline policy propagation is eventually consistent.
    time.sleep(10)

    if state.get("runtime_status") == "planned":
        state["runtime_status"] = "create_requested"
        _write_state(state)
    _apply_runtime_plan(control, sts, state)
    print("Runtime is READY on V2 with MMDSv2 enabled.")
    print("The bucket, execution role, Runtime, and ZIP are retained for later sections; use the S03 cleanup instructions after their final dependent use.")
    print("Keep the local state file private; it contains account-specific resource identifiers.")


def deploy(zip_path: Path, profile: str | None) -> None:
    sts, s3, iam, control, _runtime = _clients(profile)
    if STATE_PATH.exists():
        state = _read_state(sts)
        if state.get("runtime_status") == "ready":
            raise RuntimeError("This deployment is already READY; use invoke or stop instead of deploy")
        _resume_deploy(zip_path, sts, s3, iam, control, state)
        return
    if not zip_path.is_file() or zip_path.stat().st_size > 250 * 1024 * 1024:
        raise RuntimeError("Choose an existing deployment ZIP no larger than 250 MB")
    account = _identity(sts)
    expected = getpass.getpass("確認済みの学習用 AWS account ID (入力は表示されません): ").strip()
    if len(expected) != 12 or not expected.isdigit() or expected != account:
        raise RuntimeError("AWS account did not match the confirmed target; no resource was created")

    run_id = uuid.uuid4().hex
    bucket = (BUCKET_PREFIX + secrets.token_hex(16)).lower()
    trust, policy = _runtime_role_documents(account)
    role_arn = f"arn:aws:iam::{account}:role/{ROLE_NAME}"
    artifact = {
        "codeConfiguration": {
            "code": {"s3": {"bucket": bucket, "prefix": OBJECT_KEY}},
            "entryPoint": ["runtime_app.py"],
            "runtime": "PYTHON_3_13",
        }
    }
    runtime_request = {
        "agentRuntimeName": RUNTIME_NAME,
        "roleArn": role_arn,
        "platformVersion": "V2",
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "agentRuntimeArtifact": artifact,
        "environmentVariables": {},
        "clientToken": f"s03-{uuid.uuid4()}-create",
    }
    state: dict[str, Any] = {
        "schema_version": 1,
        "run_tag": RUN_TAG,
        "run_id": run_id,
        "account_id": account,
        "region": REGION,
        "zip_path": str(zip_path.resolve()),
        "zip_sha256": _zip_sha256(zip_path),
        "zip_size": zip_path.stat().st_size,
        "bucket": bucket,
        "bucket_status": "planned",
        "object_key": OBJECT_KEY,
        "role_name": ROLE_NAME,
        "role_description": f"Udemy S03 Runtime role run {run_id}",
        "role_status": "planned",
        "role_policy_status": "planned",
        "role_trust": trust,
        "role_policy": policy,
        "runtime_name": RUNTIME_NAME,
        "runtime_status": "planned",
        "runtime_create_request": runtime_request,
        "runtime_client_token": runtime_request["clientToken"],
        "sessions": [],
    }
    # Persist all three exact resource intents before the first AWS write.
    _write_state(state)
    _resume_deploy(zip_path, sts, s3, iam, control, state)


def _invoke(runtime: Any, state: dict[str, Any], session_id: str, prompt: str) -> dict[str, Any]:
    response = runtime.invoke_agent_runtime(
        agentRuntimeArn=state["runtime_arn"],
        qualifier="DEFAULT",
        runtimeSessionId=session_id,
        contentType="application/json",
        accept="application/json",
        payload=json.dumps({"prompt": prompt}, ensure_ascii=False).encode("utf-8"),
    )
    body = response["response"].read()
    document = json.loads(body.decode("utf-8"))
    return document


def invoke(profile: str | None) -> None:
    sts, _s3, _iam, _control, runtime = _clients(profile)
    state = _read_state(sts)
    first = str(uuid.uuid4())
    second = first
    fresh = str(uuid.uuid4()) + "-new-session-123456789"
    prompts = [
        "catalog-api の5xxを調べる順序を教えてください。",
        "toolが返した確認順を短く振り返ってください。",
        "toolが返した確認順を短く振り返ってください。",
    ]
    session_ids = [first, second, fresh]
    steps = ["same-session-1", "same-session-2", "new-session"]
    results: list[dict[str, Any]] = []
    for step, session_id, prompt in zip(steps, session_ids, prompts, strict=True):
        _guard(sts, state["account_id"])
        state["sessions"] = list(dict.fromkeys([*state.get("sessions", []), session_id]))
        _write_state(state)
        print(json.dumps({
            "event": "request",
            "step": step,
            "session_id": session_id,
            "prompt": prompt,
        }, ensure_ascii=False), flush=True)
        result = _invoke(runtime, state, session_id, prompt)
        if not result.get("invocation_id"):
            raise RuntimeError("Invocation response did not include an invocation ID")
        results.append(result)
        state["invocations"] = list(dict.fromkeys([*state.get("invocations", []), result["invocation_id"]]))
        _write_state(state)
        print(json.dumps({
            "event": "response",
            "step": step,
            "session_id": session_id,
            "result": result,
        }, ensure_ascii=False), flush=True)
    print("Compare the two same-session responses with the new-session response; wording is not a pass/fail criterion.")
    print("Use each invocation_id to find its agent_invocation event in CloudWatch Logs.")


def status(profile: str | None) -> None:
    sts, _s3, _iam, control, _runtime = _clients(profile)
    state = _read_state(sts)
    _guard(sts, state["account_id"])
    current = control.get_agent_runtime(agentRuntimeId=state["runtime_id"])
    metadata = current.get("metadataConfiguration") or {}
    print(json.dumps({
        "runtime_id": current.get("agentRuntimeId", state["runtime_id"]),
        "version": current.get("agentRuntimeVersion"),
        "status": current.get("status"),
        "platform_version": current.get("platformVersion"),
        "require_mmds_v2": metadata.get("requireMMDSV2"),
        "read_only_confirmation": True,
    }, ensure_ascii=False))


def _matching_agent_invocation(event: dict[str, Any], invocation_id: str) -> bool:
    message = event.get("message", "")
    if not isinstance(message, str):
        return False
    try:
        document = json.loads(message)
    except (TypeError, ValueError):
        return False
    return (
        isinstance(document, dict)
        and document.get("event") == "agent_invocation"
        and document.get("invocation_id") == invocation_id
    )


def _find_invocation_events(logs_client: Any, log_group: str, invocation_id: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    next_token: str | None = None
    seen_tokens: set[str] = set()
    for _page in range(LOG_MAX_PAGES_PER_SEARCH):
        request: dict[str, Any] = {
            "logGroupName": log_group,
            "filterPattern": f'"{invocation_id}"',
            "limit": 100,
        }
        if next_token is not None:
            request["nextToken"] = next_token
        try:
            response = logs_client.filter_log_events(**request)
        except ClientError as exc:
            if next_token is not None and _error_code(exc) == "InvalidParameterException":
                raise RuntimeError("CloudWatch log pagination token was rejected or expired; retry logs later") from None
            raise
        events.extend(
            event for event in response.get("events", [])
            if _matching_agent_invocation(event, invocation_id)
        )
        candidate = response.get("nextToken")
        if candidate is None:
            return events
        if not isinstance(candidate, str) or not candidate:
            raise RuntimeError("CloudWatch log pagination returned an invalid token; stop and inspect")
        if candidate in seen_tokens:
            raise RuntimeError("CloudWatch log pagination repeated a token; stop and retry logs later")
        seen_tokens.add(candidate)
        next_token = candidate
    raise RuntimeError("CloudWatch log pagination reached its safety limit; retry logs later")


def logs(profile: str | None, wait_seconds: int = LOG_WAIT_SECONDS) -> None:
    sts, _s3, _iam, _control, _runtime = _clients(profile)
    state = _read_state(sts)
    invocation_ids = state.get("invocations", [])
    if not invocation_ids:
        raise RuntimeError("No saved invocation IDs; run invoke first")
    logs_client = boto3.Session(profile_name=profile, region_name=REGION).client("logs")
    log_group = f"{LOG_GROUP_PREFIX}{state['runtime_id']}-DEFAULT"
    deadline = time.monotonic() + wait_seconds
    found: dict[str, list[dict[str, Any]]] = {item: [] for item in invocation_ids}
    while True:
        _guard(sts, state["account_id"])
        for invocation_id in invocation_ids:
            if found[invocation_id]:
                continue
            try:
                found[invocation_id] = _find_invocation_events(
                    logs_client, log_group, invocation_id
                )
            except ClientError as exc:
                if _error_code(exc) == "ResourceNotFoundException" and time.monotonic() < deadline:
                    continue
                if _error_code(exc) == "ResourceNotFoundException":
                    break
                raise
        if all(found.values()) or time.monotonic() >= deadline:
            break
        time.sleep(min(LOG_POLL_SECONDS, max(0, deadline - time.monotonic())))
    missing = [item for item, events in found.items() if not events]
    if missing:
        raise RuntimeError(f"No matching agent_invocation event yet for {len(missing)} saved invocation(s); retry logs later")
    for invocation_id, events in found.items():
        for event in events:
            try:
                document = json.loads(event.get("message", "{}"))
            except (TypeError, ValueError):
                document = {}
            print(json.dumps({
                "invocation_id": invocation_id,
                "timestamp": event.get("timestamp"),
                "event": "agent_invocation",
                "status": document.get("status"),
                "tool_call_count": document.get("tool_call_count"),
                "tool_names": document.get("tool_names", []),
            }, ensure_ascii=False))
    print("Read-only lookup used only the saved invocation IDs and this Runtime’s exact log group.")


def stop(profile: str | None) -> None:
    sts, _s3, _iam, _control, runtime = _clients(profile)
    state = _read_state(sts)
    for session_id in state.get("sessions", []):
        _guard(sts, state["account_id"])
        try:
            runtime.stop_runtime_session(
                agentRuntimeArn=state["runtime_arn"],
                runtimeSessionId=session_id,
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "Unknown")
            if code != "ResourceNotFoundException":
                raise RuntimeError(f"Session stop failed ({code}); inspect before continuing") from None
        print("Stopped or already-ended demonstration session.")
    state["sessions"] = []
    _write_state(state)


def main() -> int:
    _configure_output_encoding()
    parser = argparse.ArgumentParser(description="Deploy and demonstrate the S03 AgentCore Runtime using boto3.")
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE"), help="AWS CLI/SSO profile name")
    parser.add_argument("--run-tag", default="default", help="isolated resource/state namespace (1-8 lowercase letters or digits)")
    commands = parser.add_subparsers(dest="command", required=True)
    deploy_parser = commands.add_parser("deploy", help="create the S03 bucket, role, and Runtime")
    deploy_parser.add_argument("--zip", required=True, type=Path, dest="zip_path")
    commands.add_parser("invoke", help="compare same-session and new-session requests")
    commands.add_parser("status", help="read the exact Runtime status")
    commands.add_parser("logs", help="find this demonstration's saved invocation events")
    commands.add_parser("stop", help="stop the active sessions created by this demonstration")
    args = parser.parse_args()
    try:
        _configure_run_tag(args.run_tag)
        if args.command == "deploy":
            deploy(args.zip_path, args.profile)
        elif args.command == "invoke":
            invoke(args.profile)
        elif args.command == "status":
            status(args.profile)
        elif args.command == "logs":
            logs(args.profile)
        else:
            stop(args.profile)
    except (BotoCoreError, ClientError) as exc:
        code = exc.response.get("Error", {}).get("Code", type(exc).__name__) if isinstance(exc, ClientError) else type(exc).__name__
        print(f"AWS operation failed ({code}); stop and inspect the current state before retrying.", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
