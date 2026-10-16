"""Provision and operate the bounded S04 AgentCore Gateway demonstration."""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from datetime import datetime, timedelta, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from cloudwatch_reader import TOOL_SCHEMA

REGION = "ap-northeast-1"
STATE_PATH = Path(__file__).resolve().parent / ".agentcore-s04-state.json"
PREFLIGHT_PATH = Path(__file__).resolve().parent / ".agentcore-s04-preflight.json"
SEED_STATE_PATH = Path(__file__).resolve().with_name(".s04-seed-state.json")
S03_STATE_PATH = Path(__file__).resolve().parents[1] / "s03" / ".agentcore-s03-state.json"
LAMBDA_NAME = "udemy-agentcore-s04-cw-reader"
LAMBDA_ROLE = "udemy-agentcore-s04-cw-reader-role"
GATEWAY_NAME = "udemy-agentcore-s04-gateway"
GATEWAY_ROLE = "udemy-agentcore-s04-gateway-role"
GATEWAY_TARGET_NAME = "udemy-agentcore-s04-cw-target"
GATEWAY_TOOL_NAME = f"{GATEWAY_TARGET_NAME}___read_synthetic_cloudwatch"
LOG_GROUP = "/udemy/agentcore/s04/synthetic/catalog-api"
LOG_RETENTION_DAYS = 7
MAX_BUDGET_USD = 1.00
STOP_BUDGET_USD = 0.80
MAX_VALIDATION_RUNS = 2
MAX_POSITIVE_CALLS_PER_RUN = 2


def _created_lambda_configuration(response: dict[str, Any]) -> dict[str, Any]:
    """Validate and return the direct Lambda CreateFunction response shape."""
    if not isinstance(response, dict) or not response.get("FunctionArn"):
        raise RuntimeError("Lambda CreateFunction response is missing FunctionArn; inspect before continuing")
    return response


def _mcp_endpoint_url(endpoint: str) -> str:
    """Ensure a gateway URL has exactly one terminal MCP route suffix."""
    normalized = endpoint.rstrip("/")
    if not normalized:
        raise RuntimeError("Gateway endpoint is empty; stop and inspect")
    return normalized if normalized.endswith("/mcp") else f"{normalized}/mcp"


def _gateway_tool_schema() -> dict[str, Any]:
    """Project the Lambda's richer JSON Schema onto AgentCore's API schema."""
    schema = copy.deepcopy(TOOL_SCHEMA)
    tool = schema["inlinePayload"][0]
    properties = tool["inputSchema"]["properties"]
    # AgentCore's API model accepts the structural JSON Schema fields only;
    # retain these constraints in descriptions while Lambda enforces them.
    properties["data_type"].pop("enum", None)
    properties["data_type"]["description"] = "Must be exactly logs or metrics."
    properties["max_results"].pop("minimum", None)
    properties["max_results"].pop("maximum", None)
    properties["max_results"]["description"] = "Integer from 1 through 50."
    return schema


def _recover_gateway_from_summary(control: Any, summary: dict[str, Any], expected_role: str) -> dict[str, Any]:
    """Resolve an exact prior Gateway from its list summary, then validate details."""
    gateway_id = summary.get("gatewayId")
    if not gateway_id:
        raise RuntimeError("Gateway summary has no identifier; stop and inspect")
    gateway = control.get_gateway(gatewayIdentifier=gateway_id)
    if (
        gateway.get("gatewayId") != gateway_id
        or gateway.get("name") != GATEWAY_NAME
        or gateway.get("roleArn") != expected_role
        or gateway.get("protocolType") != "MCP"
        or gateway.get("authorizerType") != "AWS_IAM"
        or not gateway.get("gatewayArn")
    ):
        raise RuntimeError("Recovered Gateway does not match the exact planned name/role/MCP/AWS_IAM configuration")
    return gateway


def _clients(profile: str | None) -> dict[str, Any]:
    session = boto3.Session(profile_name=profile, region_name=REGION)
    names = ("sts", "iam", "lambda", "logs", "cloudwatch", "s3", "bedrock-agentcore-control", "bedrock-agentcore")
    result = {name: session.client(name) for name in names}
    result["bedrock-agentcore"] = session.client("bedrock-agentcore", config=Config(read_timeout=900, connect_timeout=10, retries={"total_max_attempts": 1}))
    return result


def _save(state: dict[str, Any]) -> None:
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(STATE_PATH)


def _state(clients: dict[str, Any]) -> dict[str, Any]:
    if not STATE_PATH.is_file():
        raise RuntimeError("S04 state is missing; run deploy first and preserve this state file")
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    if state.get("schema_version") != 1 or state.get("region") != REGION:
        raise RuntimeError("S04 state is unsupported or belongs to another region; preserve and inspect it")
    _guard(clients["sts"], state["account_id"])
    return state


def _session_stop_confirmed(state: dict[str, Any], session_id: str) -> bool:
    """Return whether the latest recorded stop result confirms this session ended."""
    attempts = [item for item in state.get("stop_attempts", []) if item.get("session_id") == session_id]
    return bool(attempts) and attempts[-1].get("status") in {"stopped", "not_found_after_successful_invoke"}


def _update_stop_confirmed(state: dict[str, Any], session_id: str) -> bool:
    """Artifact updates require the latest exact-session stop to return both HTTP and API 200."""
    attempts = [item for item in state.get("stop_attempts", []) if item.get("session_id") == session_id]
    if not attempts:
        return False
    latest = attempts[-1]
    request_id = latest.get("request_id")
    return (latest.get("status") == "stopped" and latest.get("status_code") == 200
            and latest.get("http_status_code") == 200 and isinstance(request_id, str) and bool(request_id.strip()))


def _update_failed_attempts_reconciled(state: dict[str, Any]) -> bool:
    """Do not rewrite/reset a requested invocation; require its exact failed-log and stop evidence."""
    for attempt in state.get("positive_attempts", []):
        if attempt.get("status") != "requested":
            continue
        session_id = attempt.get("session_id")
        matches = [item for item in state.get("reconciliations", []) if item.get("session_id") == session_id]
        if len(matches) != 1 or not _update_stop_confirmed(state, session_id or ""):
            return False
        proof = matches[0].get("stop_response", {})
        if (matches[0].get("runtime_status") != 500 or matches[0].get("error_type") != "RuntimeError"
                or matches[0].get("tool_call_count") != 0 or matches[0].get("tool_names") != []
                or proof.get("status") != "stopped" or proof.get("status_code") != 200
                or proof.get("http_status_code") != 200 or not proof.get("request_id")):
            return False
    return True


def _ensure_validation_runs(state: dict[str, Any]) -> list[dict[str, Any]]:
    runs = state.setdefault("validation_runs", [])
    if not runs and state.get("positive_session"):
        session = state["positive_session"]
        attempts = [item for item in state.get("positive_attempts", []) if item.get("session_id") == session]
        started = state.get("first_invoke_started_epoch") or min((item.get("at_epoch", int(time.time())) for item in attempts), default=int(time.time()))
        runs.append({"session_id": session, "started_epoch": started,
                     "stop_deadline_epoch": state.get("stop_deadline_epoch", started + 300),
                     "attempt_steps": [item.get("step") for item in attempts]})
    return runs


def _positive_query_window(seed: dict[str, Any], now: datetime | None = None) -> tuple[str, str, str]:
    """Derive an explicit bounded UTC query from the single saved seed event."""
    if seed.get("status") != "metric_written" or seed.get("region") != REGION:
        raise RuntimeError("A completed S04 seed state in ap-northeast-1 is required before invoking")
    if seed.get("service", "catalog-api") != "catalog-api":
        raise RuntimeError("S04 seed service does not match catalog-api")
    request_id = seed.get("request_id")
    if not isinstance(request_id, str) or not request_id.startswith("s04-demo-"):
        raise RuntimeError("S04 seed state is missing its exact synthetic request_id")
    timestamp = seed.get("timestamp")
    try:
        seeded_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError) as exc:
        raise RuntimeError("S04 seed timestamp is invalid; inspect the seed state") from exc
    if seeded_at.tzinfo is None or seeded_at.utcoffset() != timedelta(0):
        raise RuntimeError("S04 seed timestamp must be UTC")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() != timedelta(0):
        raise ValueError("validation clock must be timezone-aware UTC")
    if seeded_at > current or current - seeded_at > timedelta(days=7):
        raise RuntimeError("The saved synthetic seed is not within the allowed 7-day retention window")
    seed_minute = seeded_at.replace(second=0, microsecond=0)
    start = seed_minute - timedelta(minutes=1)
    end = min(seed_minute + timedelta(minutes=2), current).replace(microsecond=0)
    return (start.isoformat(timespec="seconds").replace("+00:00", "Z"),
            end.isoformat(timespec="seconds").replace("+00:00", "Z"), request_id)


def _has_expected_positive_trace(result: dict[str, Any], data_type: str, start: str, end: str) -> bool:
    """Require a successful S04 tool result for the exact seeded query window."""
    trace = result.get("trace")
    if not isinstance(trace, list):
        return False
    for item in trace:
        if not isinstance(item, dict) or item.get("name") != "read_synthetic_cloudwatch":
            continue
        arguments = item.get("arguments")
        tool_result = item.get("result")
        if not isinstance(arguments, dict) or not isinstance(tool_result, dict):
            continue
        if (arguments.get("data_type") == data_type
                and arguments.get("service") == "catalog-api"
                and arguments.get("start_time") == start
                and arguments.get("end_time") == end
                and arguments.get("max_results") == 50
                and tool_result.get("ok") is True):
            return True
    return False


def _is_expected_negative_response(result: dict[str, Any], text: str) -> bool:
    """Accept only the exact successful MCP response from the service guard."""
    if result.get("status") != "success":
        return False
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return False
    return (isinstance(document, dict) and document.get("ok") is False
            and document.get("error") == "service is outside the S04 synthetic dataset")


def _guard(sts: Any, expected: str) -> None:
    actual = sts.get_caller_identity()["Account"]
    if actual != expected:
        raise RuntimeError("AWS account guard failed; no further AWS action was taken")


def _confirm(clients: dict[str, Any], expected: str, region: str = REGION) -> str:
    if len(expected) != 12 or not expected.isdigit() or region != REGION:
        raise ValueError("Pass the confirmed 12-digit account and exact ap-northeast-1 region")
    _guard(clients["sts"], expected)
    print(f"Confirmed account {expected}, region {region}.")
    return expected


def _effective_principal_arn(identity_arn: str, account: str, iam: Any) -> str:
    """Resolve STS assumed-role identities to the actual IAM role ARN, retaining SSO paths."""
    if ":assumed-role/" not in identity_arn:
        return identity_arn
    assumed = identity_arn.split(":assumed-role/", 1)[1]
    role_name = assumed.split("/", 1)[0]
    if not role_name:
        raise RuntimeError("NO-GO: caller assumed-role ARN has no role name")
    role = iam.get_role(RoleName=role_name)["Role"]
    arn = role.get("Arn", "")
    expected_prefix = f"arn:aws:iam::{account}:role/"
    if not arn.startswith(expected_prefix) or arn.rsplit("/", 1)[-1] != role_name:
        raise RuntimeError("NO-GO: resolved IAM role ARN does not match the exact caller account and assumed role")
    return arn


def _safe_runtime_snapshot(runtime: dict[str, Any]) -> dict[str, Any]:
    """Keep only required Runtime configuration and refuse unknown live environment values."""
    environment = runtime.get("environmentVariables") or {}
    if not isinstance(environment, dict) or environment:
        raise RuntimeError("Existing Runtime has non-empty environment variables; refusing to persist or overwrite them")
    allowed = (
        "agentRuntimeArn", "agentRuntimeId", "agentRuntimeArtifact", "roleArn", "platformVersion",
        "networkConfiguration", "protocolConfiguration", "lifecycleConfiguration", "metadataConfiguration",
        "status", "agentRuntimeVersion",
    )
    snapshot = {key: copy.deepcopy(runtime[key]) for key in allowed if key in runtime}
    snapshot["environmentVariables"] = {}
    return snapshot


def _runtime_lifecycle(runtime: dict[str, Any]) -> dict[str, Any]:
    lifecycle = runtime.get("lifecycleConfiguration", {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800})
    idle = lifecycle.get("idleRuntimeSessionTimeout")
    maximum = lifecycle.get("maxLifetime")
    if (not isinstance(idle, int) or not isinstance(maximum, int)
            or idle <= 0 or maximum <= 0 or idle > maximum or idle > 900):
        raise RuntimeError("Runtime lifecycle no longer meets the reviewed 15-minute idle bound; stop and reassess cost")
    return copy.deepcopy(lifecycle)


def _check_deployed_lifecycle(control: Any, state: dict[str, Any]) -> None:
    active = state.get("active_runtime_artifact") or {}
    expected_request = active.get("update_request") or state.get("runtime_update_request", {})
    expected = expected_request.get("lifecycleConfiguration")
    if not isinstance(expected, dict):
        raise RuntimeError("Saved expected Runtime lifecycle configuration is missing; no invoke was sent")
    live = control.get_agent_runtime(agentRuntimeId=state["runtime_id"])
    expected_version = active.get("runtime_version") or state.get("runtime_version")
    expected_artifact = expected_request.get("agentRuntimeArtifact")
    drift = live.get("status") != "READY" or _runtime_lifecycle(live) != expected
    if active:
        drift = drift or (expected_version and live.get("agentRuntimeVersion") != expected_version)
        drift = drift or (expected_artifact and live.get("agentRuntimeArtifact") != expected_artifact)
        drift = drift or live.get("roleArn") != expected_request.get("roleArn")
        drift = drift or (live.get("environmentVariables") or {}) != expected_request.get("environmentVariables", {})
    if drift:
        raise RuntimeError("Live Runtime differs from the saved S04 deployment (version, artifact, role, environment, lifecycle, or readiness); no invoke was sent")


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _record(state: dict[str, Any], key: str, request: dict[str, Any]) -> None:
    state.setdefault("operations", {})[key] = {"status": "requested", "request": request}
    _save(state)


def _record_client_error(state: dict[str, Any], key: str, operation: str, exc: ClientError) -> None:
    error = exc.response.get("Error", {})
    metadata = exc.response.get("ResponseMetadata", {})
    message = " ".join(str(error.get("Message", "")).split())[:500]
    message = re.sub(r"\b\d{12}\b", "[account]", message)
    message = re.sub(r"arn:aws:[^\s,;]+", "[arn]", message)
    diagnostic = {
        "operation": operation,
        "code": str(error.get("Code", type(exc).__name__)),
        "request_id": metadata.get("RequestId"),
        "message": message,
    }
    state.setdefault("operations", {}).setdefault(key, {}).update({"status": "failed", "error": diagnostic})
    _save(state)
    print(json.dumps({"aws_operation": operation, **diagnostic}, ensure_ascii=False), file=sys.stderr)


def _all_items(client: Any, method: str, field: str, **kwargs: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    token: str | None = None
    seen: set[str] = set()
    while True:
        request = dict(kwargs)
        if token:
            request["nextToken"] = token
        response = getattr(client, method)(**request)
        items.extend(response.get(field, []))
        candidate = response.get("nextToken")
        if not candidate:
            return items
        if not isinstance(candidate, str) or candidate in seen:
            raise RuntimeError(f"{method} returned an invalid or repeated pagination token")
        seen.add(candidate)
        token = candidate


def _ensure_role(iam: Any, state: dict[str, Any], key: str, name: str, trust: dict[str, Any], policy: dict[str, Any]) -> str:
    arn = f"arn:aws:iam::{state['account_id']}:role/{name}"
    policy_name = "S04CloudWatchRead" if key == "lambda" else "S04InvokeReaderLambda"
    try:
        existing = iam.get_role(RoleName=name)["Role"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "NoSuchEntity":
            raise
        existing = None
    description = f"S04 demonstration {key}"
    if existing:
        if (existing.get("Description") != description
                or json.dumps(existing.get("AssumeRolePolicyDocument"), sort_keys=True) != json.dumps(trust, sort_keys=True)
                or f"{key}_role" not in state.get("operations", {})):
            raise RuntimeError(f"{name} exists but is not owned by this S04 run; stopped")
        arn = existing["Arn"]
    else:
        _record(state, f"{key}_role", {"name": name, "trust": trust, "description": description})
        role = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust), Description=description)["Role"]
        arn = role["Arn"]
    try:
        current = iam.get_role_policy(RoleName=name, PolicyName=policy_name)["PolicyDocument"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "NoSuchEntity":
            raise
        current = None
    if current is not None and json.dumps(current, sort_keys=True) != json.dumps(policy, sort_keys=True):
        raise RuntimeError(f"{name} has a different inline policy; refusing to overwrite it")
    if current is not None and f"{key}_policy" not in state.get("operations", {}):
        raise RuntimeError(f"{name} policy exists without this run's recorded create intent")
    if current is None:
        _record(state, f"{key}_policy", {"name": name, "policy_name": policy_name, "policy": policy})
        iam.put_role_policy(RoleName=name, PolicyName=policy_name, PolicyDocument=json.dumps(policy))
    state.setdefault("operations", {}).setdefault(f"{key}_role", {})["status"] = "created"
    state.setdefault("operations", {}).setdefault(f"{key}_policy", {})["status"] = "configured"
    state.setdefault("owned", {})[f"{key}_role"] = name
    _save(state)
    return arn


def _s03_state(clients: dict[str, Any], account: str) -> dict[str, Any]:
    if not S03_STATE_PATH.is_file():
        raise RuntimeError("S03 local state is required to update the existing Runtime")
    state = json.loads(S03_STATE_PATH.read_text(encoding="utf-8"))
    runtime_id = state.get("runtime_id")
    if state.get("account_id") != account or state.get("region") != REGION or not isinstance(runtime_id, str) or not runtime_id:
        raise RuntimeError("S03 state does not identify the existing Runtime/account/region")
    runtime = clients["bedrock-agentcore-control"].get_agent_runtime(agentRuntimeId=runtime_id)
    if runtime.get("status") != "READY":
        raise RuntimeError("Existing S03 Runtime is not READY; stop and inspect")
    return state


def preflight(expected_account: str, expected_region: str, expected_runtime_id: str, profile: str | None) -> None:
    clients = _clients(profile)
    account = _confirm(clients, expected_account, expected_region)
    s03 = _s03_state(clients, account)
    if s03["runtime_id"] != expected_runtime_id:
        raise RuntimeError("NO-GO: typed Runtime target differs from the exact Runtime in S03 local state")
    runtime_live = clients["bedrock-agentcore-control"].get_agent_runtime(agentRuntimeId=s03["runtime_id"])
    if runtime_live.get("status") != "READY":
        raise RuntimeError("Existing S03 Runtime is not READY; stop and inspect")
    live_lifecycle = _runtime_lifecycle(runtime_live)
    identity = clients["sts"].get_caller_identity()
    principal = _effective_principal_arn(identity["Arn"], account, clients["iam"])
    runtime_arn = s03.get("runtime_arn") or f"arn:aws:bedrock-agentcore:{REGION}:{account}:runtime/{s03['runtime_id']}"
    permission = clients["iam"].simulate_principal_policy(PolicySourceArn=principal,
        ActionNames=["bedrock-agentcore:StopRuntimeSession"], ResourceArns=[runtime_arn])
    evaluation = permission.get("EvaluationResults", [{}])[0]
    decision = evaluation.get("EvalDecision")
    orgs_allowed = evaluation.get("OrganizationsDecisionDetail", {}).get("AllowedByOrganizations")
    missing_context = evaluation.get("MissingContextValues", [])
    if decision != "allowed" or orgs_allowed is not True or missing_context:
        raise RuntimeError("NO-GO: effective StopRuntimeSession permission was not confirmed for this exact Runtime")
    inventory_checked_targets = _inventory_exact_names(clients)
    snapshot = {"schema_version": 1, "account_id": account, "region": REGION,
        "runtime_id": s03["runtime_id"], "inventory_completed_epoch": int(time.time()),
        "inventory_checked_targets": inventory_checked_targets}
    temporary = PREFLIGHT_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    temporary.replace(PREFLIGHT_PATH)
    print(json.dumps({"account_confirmed": account, "region": REGION, "existing_runtime": s03["runtime_id"],
                      "runtime_status": "READY", "maximum_projected_cost_usd": MAX_BUDGET_USD,
                      "stop_threshold_usd": STOP_BUDGET_USD, "stop_permission": decision,
                      "runtime_exposure_bound_minutes": 35,
                      "runtime_lifecycle_configuration": live_lifecycle,
                      "inventory_checked_targets": inventory_checked_targets,
                      "organizations_allowed": orgs_allowed, "missing_context": missing_context,
                      "maximum_runtime_allocation": {"vCPU": 2, "memory_GiB": 8}, "read_only": True}, indent=2))


def _role_documents(account: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    lambda_trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    lambda_policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "logs:FilterLogEvents", "Resource": f"arn:aws:logs:{REGION}:{account}:log-group:{LOG_GROUP}:*"},
        {"Effect": "Allow", "Action": "cloudwatch:GetMetricData", "Resource": "*", "Condition": {"StringEquals": {"aws:RequestedRegion": REGION}}},
    ]}
    gateway_trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"}, "Action": "sts:AssumeRole", "Condition": {"StringEquals": {"aws:SourceAccount": account}, "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{REGION}:{account}:gateway/{GATEWAY_NAME}-*"}}}]}
    gateway_policy = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": f"arn:aws:lambda:{REGION}:{account}:function:{LAMBDA_NAME}"}]}
    return lambda_trust, lambda_policy, gateway_trust, gateway_policy


def _inventory_exact_names(clients: dict[str, Any]) -> list[str]:
    collisions = []
    checked = [f"Lambda {LAMBDA_NAME}", f"IAM role {LAMBDA_ROLE}", f"IAM role {GATEWAY_ROLE}",
               f"Gateway {GATEWAY_NAME}", f"Log group {LOG_GROUP}",
               "CloudWatch metric Udemy/AgentCoreS04/Http5xxCount Service=catalog-api"]
    try:
        clients["lambda"].get_function(FunctionName=LAMBDA_NAME)
        collisions.append(f"Lambda {LAMBDA_NAME}")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise
    for role_name in (LAMBDA_ROLE, GATEWAY_ROLE):
        try:
            clients["iam"].get_role(RoleName=role_name)
            collisions.append(f"IAM role {role_name}")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "NoSuchEntity":
                raise
    if any(item.get("name") == GATEWAY_NAME for item in _all_items(clients["bedrock-agentcore-control"], "list_gateways", "items")):
        collisions.append(f"Gateway {GATEWAY_NAME}")
    if any(item.get("logGroupName") == LOG_GROUP for item in _all_items(clients["logs"], "describe_log_groups", "logGroups", logGroupNamePrefix=LOG_GROUP)):
        collisions.append(f"Log group {LOG_GROUP}")
    metrics = _all_items(clients["cloudwatch"], "list_metrics", "Metrics", Namespace="Udemy/AgentCoreS04", MetricName="Http5xxCount")
    if any(metric.get("Dimensions", []) == [{"Name": "Service", "Value": "catalog-api"}] for metric in metrics):
        collisions.append("CloudWatch metric Udemy/AgentCoreS04/Http5xxCount Service=catalog-api")
    if collisions:
        raise RuntimeError("NO-GO: exact S04 names already exist; do not reuse them: " + "; ".join(collisions))
    return checked


def _ensure_runtime_gateway_permission(iam: Any, state: dict[str, Any], role_name: str) -> None:
    policy_name = "S04GatewayInvoke"
    expected = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Action": "bedrock-agentcore:InvokeGateway", "Resource": state["gateway_arn"]
    }]}
    try:
        current = iam.get_role_policy(RoleName=role_name, PolicyName=policy_name)["PolicyDocument"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "NoSuchEntity":
            raise
        current = None
    if current is not None and json.dumps(current, sort_keys=True) != json.dumps(expected, sort_keys=True):
        raise RuntimeError("The Runtime role's S04 inline policy differs from this exact Gateway; refusing to overwrite")
    if current is None:
        _record(state, "runtime_gateway_permission", {"role_name": role_name, "policy": expected})
        iam.put_role_policy(RoleName=role_name, PolicyName=policy_name, PolicyDocument=json.dumps(expected))
    state["runtime_role_name"] = role_name
    _save(state)


def deploy(expected_account: str, expected_region: str, expected_runtime_id: str, lambda_zip: Path, runtime_zip: Path, profile: str | None) -> None:
    clients = _clients(profile)
    account = _confirm(clients, expected_account, expected_region)
    if not lambda_zip.is_file() or not runtime_zip.is_file():
        raise ValueError("Both package_artifacts.py ZIP outputs must exist")
    if STATE_PATH.exists():
        state = _state(clients)
        if state.get("runtime_id") != expected_runtime_id:
            raise RuntimeError("Typed Runtime target differs from saved S04 state")
        if state.get("phase") == "ready":
            raise RuntimeError("S04 is already READY; inspect status or continue the documented workflow")
        if _sha(lambda_zip) != state["lambda_sha256"] or _sha(runtime_zip) != state["runtime_sha256"]:
            raise RuntimeError("Resume with the exact ZIPs recorded in S04 state")
    else:
        s03 = _s03_state(clients, account)
        if s03["runtime_id"] != expected_runtime_id:
            raise RuntimeError("Expected Runtime target differs from the exact Runtime in S03 local state")
        if not PREFLIGHT_PATH.is_file():
            raise RuntimeError("Run preflight first; no exact-name availability snapshot is saved")
        snapshot = json.loads(PREFLIGHT_PATH.read_text(encoding="utf-8"))
        if snapshot.get("account_id") != account or snapshot.get("region") != REGION or snapshot.get("runtime_id") != s03["runtime_id"]:
            raise RuntimeError("Saved preflight belongs to another account, region, or Runtime; rerun preflight")
        _inventory_exact_names(clients)
        live_runtime = clients["bedrock-agentcore-control"].get_agent_runtime(agentRuntimeId=s03["runtime_id"])
        prior_runtime = _safe_runtime_snapshot(live_runtime)
        lifecycle = _runtime_lifecycle(prior_runtime)
        state = {"schema_version": 1, "account_id": account, "region": REGION, "phase": "planned",
                 "lambda_zip": str(lambda_zip.resolve()), "lambda_sha256": _sha(lambda_zip),
                 "runtime_zip": str(runtime_zip.resolve()), "runtime_sha256": _sha(runtime_zip),
                 "runtime_id": s03["runtime_id"], "runtime_arn": s03.get("runtime_arn") or f"arn:aws:bedrock-agentcore:{REGION}:{account}:runtime/{s03['runtime_id']}",
                 "sessions": [], "invocations": [], "positive_attempts": [],
                 "prior_runtime": prior_runtime,
                 "s03_state_path": str(S03_STATE_PATH), "s03_bucket": s03["bucket"], "operations": {}, "owned": {}}
        _save(state)
    iam, lam, logs, control, s3 = (clients[k] for k in ("iam", "lambda", "logs", "bedrock-agentcore-control", "s3"))
    lt, lp, gt, gp = _role_documents(account)
    lambda_role = _ensure_role(iam, state, "lambda", LAMBDA_ROLE, lt, lp)
    gateway_role = _ensure_role(iam, state, "gateway", GATEWAY_ROLE, gt, gp)
    state["lambda_role_arn"], state["gateway_role_arn"] = lambda_role, gateway_role
    _save(state)
    group = next((g for g in logs.describe_log_groups(logGroupNamePrefix=LOG_GROUP)["logGroups"] if g["logGroupName"] == LOG_GROUP), None)
    if group is None:
        _record(state, "log_group", {"name": LOG_GROUP, "retention": LOG_RETENTION_DAYS})
        logs.create_log_group(logGroupName=LOG_GROUP)
    elif "log_group" not in state.get("operations", {}):
        raise RuntimeError("The exact log group exists without this run's create intent; refusing to reuse it")
    logs.put_retention_policy(logGroupName=LOG_GROUP, retentionInDays=LOG_RETENTION_DAYS)
    state.setdefault("operations", {}).setdefault("log_group", {})["status"] = "configured"
    state["owned"]["log_group"] = LOG_GROUP
    _save(state)
    s3_key = f"s04/runtime-{state['runtime_sha256']}.zip"
    s3.head_bucket(Bucket=state["s03_bucket"])
    if state.get("artifact_uploaded") is not True:
        _record(state, "runtime_artifact", {"bucket": state["s03_bucket"], "key": s3_key, "sha256": state["runtime_sha256"]})
        try:
            head = s3.head_object(Bucket=state["s03_bucket"], Key=s3_key, ExpectedBucketOwner=account)
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
                raise
            head = None
        if head and (head.get("Metadata", {}).get("sha256") != state["runtime_sha256"] or head.get("ContentLength") != Path(state["runtime_zip"]).stat().st_size):
            raise RuntimeError("Runtime ZIP object exists but differs from this exact build; stopped")
        if head is None:
            with Path(state["runtime_zip"]).open("rb") as body:
                s3.put_object(Bucket=state["s03_bucket"], Key=s3_key, Body=body, ExpectedBucketOwner=account, Metadata={"sha256": state["runtime_sha256"]})
        state["artifact_bucket"], state["artifact_key"], state["artifact_uploaded"] = state["s03_bucket"], s3_key, True
        state["owned"]["artifact"] = {"bucket": state["s03_bucket"], "key": s3_key}
        _save(state)
    expected_lambda_hash = base64.b64encode(bytes.fromhex(state["lambda_sha256"])).decode("ascii")
    try:
        function = lam.get_function(FunctionName=LAMBDA_NAME)["Configuration"]
        if (function.get("Role") != lambda_role or function.get("Handler") != "cloudwatch_reader.lambda_handler"
                or function.get("Runtime") != "python3.13"
                or function.get("CodeSha256") != expected_lambda_hash
                or "lambda" not in state.get("operations", {})):
            raise RuntimeError("Named Lambda does not match this exact run's recorded code and role; refusing to adopt it")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise
        _record(state, "lambda", {"name": LAMBDA_NAME, "role": lambda_role, "zip_sha256": state["lambda_sha256"]})
        try:
            function = _created_lambda_configuration(lam.create_function(FunctionName=LAMBDA_NAME, Runtime="python3.13", Role=lambda_role, Handler="cloudwatch_reader.lambda_handler", Code={"ZipFile": Path(state["lambda_zip"]).read_bytes()}, Timeout=30, MemorySize=256, Environment={"Variables": {"EXPECTED_GATEWAY_ID": "pending", "EXPECTED_TARGET_ID": "pending"}}))
        except ClientError as exc:
            _record_client_error(state, "lambda", "lambda.CreateFunction", exc)
            raise
    state["lambda_arn"] = function["FunctionArn"]
    state.setdefault("operations", {}).setdefault("lambda", {})["status"] = "created"
    state["owned"]["lambda"] = LAMBDA_NAME
    _save(state)
    _wait_lambda_active(lam)
    # Gateway APIs have no client token; after an ambiguous response discover only the exact planned name.
    if not state.get("gateway_id"):
        operation = state.get("operations", {}).get("gateway", {})
        found = [item for item in _all_items(clients["bedrock-agentcore-control"], "list_gateways", "items") if item.get("name") == GATEWAY_NAME]
        if found:
            if len(found) != 1 or not operation:
                raise RuntimeError("Exact Gateway name is already present or ambiguous; inspect without creating another")
            recovered_gateway = _recover_gateway_from_summary(clients["bedrock-agentcore-control"], found[0], gateway_role)
            state["gateway_id"], state["gateway_arn"] = recovered_gateway["gatewayId"], recovered_gateway["gatewayArn"]
        else:
            token = operation.get("request", {}).get("clientToken") or str(uuid.uuid4())
            gateway_request = {"name": GATEWAY_NAME, "roleArn": gateway_role, "protocolType": "MCP", "authorizerType": "AWS_IAM", "clientToken": token}
            _record(state, "gateway", gateway_request)
            response = clients["bedrock-agentcore-control"].create_gateway(**gateway_request)
            state["gateway_id"], state["gateway_arn"] = response["gatewayId"], response["gatewayArn"]
        state.setdefault("operations", {}).setdefault("gateway", {})["status"] = "created"
        state["owned"]["gateway"] = state["gateway_id"]
        _save(state)
    gateway_check = clients["bedrock-agentcore-control"].get_gateway(gatewayIdentifier=state["gateway_id"])
    if gateway_check.get("name") != GATEWAY_NAME or gateway_check.get("roleArn") != gateway_role or gateway_check.get("protocolType") != "MCP" or gateway_check.get("authorizerType") != "AWS_IAM":
        raise RuntimeError("Recovered Gateway does not match the exact planned name/role/MCP/AWS_IAM configuration")
    runtime_role_name = json.loads(S03_STATE_PATH.read_text(encoding="utf-8"))["role_name"]
    _ensure_runtime_gateway_permission(iam, state, runtime_role_name)
    if not state.get("target_id"):
        target_config = {"mcp": {"lambda": {"lambdaArn": state["lambda_arn"], "toolSchema": _gateway_tool_schema()}}}
        operation = state.get("operations", {}).get("target", {})
        found = [item for item in _all_items(clients["bedrock-agentcore-control"], "list_gateway_targets", "items", gatewayIdentifier=state["gateway_id"]) if item.get("name") == GATEWAY_TARGET_NAME]
        if found:
            if len(found) != 1 or not operation:
                raise RuntimeError("Exact Gateway target name is already present or ambiguous; inspect without creating another")
            state["target_id"] = found[0]["targetId"]
            current_target = clients["bedrock-agentcore-control"].get_gateway_target(gatewayIdentifier=state["gateway_id"], targetId=state["target_id"])
            configured_lambda = current_target.get("targetConfiguration", {}).get("mcp", {}).get("lambda", {}).get("lambdaArn")
            providers = current_target.get("credentialProviderConfigurations", [])
            if configured_lambda != state["lambda_arn"] or not any(provider.get("credentialProviderType") == "GATEWAY_IAM_ROLE" for provider in providers):
                raise RuntimeError("Recovered Gateway target does not match the exact Lambda and Gateway IAM role")
        else:
            token = operation.get("request", {}).get("clientToken") or str(uuid.uuid4())
            target_request = {"gatewayIdentifier": state["gateway_id"], "name": GATEWAY_TARGET_NAME, "targetConfiguration": target_config, "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}], "clientToken": token}
            _record(state, "target", {"gateway_id": state["gateway_id"], "name": GATEWAY_TARGET_NAME, "clientToken": token})
            target = clients["bedrock-agentcore-control"].create_gateway_target(**target_request)
            state["target_id"] = target["targetId"]
        state.setdefault("operations", {}).setdefault("target", {})["status"] = "created"
        state["owned"]["target"] = state["target_id"]
        _save(state)
    _wait_gateway_ready(clients["bedrock-agentcore-control"], state["gateway_id"])
    _wait_target_ready(clients["bedrock-agentcore-control"], state["gateway_id"], state["target_id"])
    _wait_lambda_active(lam)
    lam.update_function_configuration(FunctionName=LAMBDA_NAME, Environment={"Variables": {"EXPECTED_GATEWAY_ID": state["gateway_id"], "EXPECTED_TARGET_ID": state["target_id"]}})
    # Narrow Gateway trust to the exact ARN after the gateway exists.
    gt["Statement"][0]["Condition"]["ArnLike"]["aws:SourceArn"] = state["gateway_arn"]
    iam.update_assume_role_policy(RoleName=GATEWAY_ROLE, PolicyDocument=json.dumps(gt))
    gateway = clients["bedrock-agentcore-control"].get_gateway(gatewayIdentifier=state["gateway_id"])
    state["gateway_endpoint"] = gateway.get("gatewayUrl") or gateway.get("endpoint")
    if not state["gateway_endpoint"]:
        raise RuntimeError("Gateway endpoint is absent; stop and inspect")
    runtime = control.get_agent_runtime(agentRuntimeId=state["runtime_id"])
    prior = state["prior_runtime"]
    env = dict(prior.get("environmentVariables") or {})
    env.update({"S04_GATEWAY_URL": _mcp_endpoint_url(state["gateway_endpoint"]), "S04_GATEWAY_REGION": REGION, "S04_GATEWAY_TOOL_NAME": GATEWAY_TOOL_NAME})
    artifact = copy.deepcopy(prior["agentRuntimeArtifact"])
    artifact["codeConfiguration"]["code"]["s3"] = {"bucket": state["artifact_bucket"], "prefix": state["artifact_key"]}
    request = {"agentRuntimeId": state["runtime_id"], "agentRuntimeArtifact": artifact, "roleArn": prior["roleArn"], "platformVersion": prior["platformVersion"], "networkConfiguration": prior["networkConfiguration"], "protocolConfiguration": prior["protocolConfiguration"], "environmentVariables": env, "metadataConfiguration": prior.get("metadataConfiguration", {"requireMMDSV2": True})}
    lifecycle = _runtime_lifecycle(prior)
    request["lifecycleConfiguration"] = lifecycle
    request["clientToken"] = state.get("runtime_update_token") or str(uuid.uuid4())
    state["runtime_update_token"] = request["clientToken"]
    state["runtime_update_request"] = request
    _record(state, "runtime_update", request)
    update = control.update_agent_runtime(**request)
    state["runtime_version"] = update.get("agentRuntimeVersion")
    _save(state)
    _wait_ready(control, state)
    state["phase"] = "ready"
    state["operations"]["runtime_update"]["status"] = "ready"
    _save(state)
    print("S04 resources are READY. S03 Runtime remains in place with the S04 artifact at DEFAULT.")


def _wait_ready(control: Any, state: dict[str, Any]) -> dict[str, Any]:
    until = time.monotonic() + 1200
    while time.monotonic() < until:
        value = control.get_agent_runtime(agentRuntimeId=state["runtime_id"], agentRuntimeVersion=state.get("runtime_version"))
        if value.get("status") == "READY":
            if value.get("metadataConfiguration", {}).get("requireMMDSV2") is not True:
                raise RuntimeError("Runtime MMDSv2 confirmation failed")
            return value
        if value.get("status") not in {"CREATING", "UPDATING"}:
            raise RuntimeError(f"Runtime status is {value.get('status')}; stop and inspect")
        time.sleep(10)
    raise RuntimeError("Runtime readiness timed out; inspect before retrying")


def _artifact_s3(runtime: dict[str, Any]) -> dict[str, Any]:
    return (runtime.get("agentRuntimeArtifact", {}).get("codeConfiguration", {})
            .get("code", {}).get("s3", {}))


def _runtime_drift_check(live: dict[str, Any], state: dict[str, Any], old_request: dict[str, Any]) -> None:
    expected_version = state.get("runtime_version")
    expected_artifact = old_request.get("agentRuntimeArtifact")
    if (live.get("status") != "READY" or live.get("agentRuntimeVersion") != expected_version
            or live.get("agentRuntimeArtifact") != expected_artifact
            or live.get("roleArn") != old_request.get("roleArn")
            or live.get("platformVersion") != old_request.get("platformVersion")
            or live.get("networkConfiguration") != old_request.get("networkConfiguration")
            or live.get("protocolConfiguration") != old_request.get("protocolConfiguration")
            or (live.get("environmentVariables") or {}) != old_request.get("environmentVariables", {})
            or live.get("metadataConfiguration") != old_request.get("metadataConfiguration")
            or _runtime_lifecycle(live) != old_request.get("lifecycleConfiguration")):
        raise RuntimeError("Live Runtime drifted from the saved V3 S04 configuration; no update was sent")


def update_runtime_artifact(expected_account: str, expected_region: str, expected_runtime_id: str,
                            runtime_zip: Path, profile: str | None) -> None:
    """Update only the exact existing S04 Runtime code artifact, with read-only resume."""
    clients = _clients(profile)
    account = _confirm(clients, expected_account, expected_region)
    if not runtime_zip.is_file():
        raise ValueError("Pass the exact packaged Runtime ZIP")
    state = _state(clients)
    if state.get("runtime_id") != expected_runtime_id or state.get("phase") != "ready":
        raise RuntimeError("Artifact update requires the exact READY S04 Runtime state")
    sessions = state.get("sessions", [])
    if not isinstance(sessions, list) or any(not isinstance(session, str) or not session for session in sessions):
        raise RuntimeError("Saved S04 session ledger is malformed; no Runtime artifact update was sent")
    if any(not _update_stop_confirmed(state, session) for session in sessions):
        raise RuntimeError("Every saved S04 session must have a latest matching HTTP 200 and statusCode 200 stop response with request ID before updating the Runtime artifact")
    if not _update_failed_attempts_reconciled(state):
        raise RuntimeError("Preserve and reconcile every requested Runtime invocation from exact 500/zero-tool logs and HTTP 200 stop evidence before updating the artifact")
    digest = _sha(runtime_zip)
    history = state.setdefault("runtime_artifact_updates", [])
    control, s3 = clients["bedrock-agentcore-control"], clients["s3"]

    # Once an intent exists, resume is read-only. Never repeat PutObject or UpdateAgentRuntime.
    if history:
        entry = history[-1]
        if entry.get("candidate_sha256") != digest:
            raise RuntimeError("A different artifact update is already recorded; read-only inspect it before proceeding")
        live = control.get_agent_runtime(agentRuntimeId=state["runtime_id"])
        obj = s3.head_object(Bucket=entry["bucket"], Key=entry["key"], ExpectedBucketOwner=account)
        object_matches = (obj.get("Metadata", {}).get("sha256") == digest
                          and obj.get("ContentLength") == entry.get("size"))
        expected_live = entry.get("update_request", {}).get("agentRuntimeArtifact")
        if (object_matches and expected_live and live.get("status") == "READY"
                and live.get("agentRuntimeVersion") == entry.get("runtime_version")
                and live.get("agentRuntimeArtifact") == expected_live):
            state["active_runtime_artifact"] = {"runtime_version": entry["runtime_version"],
                "sha256": digest, "bucket": entry["bucket"], "key": entry["key"],
                "update_request": entry["update_request"]}
            entry["status"] = "ready"
            _save(state)
            print("Read-only resume confirmed the recorded artifact update is READY; no AWS write was sent.")
            return
        print(json.dumps({"read_only_resume": True, "update_status": entry.get("status"),
                          "runtime_status": live.get("status"), "runtime_version": live.get("agentRuntimeVersion"),
                          "candidate_object_matches": object_matches, "no_write_repeated": True}, indent=2))
        return

    old_request = state.get("runtime_update_request")
    if not isinstance(old_request, dict) or not state.get("runtime_version"):
        raise RuntimeError("Saved V3 Runtime update intent is missing; no update was sent")
    live = control.get_agent_runtime(agentRuntimeId=state["runtime_id"])
    _runtime_drift_check(live, state, old_request)
    bucket = state.get("s03_bucket")
    key = f"s04/runtime-{digest}.zip"
    if not bucket or _artifact_s3(live) != {"bucket": state.get("artifact_bucket"), "prefix": state.get("artifact_key")}:
        raise RuntimeError("Live Runtime does not point at the exact saved V3 S04 artifact; no update was sent")
    if bucket != state.get("artifact_bucket"):
        raise RuntimeError("S03 artifact bucket differs from the saved S04 bucket")
    s3.head_bucket(Bucket=bucket, ExpectedBucketOwner=account)
    try:
        s3.head_object(Bucket=bucket, Key=key, ExpectedBucketOwner=account)
    except ClientError as exc:
        if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
            raise
    else:
        raise RuntimeError("Content-addressed candidate object already exists; refusing overwrite or adoption")

    request = copy.deepcopy(old_request)
    request["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"] = {"bucket": bucket, "prefix": key}
    request["clientToken"] = str(uuid.uuid4())
    entry = {"status": "intent_saved", "candidate_sha256": digest, "bucket": bucket, "key": key,
             "size": runtime_zip.stat().st_size, "base_version": state["runtime_version"],
             "base_artifact_key": state["artifact_key"], "client_token": request["clientToken"],
             "update_request": request}
    history.append(entry)
    _save(state)
    with runtime_zip.open("rb") as body:
        s3.put_object(Bucket=bucket, Key=key, Body=body, ExpectedBucketOwner=account,
                      Metadata={"sha256": digest})
    entry["status"] = "artifact_uploaded"
    _save(state)
    update = control.update_agent_runtime(**request)
    entry["runtime_version"] = update.get("agentRuntimeVersion")
    entry["status"] = "update_submitted"
    _save(state)
    ready = _wait_ready(control, {**state, "runtime_version": entry["runtime_version"]})
    if (_artifact_s3(ready) != {"bucket": bucket, "prefix": key}
            or ready.get("agentRuntimeVersion") != entry["runtime_version"]):
        raise RuntimeError("Updated Runtime became READY with an unexpected version or artifact; inspect without retrying")
    entry["status"] = "ready"
    state["active_runtime_artifact"] = {"runtime_version": entry["runtime_version"], "sha256": digest,
        "bucket": bucket, "key": key, "update_request": request}
    _save(state)
    print(f"Runtime artifact update READY at version {entry['runtime_version']}; prior S03 and V3 records are preserved.")


def _wait_lambda_active(lam: Any) -> None:
    until = time.monotonic() + 600
    while time.monotonic() < until:
        config = lam.get_function(FunctionName=LAMBDA_NAME)["Configuration"]
        if config.get("State") == "Active" and config.get("LastUpdateStatus") in {None, "Successful"}:
            return
        if config.get("State") == "Failed" or config.get("LastUpdateStatus") == "Failed":
            raise RuntimeError("Lambda deployment failed; inspect before retrying")
        time.sleep(5)
    raise RuntimeError("Lambda did not become active within ten minutes")


def _wait_gateway_ready(control: Any, gateway_id: str) -> None:
    until = time.monotonic() + 900
    while time.monotonic() < until:
        value = control.get_gateway(gatewayIdentifier=gateway_id)
        if value.get("status") == "READY":
            return
        if value.get("status") in {"FAILED", "DELETING", "DELETED"}:
            raise RuntimeError(f"Gateway status is {value.get('status')}; inspect before continuing")
        time.sleep(10)
    raise RuntimeError("Gateway did not become READY within fifteen minutes")


def _wait_target_ready(control: Any, gateway_id: str, target_id: str) -> None:
    until = time.monotonic() + 900
    while time.monotonic() < until:
        items = _all_items(control, "list_gateway_targets", "items", gatewayIdentifier=gateway_id)
        target = next((item for item in items if item.get("targetId") == target_id), None)
        if target and target.get("status") == "READY":
            return
        if target and target.get("status") in {"FAILED", "DELETING", "DELETED"}:
            raise RuntimeError(f"Gateway target status is {target.get('status')}; inspect before continuing")
        time.sleep(10)
    raise RuntimeError("Gateway target did not become READY within fifteen minutes")


def status(profile: str | None) -> None:
    clients = _clients(profile)
    state = _state(clients)
    result = clients["bedrock-agentcore-control"].get_agent_runtime(agentRuntimeId=state["runtime_id"])
    active_sessions = [session for session in state.get("sessions", []) if not _session_stop_confirmed(state, session)]
    print(json.dumps({"runtime_id": state["runtime_id"], "status": result.get("status"), "version": result.get("agentRuntimeVersion"), "gateway_id": state.get("gateway_id"), "active_sessions": len(active_sessions), "read_only": True}, indent=2))


def _invoke(runtime: Any, state: dict[str, Any], session: str, prompt: str) -> dict[str, Any]:
    response = runtime.invoke_agent_runtime(agentRuntimeArn=state["runtime_arn"], qualifier="DEFAULT", runtimeSessionId=session, contentType="application/json", accept="application/json", payload=json.dumps({"prompt": prompt}, ensure_ascii=False).encode())
    return json.loads(response["response"].read().decode("utf-8"))


def _confirmed_state(profile: str | None, expected_account: str, expected_region: str, expected_runtime_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    clients = _clients(profile)
    _confirm(clients, expected_account, expected_region)
    state = _state(clients)
    if state.get("runtime_id") != expected_runtime_id:
        raise RuntimeError("Typed Runtime target differs from S04 local state")
    return clients, state


def reconcile_invocation(expected_account: str, expected_region: str, expected_runtime_id: str,
                         invocation_id: str, profile: str | None) -> None:
    """Record one exact failed Runtime invocation from logs after its session stopped."""
    clients, state = _confirmed_state(profile, expected_account, expected_region, expected_runtime_id)
    pending = [item for item in state.get("positive_attempts", []) if item.get("status") == "requested"]
    if len(pending) != 1:
        raise RuntimeError("Reconciliation requires exactly one preserved requested positive attempt")
    attempt = pending[0]
    session = attempt.get("session_id")
    if any(item.get("session_id") == session for item in state.get("reconciliations", [])):
        raise RuntimeError("This failed session already has a reconciliation entry; do not repeat it")
    if not isinstance(session, str) or not _session_stop_confirmed(state, session):
        raise RuntimeError("The exact session must have a confirmed terminal stop response before reconciliation")
    stop = next(item for item in reversed(state.get("stop_attempts", [])) if item.get("session_id") == session)
    stop_proof = {key: stop.get(key) for key in ("status", "status_code", "http_status_code", "request_id")}
    if (stop_proof.get("status") != "stopped" or stop_proof.get("status_code") != 200
            or stop_proof.get("http_status_code") != 200
            or not isinstance(stop_proof.get("request_id"), str) or not stop_proof["request_id"].strip()):
        raise RuntimeError("Reconciliation requires an explicit HTTP 200/statusCode 200 stop result")
    attempt_epoch = attempt.get("at_epoch")
    stop_epoch = stop.get("at_epoch")
    if (not isinstance(attempt_epoch, int) or isinstance(attempt_epoch, bool)
            or not isinstance(stop_epoch, int) or isinstance(stop_epoch, bool)
            or attempt_epoch < 0 or stop_epoch < attempt_epoch):
        raise RuntimeError("Reconciliation requires a valid attempt-to-stop time window")
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", invocation_id):
        raise ValueError("Pass the exact Runtime invocation ID from CloudWatch logs")
    log_group = f"/aws/bedrock-agentcore/runtimes/{state['runtime_id']}-DEFAULT"
    request = {"logGroupName": log_group, "filterPattern": f'"{invocation_id}"',
               "startTime": attempt_epoch * 1000, "endTime": stop_epoch * 1000, "limit": 50}
    matches: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    seen_tokens: set[str] = set()
    token: str | None = None
    page_complete = False
    for _page_number in range(10):
        page_request = dict(request)
        if token is not None:
            page_request["nextToken"] = token
        response = clients["logs"].filter_log_events(**page_request)
        for event in response.get("events", []):
            try:
                document = json.loads(event.get("message", "{}"))
            except (TypeError, ValueError):
                continue
            timestamp = event.get("timestamp")
            if document.get("event") == "agent_invocation" and document.get("invocation_id") == invocation_id:
                if (not isinstance(timestamp, int) or isinstance(timestamp, bool)
                        or not request["startTime"] <= timestamp <= request["endTime"]):
                    raise RuntimeError("Exact Runtime invocation log event falls outside the recorded attempt-to-stop window")
                event_id = event.get("eventId")
                if not isinstance(event_id, str) or not event_id:
                    raise RuntimeError("Exact Runtime invocation log event lacks its CloudWatch event ID")
                matches[event_id] = (event, document)
                if len(matches) > 1:
                    raise RuntimeError("Runtime invocation evidence contains conflicting exact events")
        next_token = response.get("nextToken")
        if next_token is None:
            page_complete = True
            break
        if not isinstance(next_token, str) or not next_token or next_token in seen_tokens:
            raise RuntimeError("CloudWatch log pagination repeated or returned an invalid token; evidence is incomplete")
        seen_tokens.add(next_token)
        token = next_token
    if not page_complete:
        raise RuntimeError("CloudWatch log pagination exceeded the ten-page reconciliation limit; evidence is incomplete")
    if len(matches) != 1:
        raise RuntimeError("Runtime invocation evidence is absent or ambiguous; do not start another validation run")
    event, document = next(iter(matches.values()))
    if document.get("status") != 500 or document.get("error_type") != "RuntimeError" or document.get("tool_call_count") != 0 or document.get("tool_names") != []:
        raise RuntimeError("Runtime log does not match the exact confirmed 500/RuntimeError/zero-tool failure")
    runs = _ensure_validation_runs(state)
    if not runs or runs[-1].get("session_id") != session:
        raise RuntimeError("Preserved validation run history does not match the failed session")
    history = state.setdefault("reconciliations", [])
    history.append({"kind": "runtime_failure_reconciled", "session_id": session,
                    "attempt_step": attempt.get("step"), "invocation_id": invocation_id,
                    "log_event_timestamp": event.get("timestamp"), "runtime_status": 500,
                    "error_type": "RuntimeError", "tool_call_count": 0, "tool_names": [],
                    "stop_response": stop_proof, "recorded_at_epoch": int(time.time())})
    _save(state)
    print(json.dumps({"reconciled": True, "invocation_id": invocation_id,
                      "runtime_status": 500, "tool_call_count": 0, "session_stopped": True,
                      "new_validation_run_allowance": 1}, ensure_ascii=False))


def invoke(expected_account: str, expected_region: str, expected_runtime_id: str, profile: str | None) -> None:
    clients, state = _confirmed_state(profile, expected_account, expected_region, expected_runtime_id)
    control = clients["bedrock-agentcore-control"]
    if not SEED_STATE_PATH.exists():
        raise RuntimeError("Completed .s04-seed-state.json is required before positive Runtime calls")
    seed = json.loads(SEED_STATE_PATH.read_text(encoding="utf-8"))
    query_start, query_end, request_id = _positive_query_window(seed)
    state.setdefault("positive_attempts", [])
    state.setdefault("invocations", [])
    runs = _ensure_validation_runs(state)
    now = int(time.time())
    if not runs:
        session = str(uuid.uuid4()) + "-s04-positive-session-0001"
        run = {"session_id": session, "started_epoch": now, "stop_deadline_epoch": now + 5 * 60, "attempt_steps": []}
        runs.append(run)
    else:
        run = runs[-1]
        session = run["session_id"]
        if _session_stop_confirmed(state, session):
            if len(runs) >= MAX_VALIDATION_RUNS or not any(item.get("session_id") == session for item in state.get("reconciliations", [])):
                raise RuntimeError("No new validation run is allowed without the single exact reconciled failure; preserve state")
            session = str(uuid.uuid4()) + "-s04-positive-session-0002"
            run = {"session_id": session, "started_epoch": now, "stop_deadline_epoch": now + 5 * 60, "attempt_steps": []}
            runs.append(run)
        else:
            unresolved = [item for item in state["positive_attempts"]
                          if item.get("session_id") == session and item.get("status") == "requested"]
            if unresolved:
                raise RuntimeError("A positive call in this session is unresolved; reconcile exact log evidence after stopping")
    run_attempts = [item for item in state["positive_attempts"] if item.get("session_id") == session]
    if len(run_attempts) >= MAX_POSITIVE_CALLS_PER_RUN:
        raise RuntimeError("Two positive call attempts are already recorded for this session; no more calls are allowed")
    state["first_invoke_started_epoch"] = run["started_epoch"]
    state["stop_deadline_epoch"] = run["stop_deadline_epoch"]
    state["positive_session"] = session
    state["sessions"] = sorted(set(state.get("sessions", []) + [session]))
    _save(state)
    print(json.dumps({"event": "positive_run_started", "first_invoke_started_epoch": state["first_invoke_started_epoch"], "second_call_deadline_epoch": state["stop_deadline_epoch"], "runtime_session_id": session}), flush=True)
    prompts = (
        ("logs", f"data_type=logs、service=catalog-api、start_time={query_start}、end_time={query_end}、max_results=50で合成CloudWatch Logsを調べてください。request_id={request_id} と status_code=503 のイベントを確認し、観測できた内容だけを説明してください。"),
        ("metrics", f"data_type=metrics、service=catalog-api、start_time={query_start}、end_time={query_end}、max_results=50で合成CloudWatch metricを調べてください。Http5xxCount の値と時刻を確認し、合成データであることを明記して説明してください。"),
    )
    for data_type, phrase in prompts:
        run_attempts = [item for item in state["positive_attempts"] if item.get("session_id") == session]
        if len(run_attempts) >= MAX_POSITIVE_CALLS_PER_RUN:
            break
        if time.time() >= state["stop_deadline_epoch"]:
            raise RuntimeError("Five-minute stop gate reached; stop exact saved session now")
        _check_deployed_lifecycle(control, state)
        _guard(clients["sts"], state["account_id"])
        attempt_step = len(state["positive_attempts"]) + 1
        state["positive_attempts"].append({"status": "requested", "at_epoch": int(time.time()), "session_id": session, "step": attempt_step})
        run.setdefault("attempt_steps", []).append(attempt_step)
        _save(state)
        result = _invoke(clients["bedrock-agentcore"], state, session, phrase)
        invocation_id = result.get("invocation_id")
        if not invocation_id:
            raise RuntimeError("Runtime response omitted invocation ID; stop and inspect")
        state["invocations"].append(invocation_id)
        state["positive_attempts"][-1].update({"status": "response_received", "invocation_id": invocation_id})
        _save(state)
        if not _has_expected_positive_trace(result, data_type, query_start, query_end):
            state["positive_attempts"][-1]["status"] = "response_unverified"
            _save(state)
            raise RuntimeError("Runtime response did not include an ok=true tool trace for the exact S04 service and seeded UTC window; stop and inspect")
        state["positive_attempts"][-1].update({"status": "succeeded", "invocation_id": invocation_id})
        _save(state)
        print(json.dumps({"session_id": session, "invocation_id": invocation_id, "response": result}, ensure_ascii=False))


def negative(expected_account: str, expected_region: str, expected_runtime_id: str, profile: str | None) -> None:
    clients, state = _confirmed_state(profile, expected_account, expected_region, expected_runtime_id)
    if state.get("negative_attempts"):
        raise RuntimeError("Negative Gateway attempts are already recorded; do not replay them")
    state["negative_attempts"] = []
    _save(state)
    endpoint = _mcp_endpoint_url(state["gateway_endpoint"])
    # Test unauthenticated access without loading or passing credentials.
    import urllib.error
    import urllib.request
    state["negative_attempts"].append({"kind": "unauthenticated", "status": "requested", "at_epoch": int(time.time())})
    _save(state)
    request = urllib.request.Request(endpoint, data=json.dumps({"jsonrpc": "2.0", "id": "s04-unauth", "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "s04-negative-check", "version": "1.0"}}}).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as exc:
        if exc.code not in {401, 403}:
            state["negative_attempts"][-1].update({"status": "unexpected", "http_status": exc.code})
            _save(state)
            raise RuntimeError(f"Unauthenticated Gateway returned HTTP {exc.code}, expected 401/403") from None
        state["negative_attempts"][-1].update({"status": "denied", "http_status": exc.code})
        _save(state)
        print(json.dumps({"unauthenticated_gateway": "denied", "http_status": exc.code}))
    else:
        state["negative_attempts"][-1]["status"] = "unexpected_success"
        _save(state)
        raise RuntimeError("Unauthenticated Gateway request unexpectedly succeeded")
    # Exercise the same service guard through a signed Gateway tool call, without Runtime/model use.
    from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
    from strands.tools.mcp.mcp_client import MCPClient
    mcp = MCPClient(lambda: aws_iam_streamablehttp_client(endpoint=endpoint, aws_region=REGION,
        aws_service="bedrock-agentcore", timeout=10, sse_read_timeout=20))
    state["negative_attempts"].append({"kind": "signed_invalid_service", "status": "requested", "at_epoch": int(time.time())})
    _save(state)
    with mcp:
        result = mcp.call_tool_sync(tool_use_id=str(uuid.uuid4()), name=GATEWAY_TOOL_NAME,
            arguments={"data_type": "logs", "service": "other-service", "start_time": "2026-01-01T00:00:00Z", "end_time": "2026-01-01T00:01:00Z", "max_results": 1})
    text = "\n".join(item.get("text", "") for item in result.get("content", []) if isinstance(item, dict))
    if not _is_expected_negative_response(result, text):
        state["negative_attempts"][-1]["status"] = "unexpected_success"
        state["negative_attempts"][-1]["gateway_status"] = result.get("status")
        _save(state)
        raise RuntimeError("Invalid-service Gateway call did not return the exact Lambda service-boundary rejection")
    state["negative_attempts"][-1].update({"status": "denied", "gateway_status": result.get("status")})
    _save(state)
    print(json.dumps({"invalid_service": "denied", "gateway_status": result.get("status"), "response": text}, ensure_ascii=False))


def stop(expected_account: str, expected_region: str, expected_runtime_id: str, profile: str | None) -> None:
    clients, state = _confirmed_state(profile, expected_account, expected_region, expected_runtime_id)
    runtime = clients["bedrock-agentcore"]
    state.setdefault("stop_attempts", [])
    for session in list(state.get("sessions", [])):
        if _session_stop_confirmed(state, session):
            continue
        _guard(clients["sts"], state["account_id"])
        attempt = {"session_id": session, "status": "requested", "at_epoch": int(time.time())}
        state["stop_attempts"].append(attempt)
        _save(state)
        try:
            response = runtime.stop_runtime_session(agentRuntimeArn=state["runtime_arn"], runtimeSessionId=session)
            metadata = response.get("ResponseMetadata", {})
            attempt.update({"status_code": response.get("statusCode"), "http_status_code": metadata.get("HTTPStatusCode"), "request_id": metadata.get("RequestId")})
            if response.get("statusCode") == 200 and metadata.get("HTTPStatusCode") == 200:
                attempt["status"] = "stopped"
            else:
                attempt["status"] = "unconfirmed"
                _save(state)
                raise RuntimeError("StopRuntimeSession returned an unconfirmed response; inspect before retrying")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                attempt.update({"status": "failed", "error_code": exc.response.get("Error", {}).get("Code"), "request_id": exc.response.get("ResponseMetadata", {}).get("RequestId")})
                _save(state)
                raise RuntimeError("Could not stop an exact saved session; stop manually in the AWS Console and verify it ended") from None
            in_flight = any(item.get("session_id") == session and item.get("status") == "requested" for item in state.get("positive_attempts", []))
            invoke_succeeded = any(item.get("session_id") == session and item.get("status") == "succeeded" for item in state.get("positive_attempts", []))
            attempt.update({"status": "not_found_unconfirmed" if in_flight or not invoke_succeeded else "not_found_after_successful_invoke",
                            "request_id": exc.response.get("ResponseMetadata", {}).get("RequestId")})
            _save(state)
            if attempt["status"] == "not_found_unconfirmed":
                raise RuntimeError("Session was not found but no successful stop was confirmed; if invoke is still running, wait and run stop again") from None
        _save(state)
    state["stop_requested_at_epoch"] = int(time.time())
    _save(state)
    outcomes = {item["status"] for item in state["stop_attempts"]}
    if "not_found_unconfirmed" in outcomes or "unconfirmed" in outcomes or "failed" in outcomes:
        print("Stop was attempted only for exact S04-recorded sessions. At least one outcome is unconfirmed; verify or retry after any in-flight invoke exits.")
    else:
        print("Stop response evidence is saved only for exact S04-recorded sessions. A 404 after a successful invoke is recorded as not-found/terminated, not as HTTP 200 success.")


def logs(profile: str | None) -> None:
    clients = _clients(profile)
    state = _state(clients)
    ids = list(state.get("invocations", []))
    if not ids:
        raise RuntimeError("No successful invocation IDs are saved")
    log_group = f"/aws/bedrock-agentcore/runtimes/{state['runtime_id']}-DEFAULT"
    for invocation_id in ids:
        response = clients["logs"].filter_log_events(logGroupName=log_group, filterPattern=f'"{invocation_id}"', limit=50)
        events = []
        for item in response.get("events", []):
            try:
                doc = json.loads(item.get("message", "{}"))
            except (TypeError, ValueError):
                continue
            if doc.get("event") == "agent_invocation" and doc.get("invocation_id") == invocation_id:
                events.append({"timestamp": item.get("timestamp"), "status": doc.get("status"), "tool_call_count": doc.get("tool_call_count"), "tool_names": doc.get("tool_names", [])})
        print(json.dumps({"invocation_id": invocation_id, "events": events}, ensure_ascii=False))


def restore(expected_account: str, expected_region: str, expected_runtime_id: str, profile: str | None) -> None:
    """Restore the exact S03 artifact and configuration captured before S04."""
    clients, state = _confirmed_state(profile, expected_account, expected_region, expected_runtime_id)
    active_sessions = [session for session in state.get("sessions", []) if not _session_stop_confirmed(state, session)]
    if active_sessions:
        raise RuntimeError("Stop exact saved S04 sessions before restoring the Runtime")
    prior = state.get("prior_runtime")
    if not isinstance(prior, dict):
        raise RuntimeError("Pre-update Runtime snapshot is missing; cannot safely restore")
    request = {"agentRuntimeId": state["runtime_id"], "agentRuntimeArtifact": prior["agentRuntimeArtifact"],
        "roleArn": prior["roleArn"], "platformVersion": prior["platformVersion"],
        "networkConfiguration": prior["networkConfiguration"], "protocolConfiguration": prior["protocolConfiguration"],
        "environmentVariables": prior.get("environmentVariables", {}),
        "metadataConfiguration": prior.get("metadataConfiguration", {"requireMMDSV2": True})}
    request["lifecycleConfiguration"] = prior.get("lifecycleConfiguration", {
        "idleRuntimeSessionTimeout": 900, "maxLifetime": 28800
    })
    request["clientToken"] = state.get("restore_token") or str(uuid.uuid4())
    state["restore_token"] = request["clientToken"]
    state["restore_request"] = request
    state["phase"] = "restore_requested"
    _save(state)
    response = clients["bedrock-agentcore-control"].update_agent_runtime(**request)
    state["restore_version"] = response.get("agentRuntimeVersion")
    _save(state)
    _wait_ready(clients["bedrock-agentcore-control"], {**state, "runtime_version": state["restore_version"]})
    state["phase"] = "restored"
    _save(state)
    print("The captured S03 artifact and Runtime configuration are READY at DEFAULT again.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("preflight"); p.add_argument("--expected-account", required=True); p.add_argument("--expected-region", required=True); p.add_argument("--expected-runtime-id", required=True)
    p = commands.add_parser("deploy"); p.add_argument("--expected-account", required=True); p.add_argument("--expected-region", required=True); p.add_argument("--expected-runtime-id", required=True); p.add_argument("--lambda-zip", type=Path, required=True); p.add_argument("--runtime-zip", type=Path, required=True)
    p = commands.add_parser("update-runtime-artifact"); p.add_argument("--expected-account", required=True); p.add_argument("--expected-region", required=True); p.add_argument("--expected-runtime-id", required=True); p.add_argument("--runtime-zip", type=Path, required=True)
    for name in ("status", "invoke", "negative", "stop", "logs", "restore", "reconcile-invocation"):
        command = commands.add_parser(name)
        if name in {"invoke", "negative", "stop", "restore", "reconcile-invocation"}:
            command.add_argument("--expected-account", required=True)
            command.add_argument("--expected-region", required=True)
            command.add_argument("--expected-runtime-id", required=True)
        if name == "reconcile-invocation":
            command.add_argument("--invocation-id", required=True)
    args = parser.parse_args()
    try:
        if args.command == "preflight": preflight(args.expected_account, args.expected_region, args.expected_runtime_id, args.profile)
        elif args.command == "deploy": deploy(args.expected_account, args.expected_region, args.expected_runtime_id, args.lambda_zip, args.runtime_zip, args.profile)
        elif args.command == "update-runtime-artifact": update_runtime_artifact(args.expected_account, args.expected_region, args.expected_runtime_id, args.runtime_zip, args.profile)
        elif args.command == "status": status(args.profile)
        elif args.command == "invoke": invoke(args.expected_account, args.expected_region, args.expected_runtime_id, args.profile)
        elif args.command == "negative": negative(args.expected_account, args.expected_region, args.expected_runtime_id, args.profile)
        elif args.command == "stop": stop(args.expected_account, args.expected_region, args.expected_runtime_id, args.profile)
        elif args.command == "logs": logs(args.profile)
        elif args.command == "restore": restore(args.expected_account, args.expected_region, args.expected_runtime_id, args.profile)
        else: reconcile_invocation(args.expected_account, args.expected_region, args.expected_runtime_id, args.invocation_id, args.profile)
    except (BotoCoreError, ClientError) as exc:
        code = exc.response.get("Error", {}).get("Code", type(exc).__name__) if isinstance(exc, ClientError) else type(exc).__name__
        print(f"AWS operation failed ({code}); preserve local state and inspect before retrying.", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, RuntimeError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
