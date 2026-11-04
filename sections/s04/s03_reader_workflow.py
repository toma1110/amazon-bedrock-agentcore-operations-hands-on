"""Run the isolated S03 invocation reader hands-on workflow.

Writes are recorded before they are sent. An uncertain write is never replayed;
use ``resume`` to inspect the exact named resource and preserve the result.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import ClientError

REGION = "ap-northeast-1"
S03_STATE_PATH = Path(__file__).resolve().parents[1] / "s03" / ".agentcore-s03-state.json"
STATE_DIR = Path(__file__).resolve().parents[2] / ".local" / "s04-reader"
RUN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,19}$")
LOG_RE = re.compile(r"^/aws/bedrock-agentcore/runtimes/(?P<runtime_id>[A-Za-z0-9_-]+)-DEFAULT$")
MODEL_ARN = f"arn:aws:bedrock:{REGION}::foundation-model/amazon.nova-lite-v1:0"


def names_for_run(run_id: str) -> dict[str, str]:
    if not isinstance(run_id, str) or not RUN_RE.fullmatch(run_id):
        raise ValueError("run_id must be 1-20 lowercase letters, digits, or hyphens")
    tag = hashlib.sha256(run_id.encode("ascii")).hexdigest()[:10]
    return {
        "runtime": f"udemy_agentcore_s04r{tag}",
        "lambda": f"udemy-agentcore-s04-reader-{tag}",
        "gateway": f"udemy-agentcore-s04-reader-{tag}",
        "target": f"udemy-agentcore-s04-target-{tag}",
        "lambda_role": f"udemy-agentcore-s04-lambda-{tag}",
        "gateway_role": f"udemy-agentcore-s04-gateway-{tag}",
        "runtime_role": f"udemy-agentcore-s04-runtime-{tag}",
        "s3_prefix": f"s04/s03-reader/{tag}",
    }


def log_group_runtime_id(log_group: str) -> str:
    match = LOG_RE.fullmatch(log_group) if isinstance(log_group, str) else None
    if not match:
        raise ValueError("Pass the exact S03 DEFAULT Runtime log group")
    return match.group("runtime_id")


def runtime_log_group(runtime_id: str) -> str:
    if not isinstance(runtime_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", runtime_id):
        raise ValueError("runtime_id must be the exact AgentCore Runtime ID")
    return f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"


def runtime_log_group_arn(runtime_id: str, account: str) -> str:
    if not re.fullmatch(r"[0-9]{12}", account):
        raise ValueError("account must be a 12-digit AWS account ID")
    return f"arn:aws:logs:{REGION}:{account}:log-group:{runtime_log_group(runtime_id)}"


def _exact_log_group(logs: Any, group_name: str) -> dict[str, Any] | None:
    token = None
    while True:
        request: dict[str, Any] = {"logGroupNamePrefix": group_name}
        if token:
            request["nextToken"] = token
        response = logs.describe_log_groups(**request)
        match = next((item for item in response.get("logGroups", [])
                      if item.get("logGroupName") == group_name), None)
        if match:
            return match
        token = response.get("nextToken")
        if not token:
            return None


def _runtime_log_policies(logs: Any, resource_arn: str) -> list[dict[str, Any]]:
    policies: list[dict[str, Any]] = []
    token = None
    while True:
        request: dict[str, Any] = {"policyScope": "RESOURCE", "resourceArn": resource_arn}
        if token:
            request["nextToken"] = token
        response = logs.describe_resource_policies(**request)
        page_policies = response.get("resourcePolicies", [])
        if any(item.get("policyScope") != "RESOURCE" or item.get("resourceArn") != resource_arn
               for item in page_policies):
            raise RuntimeError("DescribeResourcePolicies returned a policy outside the exact Runtime log group scope")
        policies.extend(page_policies)
        token = response.get("nextToken")
        if not token:
            return policies


def mcp_endpoint_url(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("https://"):
        raise ValueError("Gateway endpoint must be HTTPS")
    normalized = value.rstrip("/")
    return normalized if normalized.endswith("/mcp") else normalized + "/mcp"


def _state_path(run_id: str) -> Path:
    return STATE_DIR / f"{run_id}.json"


def _save(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    temp.replace(path)


def _load(run_id: str) -> tuple[Path, dict[str, Any]]:
    path = _state_path(run_id)
    if not path.is_file():
        raise RuntimeError("Run preflight first; exact account, S03 Runtime and resource names are not saved")
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("schema_version") != 1 or state.get("run_id") != run_id:
        raise RuntimeError("State does not match this run_id; preserve and inspect it")
    return path, state


def _clients(profile: str | None, *, writes: bool = False) -> dict[str, Any]:
    session = boto3.Session(profile_name=profile, region_name=REGION)
    cfg = Config(retries={"total_max_attempts": 1}) if writes else None
    names = ("sts", "iam", "lambda", "logs", "s3", "bedrock-agentcore-control", "bedrock-agentcore")
    result = {name: session.client(name, config=cfg) if cfg else session.client(name) for name in names[:-1]}
    runtime_cfg = Config(read_timeout=900, connect_timeout=10, retries={"total_max_attempts": 1})
    result[names[-1]] = session.client(names[-1], config=runtime_cfg)
    return result


def _identity(clients: dict[str, Any], expected_account: str, expected_region: str) -> None:
    if expected_region != REGION:
        raise ValueError(f"This exercise is limited to {REGION}")
    if not re.fullmatch(r"[0-9]{12}", expected_account):
        raise ValueError("expected_account must be a 12-digit account ID")
    actual = clients["sts"].get_caller_identity().get("Account")
    if actual != expected_account:
        raise RuntimeError("AWS caller account does not match expected_account; no write was sent")


def _s03_local() -> dict[str, Any]:
    if not S03_STATE_PATH.is_file():
        raise RuntimeError("S03 local state is missing; complete Section S03 first")
    state = json.loads(S03_STATE_PATH.read_text(encoding="utf-8"))
    if not isinstance(state.get("runtime_id"), str) or not isinstance(state.get("bucket"), str):
        raise RuntimeError("S03 state lacks the exact Runtime ID or artifact bucket")
    return state


def _resource_names_available(clients: dict[str, Any], names: dict[str, str]) -> None:
    for key in ("lambda_role", "gateway_role", "runtime_role"):
        try:
            clients["iam"].get_role(RoleName=names[key])
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "NoSuchEntity":
                raise
        else:
            raise RuntimeError(f"Exact resource {names[key]} already exists; choose a new run_id")
    try:
        clients["lambda"].get_function(FunctionName=names["lambda"])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in {"ResourceNotFoundException", "404"}:
            raise
    else:
        raise RuntimeError(f"Exact resource {names['lambda']} already exists; choose a new run_id")
    _require_log_group_absent(clients["logs"], f"/aws/lambda/{names['lambda']}")
    control = clients["bedrock-agentcore-control"]
    for key, operation, collection_key in (("runtime", "list_agent_runtimes", "agentRuntimes"),
                                           ("gateway", "list_gateways", "gateways")):
        token = None
        while True:
            request = {"maxResults": 100}
            if token:
                request["nextToken"] = token
            response = getattr(control, operation)(**request)
            collection = response.get(collection_key, [])
            if any((item.get("agentRuntimeName") if key == "runtime" else item.get("name")) == names[key]
                   for item in collection):
                raise RuntimeError(f"Exact resource {names[key]} already exists; choose a new run_id")
            token = response.get("nextToken")
            if not token:
                break


def _require_log_group_absent(logs: Any, exact_name: str) -> None:
    token = None
    while True:
        request = {"logGroupNamePrefix": exact_name}
        if token:
            request["nextToken"] = token
        response = logs.describe_log_groups(**request)
        if any(group.get("logGroupName") == exact_name for group in response.get("logGroups", [])):
            raise RuntimeError(f"Exact resource {exact_name} already exists; choose a new run_id")
        token = response.get("nextToken")
        if not token:
            return


def _bucket_versioning(s3: Any, bucket: str, expected_account: str) -> str | None:
    response = s3.get_bucket_versioning(Bucket=bucket, ExpectedBucketOwner=expected_account)
    return response.get("Status")


def _require_unversioned_bucket(s3: Any, bucket: str, expected_account: str) -> str | None:
    status_value = _bucket_versioning(s3, bucket, expected_account)
    if status_value in {"Enabled", "Suspended"}:
        raise RuntimeError(f"Bucket versioning is {status_value}; stop without changing bucket settings")
    if status_value not in {None, "Unversioned"}:
        raise RuntimeError(f"Unexpected bucket versioning state {status_value!r}; stop before writes")
    return status_value


def preflight(run_id: str, expected_account: str, expected_region: str,
              s03_log_group: str, profile: str | None) -> None:
    names = names_for_run(run_id)
    runtime_id = log_group_runtime_id(s03_log_group)
    local = _s03_local()
    if local["runtime_id"] != runtime_id:
        raise RuntimeError("The supplied S03 log group does not match the saved S03 Runtime ID")
    if local.get("account_id") and local["account_id"] != expected_account:
        raise RuntimeError("S03 local state belongs to a different AWS account")
    if local.get("region") and local["region"] != expected_region:
        raise RuntimeError("S03 local state belongs to a different AWS region")
    clients = _clients(profile)
    _identity(clients, expected_account, expected_region)
    versioning = _require_unversioned_bucket(clients["s3"], local["bucket"], expected_account)
    s03 = clients["bedrock-agentcore-control"].get_agent_runtime(agentRuntimeId=runtime_id)
    if s03.get("status") != "READY":
        raise RuntimeError("The exact S03 Runtime is not READY; no S04 resource was created")
    if s03.get("metadataConfiguration", {}).get("requireMMDSV2") is not True:
        raise RuntimeError("The exact S03 Runtime is not confirmed with MMDSv2; no S04 resource was created")
    artifact = s03.get("agentRuntimeArtifact", {}).get("codeConfiguration", {}).get("code", {}).get("s3", {})
    if not artifact.get("bucket") or not artifact.get("prefix"):
        raise RuntimeError("Could not read the exact S03 artifact location")
    if artifact["bucket"] != local["bucket"]:
        raise RuntimeError("S03 runtime artifact bucket differs from local S03 state")
    if artifact["prefix"] != local.get("object_key") or not local.get("zip_sha256"):
        raise RuntimeError("Live S03 artifact key or saved package hash differs from S03 local state")
    head = clients["s3"].head_object(Bucket=local["bucket"], Key=local["object_key"],
                                     ExpectedBucketOwner=expected_account)
    if head.get("Metadata", {}).get("sha256") != local["zip_sha256"]:
        raise RuntimeError("S03 artifact object hash does not match saved S03 state")
    existing_objects = clients["s3"].list_objects_v2(Bucket=local["bucket"], Prefix=names["s3_prefix"],
                                                       ExpectedBucketOwner=expected_account)
    if existing_objects.get("Contents") or existing_objects.get("IsTruncated"):
        raise RuntimeError("S04 package prefix already contains an object; choose a new run_id")
    _resource_names_available(clients, names)
    path = _state_path(run_id)
    if path.exists():
        raise FileExistsError(f"Local run state already exists: {path}; do not overwrite it")
    state = {
        "schema_version": 1, "run_id": run_id, "phase": "preflight_complete",
        "account_id": expected_account, "region": REGION, "s03_runtime_id": runtime_id,
        "ownership_tag": {"S04ReaderRun": run_id},
        "s03_log_group": s03_log_group, "bucket": local["bucket"], "names": names,
        "bucket_versioning_status": versioning,
        "borrowed_artifact": {"bucket": local["bucket"], "key": local["object_key"],
                               "sha256": local["zip_sha256"], "owned": False},
        "s03_snapshot": {key: s03.get(key) for key in ("agentRuntimeVersion", "roleArn", "platformVersion",
            "networkConfiguration", "protocolConfiguration", "environmentVariables", "metadataConfiguration",
            "lifecycleConfiguration", "agentRuntimeArtifact")},
        "operations": {}, "owned": {}, "sessions": [], "invocations": [],
    }
    _save(path, state)
    print(f"Preflight saved for account {expected_account}, region {REGION}, Runtime {runtime_id}; no AWS resource was changed.")


def _write_once(path: Path, state: dict[str, Any], step: str,
                operation: str, request: dict[str, Any], call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    prior = state["operations"].get(step)
    if prior:
        if prior.get("status") == "accepted":
            return prior.get("response", {})
        raise RuntimeError(f"{step} is {prior.get('status')}; run status/resume and reconcile before any further write")
    record = {"operation": operation, "request": request, "status": "requested", "at_epoch": int(time.time())}
    state["operations"][step] = record
    _save(path, state)
    try:
        response = call()
    except Exception as exc:
        record.update({"status": "outcome_unknown", "error_type": type(exc).__name__})
        _save(path, state)
        raise RuntimeError(f"{operation} outcome is unknown. Do not replay it; run status/resume and inspect the exact resource.") from None
    record.update({"status": "accepted", "response": response})
    _save(path, state)
    return response


def _wait_runtime(control: Any, runtime_id: str, version: str | None = None) -> dict[str, Any]:
    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        args = {"agentRuntimeId": runtime_id}
        if version:
            args["agentRuntimeVersion"] = version
        current = control.get_agent_runtime(**args)
        if current.get("status") == "READY":
            return current
        if current.get("status") not in {"CREATING", "UPDATING"}:
            raise RuntimeError(f"Runtime reached {current.get('status')}; preserve state and inspect")
        time.sleep(10)
    raise RuntimeError("Runtime readiness timed out; inspect exact Runtime before continuing")


def _policy(actions: list[str], resources: list[str]) -> dict[str, Any]:
    return {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": actions, "Resource": resources}]}


def _trust(service: str, account: str, source_arn: str | None = None) -> dict[str, Any]:
    condition: dict[str, Any] = {"StringEquals": {"aws:SourceAccount": account}}
    if source_arn:
        condition["ArnLike"] = {"aws:SourceArn": source_arn}
    return {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": service},
        "Action": "sts:AssumeRole", "Condition": condition}]}


def _policy_readback(iam: Any, role_name: str, policy_name: str, expected: dict[str, Any]) -> None:
    actual = iam.get_role_policy(RoleName=role_name, PolicyName=policy_name).get("PolicyDocument")
    if isinstance(actual, str):
        actual = json.loads(actual)
    if actual != expected:
        raise RuntimeError(f"IAM role policy readback differs for {role_name}; stop before creating dependent resources")


def _trust_readback(iam: Any, role_name: str, expected: dict[str, Any]) -> None:
    actual = iam.get_role(RoleName=role_name).get("Role", {}).get("AssumeRolePolicyDocument")
    if isinstance(actual, str):
        actual = json.loads(actual)
    if actual != expected:
        raise RuntimeError(f"IAM trust readback differs for {role_name}; stop before creating dependent resources")


def deploy(run_id: str, expected_account: str, expected_region: str,
          reader_lambda_zip: Path, reader_runtime_zip: Path, profile: str | None) -> None:
    path, state = _load(run_id)
    _identity(_clients(profile), expected_account, expected_region)
    if state.get("account_id") != expected_account or state.get("region") != REGION:
        raise RuntimeError("Saved preflight belongs to another account or region")
    for artifact in (reader_lambda_zip, reader_runtime_zip):
        if not artifact.is_file():
            raise FileNotFoundError(artifact)
    current_s03 = _clients(profile)["bedrock-agentcore-control"].get_agent_runtime(agentRuntimeId=state["s03_runtime_id"])
    snapshot = state["s03_snapshot"]
    for field in ("agentRuntimeVersion", "roleArn", "platformVersion", "networkConfiguration",
                  "protocolConfiguration", "environmentVariables", "metadataConfiguration",
                  "lifecycleConfiguration", "agentRuntimeArtifact"):
        if current_s03.get(field) != snapshot.get(field):
            raise RuntimeError(f"S03 Runtime changed after preflight ({field}); no write was sent")
    borrowed = state["borrowed_artifact"]
    borrowed_head = _clients(profile)["s3"].head_object(Bucket=borrowed["bucket"], Key=borrowed["key"],
                                                         ExpectedBucketOwner=expected_account)
    if borrowed_head.get("Metadata", {}).get("sha256") != borrowed["sha256"]:
        raise RuntimeError("Borrowed S03 artifact object no longer matches its saved hash; no write was sent")
    hashes = {"lambda": _sha256(reader_lambda_zip), "reader": _sha256(reader_runtime_zip)}
    if state.get("packages") and {key: value.get("sha256") for key, value in state["packages"].items()} != hashes:
        raise RuntimeError("Use the exact reader ZIP hashes saved for this run; choose a new run_id for other packages")
    if not state.get("packages"):
        state["packages"] = {key: {"path": str(value.resolve()), "sha256": hashes[key]} for key, value in
            (("lambda", reader_lambda_zip), ("reader", reader_runtime_zip))}
        _save(path, state)
    c = _clients(profile, writes=True)
    _identity(c, expected_account, expected_region)
    names, bucket, run = state["names"], state["bucket"], state["run_id"]
    prefix = names["s3_prefix"]
    keys = {key: f"{prefix}/{key}-{digest}.zip" for key, digest in hashes.items()}
    for key, file_path in (("lambda", reader_lambda_zip), ("reader", reader_runtime_zip)):
        if state["operations"].get(f"put_{key}", {}).get("status") != "accepted":
            request = {"Bucket": bucket, "Key": keys[key], "ExpectedBucketOwner": expected_account,
                       "IfNoneMatch": "*", "Metadata": {"sha256": hashes[key]}}
            _write_once(path, state, f"put_{key}", "s3.put_object", request,
                lambda file_path=file_path, request=request: c["s3"].put_object(**request, Body=file_path.read_bytes()))
        state["owned"].setdefault("objects", {})[key] = keys[key]
        _save(path, state)
    # The existing S03 artifact reference is reused read-only to bootstrap the
    # new Runtime. S03 itself and its shared artifact object are never changed.
    runtime_arn_pattern = f"arn:aws:bedrock-agentcore:{REGION}:{expected_account}:runtime/{names['runtime']}-*"
    runtime_role_arn = f"arn:aws:iam::{expected_account}:role/{names['runtime_role']}"
    lambda_role_arn = f"arn:aws:iam::{expected_account}:role/{names['lambda_role']}"
    gateway_role_arn = f"arn:aws:iam::{expected_account}:role/{names['gateway_role']}"
    groups = {
        "lambda": f"/aws/lambda/{names['lambda']}",
        "runtime": f"/aws/bedrock-agentcore/runtimes/{names['runtime']}-*",
    }
    lambda_group = f"/aws/lambda/{names['lambda']}"
    lambda_trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
        "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    lambda_policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["logs:FilterLogEvents"],
         "Resource": f"arn:aws:logs:{REGION}:{expected_account}:log-group:{state['s03_log_group']}:*"},
        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{REGION}:{expected_account}:log-group:{lambda_group}:*"}]}
    gateway_trust = _trust("bedrock-agentcore.amazonaws.com", expected_account,
        f"arn:aws:bedrock-agentcore:{REGION}:{expected_account}:gateway/*")
    gateway_policy = _policy(["lambda:InvokeFunction"],
        [f"arn:aws:lambda:{REGION}:{expected_account}:function:{names['lambda']}"])
    create_log = {"logGroupName": lambda_group}
    if "lambda_log_group" not in state["operations"]:
        _write_once(path, state, "lambda_log_group", "logs.create_log_group", create_log,
                    lambda: c["logs"].create_log_group(**create_log))
    tag_log = {"logGroupName": lambda_group, "tags": state["ownership_tag"]}
    if "lambda_log_group_tag" not in state["operations"]:
        _write_once(path, state, "lambda_log_group_tag", "logs.tag_log_group", tag_log,
                    lambda: c["logs"].tag_log_group(**tag_log))
    state["owned"]["lambda_log_group"] = lambda_group
    _save(path, state)
    retention = {"logGroupName": lambda_group, "retentionInDays": 7}
    if "lambda_log_retention" not in state["operations"]:
        _write_once(path, state, "lambda_log_retention", "logs.put_retention_policy", retention,
                    lambda: c["logs"].put_retention_policy(**retention))
    for role_name, trust, policy in ((names["lambda_role"], lambda_trust, lambda_policy),
                                     (names["gateway_role"], gateway_trust, gateway_policy)):
        role_key = "role_" + role_name
        if role_key not in state["operations"]:
            req = {"RoleName": role_name, "AssumeRolePolicyDocument": json.dumps(trust),
                   "Description": f"S04 reader {run_id} dedicated role",
                   "Tags": [{"Key": "S04ReaderRun", "Value": run_id}]}
            response = _write_once(path, state, role_key, "iam.create_role", req,
                                   lambda req=req: c["iam"].create_role(**req))
            state["owned"].setdefault("roles", []).append(role_name)
            state["role_arns"] = state.get("role_arns", {})
            state["role_arns"][role_name] = response.get("Role", {}).get("Arn", f"arn:aws:iam::{expected_account}:role/{role_name}")
            _save(path, state)
        if role_name not in state["owned"].setdefault("roles", []):
            state["owned"]["roles"].append(role_name)
            _save(path, state)
        policy_key = "policy_" + role_name
        if policy_key not in state["operations"]:
            req = {"RoleName": role_name, "PolicyName": "S03ReaderLeastPrivilege", "PolicyDocument": json.dumps(policy)}
            _write_once(path, state, policy_key, "iam.put_role_policy", req,
                        lambda req=req: c["iam"].put_role_policy(**req))
    _trust_readback(c["iam"], names["lambda_role"], lambda_trust)
    _policy_readback(c["iam"], names["lambda_role"], "S03ReaderLeastPrivilege", lambda_policy)
    _trust_readback(c["iam"], names["gateway_role"], gateway_trust)
    _policy_readback(c["iam"], names["gateway_role"], "S03ReaderLeastPrivilege", gateway_policy)
    if "lambda_function" not in state["operations"]:
        req = {"FunctionName": names["lambda"], "Runtime": "python3.13", "Role": lambda_role_arn,
            "Handler": "s03_invocation_reader.lambda_handler", "Code": {"S3Bucket": bucket, "S3Key": keys["lambda"]},
            "Description": f"S04 S03 read-only invocation summary run {run_id}", "Timeout": 30, "MemorySize": 512,
            "Publish": False, "Architectures": ["x86_64"], "Tags": state["ownership_tag"],
            "Environment": {"Variables": {"S03_LOG_GROUP": state["s03_log_group"]}}}
        _write_once(path, state, "lambda_function", "lambda.create_function", req,
                    lambda: c["lambda"].create_function(**req))
        state["owned"]["lambda"] = names["lambda"]
        _save(path, state)
    else:
        state["owned"]["lambda"] = names["lambda"]
    _wait_lambda(c["lambda"], names["lambda"])
    lambda_config = c["lambda"].get_function(FunctionName=names["lambda"])["Configuration"]
    lambda_tags = c["lambda"].list_tags(Resource=lambda_config["FunctionArn"]).get("Tags", {})
    expected_lambda_hash = base64.b64encode(bytes.fromhex(hashes["lambda"])).decode("ascii")
    if (lambda_config.get("Role") != lambda_role_arn
            or lambda_config.get("CodeSha256") != expected_lambda_hash
            or lambda_config.get("Description") != f"S04 S03 read-only invocation summary run {run_id}"
            or lambda_tags != state["ownership_tag"]):
        raise RuntimeError("Lambda role, package hash, description, or ownership tag readback differs; stop deployment")
    gateway_request = {"name": names["gateway"], "roleArn": gateway_role_arn, "protocolType": "MCP",
        "authorizerType": "AWS_IAM", "description": f"S04 read-only S03 invocation reader run {run_id}",
        "clientToken": hashlib.sha256(f"{run_id}|gateway".encode()).hexdigest(), "tags": state["ownership_tag"]}
    if "gateway" not in state["operations"]:
        response = _write_once(path, state, "gateway", "bedrock-agentcore-control.create_gateway", gateway_request,
            lambda: c["bedrock-agentcore-control"].create_gateway(**gateway_request))
        gw = response.get("gateway", response)
        state["gateway_id"] = gw.get("gatewayId") or gw.get("id")
        state["gateway_arn"] = gw.get("gatewayArn") or gw.get("arn")
        state["gateway_url"] = gw.get("gatewayUrl") or gw.get("endpoint")
        state["owned"]["gateway"] = {"id": state["gateway_id"], "arn": state["gateway_arn"]}
        _save(path, state)
    else:
        response = state["operations"]["gateway"].get("response", {})
        gw = response.get("gateway", response)
        state["gateway_id"] = state.get("gateway_id") or gw.get("gatewayId") or gw.get("id")
        state["gateway_arn"] = state.get("gateway_arn") or gw.get("gatewayArn") or gw.get("arn")
        state["gateway_url"] = state.get("gateway_url") or gw.get("gatewayUrl") or gw.get("endpoint")
        if state.get("gateway_id") and state.get("gateway_arn"):
            state["owned"]["gateway"] = {"id": state["gateway_id"], "arn": state["gateway_arn"]}
            _save(path, state)
    if not state.get("gateway_id") or not state.get("gateway_arn") or not state.get("gateway_url"):
        raise RuntimeError("Gateway create result is incomplete; inspect exact Gateway before proceeding")
    gateway_trust_exact = _trust("bedrock-agentcore.amazonaws.com", expected_account, state["gateway_arn"])
    req = {"RoleName": names["gateway_role"], "PolicyDocument": json.dumps(gateway_trust_exact)}
    if "gateway_trust_narrow" not in state["operations"]:
        _write_once(path, state, "gateway_trust_narrow", "iam.update_assume_role_policy", req,
                    lambda: c["iam"].update_assume_role_policy(**req))
    _trust_readback(c["iam"], names["gateway_role"], gateway_trust_exact)
    schema = {"inlinePayload": [{"name": "read_s03_invocation",
        "description": "Read one bounded S03 invocation summary by exact UUID and timestamp.",
        "inputSchema": {"type": "object", "properties": {
            "target_invocation_id": {"type": "string"}, "target_timestamp_ms": {"type": "integer"},
            "max_results": {"type": "integer", "description": "Integer from 1 to 5."}},
            "required": ["target_invocation_id", "target_timestamp_ms", "max_results"]}}]}
    target_request = {"gatewayIdentifier": state["gateway_id"], "name": names["target"],
        "description": f"S04 S03 reader target run {run_id}",
        "targetConfiguration": {"mcp": {"lambda": {"lambdaArn": f"arn:aws:lambda:{REGION}:{expected_account}:function:{names['lambda']}", "toolSchema": schema}}},
        "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
        "clientToken": hashlib.sha256(f"{run_id}|target".encode()).hexdigest()}
    if "gateway_target" not in state["operations"]:
        response = _write_once(path, state, "gateway_target", "bedrock-agentcore-control.create_gateway_target", target_request,
            lambda: c["bedrock-agentcore-control"].create_gateway_target(**target_request))
        target = response.get("target", response)
        state["target_id"] = target.get("targetId") or target.get("id")
        state["owned"]["gateway_target"] = state["target_id"]
        _save(path, state)
    else:
        response = state["operations"]["gateway_target"].get("response", {})
        target = response.get("target", response)
        state["target_id"] = state.get("target_id") or target.get("targetId") or target.get("id")
        if state.get("target_id"):
            state["owned"]["gateway_target"] = state["target_id"]
            _save(path, state)
    if not state.get("gateway_id") or not state.get("target_id"):
        raise RuntimeError("Exact Gateway/target identifiers are unresolved; inspect status before proceeding")
    gateway_live = _wait_ready(lambda: c["bedrock-agentcore-control"].get_gateway(
        gatewayIdentifier=state["gateway_id"]), "Reader Gateway")
    target_live = _wait_ready(lambda: c["bedrock-agentcore-control"].get_gateway_target(
        gatewayIdentifier=state["gateway_id"], targetId=state["target_id"]), "Reader Gateway target")
    runtime_policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["bedrock-agentcore:InvokeGateway"], "Resource": state["gateway_arn"]},
        {"Effect": "Allow", "Action": ["bedrock:InvokeModelWithResponseStream"], "Resource": MODEL_ARN},
        {"Effect": "Allow", "Action": ["s3:GetObject"],
         "Resource": f"arn:aws:s3:::{state['borrowed_artifact']['bucket']}/{state['borrowed_artifact']['key']}"},
        {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:DescribeLogStreams"],
         "Resource": f"arn:aws:logs:{REGION}:{expected_account}:log-group:/aws/bedrock-agentcore/runtimes/{names['runtime']}-*"},
        {"Effect": "Allow", "Action": ["logs:PutResourcePolicy"],
         "Resource": f"arn:aws:logs:{REGION}:{expected_account}:log-group:/aws/bedrock-agentcore/runtimes/{names['runtime']}-*"},
        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{REGION}:{expected_account}:log-group:/aws/bedrock-agentcore/runtimes/{names['runtime']}-*:log-stream:*"}]}
    runtime_trust = _trust("bedrock-agentcore.amazonaws.com", expected_account, runtime_arn_pattern)
    if "runtime_role" not in state["operations"]:
        req = {"RoleName": names["runtime_role"], "AssumeRolePolicyDocument": json.dumps(runtime_trust),
               "Description": f"S04 reader Runtime {run_id} role"}
        response = _write_once(path, state, "runtime_role", "iam.create_role", req,
                               lambda: c["iam"].create_role(**req))
        state["owned"].setdefault("roles", []).append(names["runtime_role"])
        state["role_arns"][names["runtime_role"]] = response.get("Role", {}).get("Arn", runtime_role_arn)
        _save(path, state)
    if names["runtime_role"] not in state["owned"].setdefault("roles", []):
        state["owned"]["roles"].append(names["runtime_role"])
        _save(path, state)
    if "runtime_policy" not in state["operations"]:
        req = {"RoleName": names["runtime_role"], "PolicyName": "S03ReaderLeastPrivilege", "PolicyDocument": json.dumps(runtime_policy)}
        _write_once(path, state, "runtime_policy", "iam.put_role_policy", req,
                    lambda: c["iam"].put_role_policy(**req))
    _trust_readback(c["iam"], names["runtime_role"], runtime_trust)
    _policy_readback(c["iam"], names["runtime_role"], "S03ReaderLeastPrivilege", runtime_policy)
    s03_artifact = snapshot["agentRuntimeArtifact"]
    create_request = {"agentRuntimeName": names["runtime"], "clientToken": hashlib.sha256(f"{run_id}|create".encode()).hexdigest(),
        "tags": state["ownership_tag"],
        "roleArn": runtime_role_arn, "agentRuntimeArtifact": s03_artifact, "platformVersion": "V2",
        "networkConfiguration": state["s03_snapshot"].get("networkConfiguration") or {"networkMode": "PUBLIC"},
        "protocolConfiguration": state["s03_snapshot"].get("protocolConfiguration") or {"serverProtocol": "HTTP"},
        "environmentVariables": {}, "metadataConfiguration": snapshot.get("metadataConfiguration"),
        "lifecycleConfiguration": state["s03_snapshot"].get("lifecycleConfiguration") or {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800}}
    if "create_runtime" not in state["operations"]:
        response = _write_once(path, state, "create_runtime", "bedrock-agentcore-control.create_agent_runtime", create_request,
            lambda: c["bedrock-agentcore-control"].create_agent_runtime(**create_request))
        state["runtime_id"] = response.get("agentRuntimeId")
        state["runtime_arn"] = response.get("agentRuntimeArn")
        state["runtime_version"] = response.get("agentRuntimeVersion")
        state["owned"]["runtime"] = {"id": state["runtime_id"], "arn": state["runtime_arn"]}
        _save(path, state)
    else:
        response = state["operations"]["create_runtime"].get("response", {})
        state["runtime_id"] = state.get("runtime_id") or response.get("agentRuntimeId")
        state["runtime_arn"] = state.get("runtime_arn") or response.get("agentRuntimeArn")
        state["runtime_version"] = state.get("runtime_version") or response.get("agentRuntimeVersion")
        if state.get("runtime_id") and state.get("runtime_arn"):
            state["owned"]["runtime"] = {"id": state["runtime_id"], "arn": state["runtime_arn"]}
            _save(path, state)
    if not state.get("runtime_id"):
        raise RuntimeError("Runtime create response is unresolved; run status/resume")
    exact_trust = _trust("bedrock-agentcore.amazonaws.com", expected_account, state["runtime_arn"])
    req = {"RoleName": names["runtime_role"], "PolicyDocument": json.dumps(exact_trust)}
    if "runtime_trust_narrow" not in state["operations"]:
        _write_once(path, state, "runtime_trust_narrow", "iam.update_assume_role_policy", req,
                    lambda: c["iam"].update_assume_role_policy(**req))
    _trust_readback(c["iam"], names["runtime_role"], exact_trust)
    first_ready = _wait_runtime(c["bedrock-agentcore-control"], state["runtime_id"], state.get("runtime_version"))
    if first_ready.get("metadataConfiguration", {}).get("requireMMDSV2") is not True:
        raise RuntimeError("S04 bootstrap Runtime lacks confirmed MMDSv2; do not update or invoke")
    state["phase"] = "bootstrap_ready"
    _save(path, state)
    reader_artifact = {"codeConfiguration": {"code": {"s3": {"bucket": bucket, "prefix": keys["reader"]}},
        "runtime": "PYTHON_3_13", "entryPoint": ["runtime_app.py"]}}
    exposed_tool_name = f"{names['target']}___read_s03_invocation"
    env = {"S04_GATEWAY_URL": mcp_endpoint_url(state["gateway_url"]), "S04_GATEWAY_TOOL_NAME": exposed_tool_name}
    update = {"agentRuntimeId": state["runtime_id"], "agentRuntimeArtifact": reader_artifact,
        "roleArn": runtime_role_arn, "platformVersion": first_ready.get("platformVersion", "V2"),
        "networkConfiguration": first_ready.get("networkConfiguration"),
        "protocolConfiguration": first_ready.get("protocolConfiguration"), "environmentVariables": env,
        "metadataConfiguration": first_ready.get("metadataConfiguration"),
        "lifecycleConfiguration": first_ready.get("lifecycleConfiguration"),
        "clientToken": hashlib.sha256(f"{run_id}|reader-update".encode()).hexdigest()}
    if "update_runtime" not in state["operations"]:
        response = _write_once(path, state, "update_runtime", "bedrock-agentcore-control.update_agent_runtime", update,
            lambda: c["bedrock-agentcore-control"].update_agent_runtime(**update))
        state["runtime_version"] = response.get("agentRuntimeVersion")
        _save(path, state)
    ready = _wait_runtime(c["bedrock-agentcore-control"], state["runtime_id"], state.get("runtime_version"))
    for field in ("roleArn", "networkConfiguration", "protocolConfiguration", "metadataConfiguration", "lifecycleConfiguration", "environmentVariables", "agentRuntimeArtifact"):
        if ready.get(field) != update.get(field):
            raise RuntimeError(f"Runtime readback differs for {field}; do not invoke")
    state["phase"] = "ready"
    _save(path, state)
    print(f"S04 reader Runtime and Gateway are READY for run {run_id}; resource identifiers are saved in local state.")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _wait_lambda(client: Any, name: str) -> None:
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        config = client.get_function(FunctionName=name)["Configuration"]
        if config.get("State") == "Active" and config.get("LastUpdateStatus") in {None, "Successful"}:
            return
        if config.get("State") == "Failed" or config.get("LastUpdateStatus") == "Failed":
            raise RuntimeError("Reader Lambda deployment failed; preserve state and inspect")
        time.sleep(5)
    raise RuntimeError("Reader Lambda activation timed out; inspect before continuing")


def _wait_ready(fetch: Callable[[], dict[str, Any]], label: str) -> dict[str, Any]:
    deadline = time.monotonic() + 900
    in_progress = {"CREATING", "UPDATING", "SYNCHRONIZING", "CREATE_PENDING_AUTH", "UPDATE_PENDING_AUTH"}
    while time.monotonic() < deadline:
        current = fetch()
        if current.get("status") == "READY":
            return current
        if current.get("status") not in in_progress:
            raise RuntimeError(f"{label} reached {current.get('status')}; preserve state and inspect")
        time.sleep(5)
    raise RuntimeError(f"{label} readiness timed out; inspect exact resource before continuing")


def _wait_not_found(call: Callable[[], Any], label: str) -> None:
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        try:
            current = call()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {
                "ResourceNotFoundException", "ResourceNotFound", "NoSuchEntity", "NoSuchKey", "404",
            }:
                return
            raise
        status_value = current.get("status") if isinstance(current, dict) else None
        if status_value == "ABSENT":
            return
        if status_value in {"FAILED", "DELETE_UNSUCCESSFUL", "DELETE_FAILED"}:
            raise RuntimeError(f"{label} deletion failed with status {status_value}; stop cleanup")
        time.sleep(5)
    raise RuntimeError(f"Timed out waiting for {label} to disappear; preserve local state")


def status(run_id: str, expected_account: str, expected_region: str, profile: str | None) -> None:
    path, state = _load(run_id)
    c = _clients(profile)
    _identity(c, expected_account, expected_region)
    if state["account_id"] != expected_account:
        raise RuntimeError("Saved state belongs to another account")
    live: dict[str, Any] = {}
    def observe(key: str, call: Callable[[], Any]) -> None:
        try:
            live[key] = call()
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in {"ResourceNotFoundException", "NoSuchEntity", "ResourceNotFound", "404"}:
                live[key] = {"status": "not_found"}
            else:
                live[key] = {"read_error": code}
    if state.get("runtime_id"):
        observe("runtime", lambda: c["bedrock-agentcore-control"].get_agent_runtime(agentRuntimeId=state["runtime_id"]))
        group_name = runtime_log_group(state["runtime_id"])
        group_arn = runtime_log_group_arn(state["runtime_id"], expected_account)
        def read_runtime_logs() -> dict[str, Any]:
            group = _exact_log_group(c["logs"], group_name)
            policies = _runtime_log_policies(c["logs"], group_arn)
            return {"group": group or {"status": "not_found"},
                    "resource_policies": policies}
        observe("runtime_logs", read_runtime_logs)
    if state.get("gateway_id"):
        observe("gateway", lambda: c["bedrock-agentcore-control"].get_gateway(gatewayIdentifier=state["gateway_id"]))
    if state.get("target_id") and state.get("gateway_id"):
        observe("target", lambda: c["bedrock-agentcore-control"].get_gateway_target(
            gatewayIdentifier=state["gateway_id"], targetId=state["target_id"]))
    if state.get("owned", {}).get("lambda"):
        observe("lambda", lambda: c["lambda"].get_function(FunctionName=state["owned"]["lambda"])["Configuration"])
    if state.get("owned", {}).get("lambda_log_group"):
        group_name = state["owned"]["lambda_log_group"]
        def read_lambda_log_group() -> dict[str, Any]:
            token = None
            while True:
                request = {"logGroupNamePrefix": group_name}
                if token:
                    request["nextToken"] = token
                page = c["logs"].describe_log_groups(**request)
                match = next((item for item in page.get("logGroups", [])
                              if item.get("logGroupName") == group_name), None)
                if match:
                    match["tags"] = c["logs"].list_tags_log_group(logGroupName=group_name).get("tags", {})
                    return match
                token = page.get("nextToken")
                if not token:
                    return {"status": "not_found"}
        observe("lambda_log_group", read_lambda_log_group)
    for role in state.get("owned", {}).get("roles", []):
        observe(f"role:{role}", lambda role=role: c["iam"].get_role(RoleName=role)["Role"])
    if state.get("owned", {}).get("objects"):
        try:
            versioning = _bucket_versioning(c["s3"], state["bucket"], expected_account)
            live["bucket_versioning"] = versioning or "Unversioned"
            live["unverified_s3_version_residue"] = versioning in {"Enabled", "Suspended"}
        except ClientError as exc:
            live["bucket_versioning"] = {"read_error": exc.response.get("Error", {}).get("Code")}
            live["unverified_s3_version_residue"] = True
        for key, object_key in state["owned"]["objects"].items():
            def read_object(key: str = key, object_key: str = object_key) -> dict[str, Any]:
                try:
                    head = c["s3"].head_object(Bucket=state["bucket"], Key=object_key,
                                               ExpectedBucketOwner=expected_account)
                    return {"status": "present", "key": object_key,
                            "sha256": head.get("Metadata", {}).get("sha256"),
                            "content_length": head.get("ContentLength")}
                except ClientError as exc:
                    code = exc.response.get("Error", {}).get("Code")
                    if code in {"NoSuchKey", "NoSuchBucket", "NotFound", "404"}:
                        return {"status": "not_found", "key": object_key}
                    return {"read_error": code, "key": object_key}
            live[f"s3_object:{key}"] = read_object()
    print(json.dumps({"run_id": run_id, "phase": state.get("phase"), "operations": state.get("operations"),
        "owned": state.get("owned"), "live": live, "region": state.get("region")}, indent=2, default=str))


def invoke(run_id: str, expected_account: str, expected_region: str, invocation_id: str,
           timestamp_ms: int, profile: str | None) -> None:
    path, state = _load(run_id)
    if state.get("phase") != "ready":
        raise RuntimeError("Invoke requires the exact reader Runtime READY after artifact and environment readback")
    if state.get("positive_attempts"):
        raise RuntimeError("This run already has a positive invoke attempt; do not replay it")
    if not isinstance(timestamp_ms, int) or isinstance(timestamp_ms, bool) or timestamp_ms <= 0:
        raise ValueError("timestamp_ms must be the exact positive integer printed by S03")
    if not isinstance(invocation_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", invocation_id):
        raise ValueError("invocation_id must be the exact UUID printed by S03")
    c = _clients(profile, writes=True)
    _identity(c, expected_account, expected_region)
    runtime = c["bedrock-agentcore"]
    session_id = str(uuid.uuid4()) + "-s04-reader"
    attempt = {"status": "requested", "at_epoch": int(time.time()), "session_id": session_id,
               "target_invocation_id": invocation_id, "target_timestamp_ms": timestamp_ms}
    state["positive_attempts"] = [attempt]
    state["sessions"] = [session_id]
    _save(path, state)
    prompt = ("S03の実行ログを確認してください。次の値を変更せずread_s03_invocationに渡してください。\n"
        f"target_invocation_id={invocation_id}\ntarget_timestamp_ms={timestamp_ms}\nmax_results=5")
    try:
        response = runtime.invoke_agent_runtime(agentRuntimeArn=state["runtime_arn"], qualifier="DEFAULT",
            runtimeSessionId=session_id, contentType="application/json", accept="application/json",
            payload=json.dumps({"prompt": prompt}, ensure_ascii=False).encode("utf-8"))
        raw = response["response"].read()
        result = json.loads(raw)
    except Exception as exc:
        attempt.update({"status": "outcome_unknown", "error_type": type(exc).__name__})
        _save(path, state)
        raise RuntimeError("Invoke outcome is unknown; do not resend. Preserve the session and run status/resume.") from None
    attempt.update({"status": "response_received", "invocation_id": result.get("invocation_id"),
                    "http_status_code": response.get("ResponseMetadata", {}).get("HTTPStatusCode"),
                    "response": result})
    _save(path, state)
    print(json.dumps({"session_id": session_id, "result": result}, ensure_ascii=False, indent=2))


def stop(run_id: str, expected_account: str, expected_region: str, profile: str | None) -> None:
    path, state = _load(run_id)
    sessions = state.get("sessions", [])
    if not sessions:
        raise RuntimeError("No saved S04 reader session is available to stop")
    if state.get("stop_attempts"):
        raise RuntimeError("Stop already has a recorded attempt; inspect status/resume before taking action")
    c = _clients(profile, writes=True)
    _identity(c, expected_account, expected_region)
    session = sessions[-1]
    attempt = {"session_id": session, "status": "requested", "at_epoch": int(time.time())}
    state["stop_attempts"] = [attempt]
    _save(path, state)
    try:
        response = c["bedrock-agentcore"].stop_runtime_session(
            agentRuntimeArn=state["runtime_arn"], runtimeSessionId=session)
    except Exception as exc:
        attempt.update({"status": "outcome_unknown", "error_type": type(exc).__name__})
        _save(path, state)
        raise RuntimeError("Stop outcome is unknown; do not resend. Read the exact session status.") from None
    metadata = response.get("ResponseMetadata", {})
    attempt.update({"http_status_code": metadata.get("HTTPStatusCode"),
                    "request_id": metadata.get("RequestId"), "response": response})
    attempt["status"] = "stopped" if metadata.get("HTTPStatusCode") == 200 else "unconfirmed"
    _save(path, state)
    if attempt["status"] != "stopped":
        raise RuntimeError("Stop result was not confirmed; do not retry")
    print(json.dumps({"session_id": session, "stop": attempt}, indent=2, default=str))


def negative(run_id: str, expected_account: str, expected_region: str, profile: str | None) -> None:
    path, state = _load(run_id)
    if state.get("phase") != "ready":
        raise RuntimeError("Negative checks require the exact READY reader Gateway")
    positives = state.get("positive_attempts", [])
    if len(positives) != 1 or positives[0].get("status") != "response_received":
        raise RuntimeError("Negative checks require one recorded reader response; no timestamp may be invented")
    target_timestamp_ms = positives[0].get("target_timestamp_ms")
    if not isinstance(target_timestamp_ms, int) or isinstance(target_timestamp_ms, bool) or target_timestamp_ms <= 0:
        raise RuntimeError("Saved positive attempt lacks its exact S03 timestamp; preserve state and stop")
    if state.get("negative_attempts"):
        raise RuntimeError("Negative request already started; do not replay either request")
    c = _clients(profile, writes=True)
    _identity(c, expected_account, expected_region)
    endpoint = mcp_endpoint_url(state["gateway_url"])
    attempts: list[dict[str, Any]] = []
    state["negative_attempts"] = attempts
    _save(path, state)
    unsigned = {"jsonrpc": "2.0", "id": "s04-reader-unsigned", "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "s04-reader-check", "version": "1.0"}}}
    attempt = {"kind": "unsigned_initialize", "status": "requested", "at_epoch": int(time.time())}
    attempts.append(attempt); _save(path, state)
    try:
        result = _post_gateway_jsonrpc(endpoint, unsigned)
        attempt.update({"status": "response_received", **result})
    except Exception as exc:
        attempt.update({"status": "outcome_unknown", "error_type": type(exc).__name__}); _save(path, state)
        raise RuntimeError("Unsigned initialize result is unknown; do not send another request") from None
    _save(path, state)
    signed_attempt = {"kind": "signed_invalid_uuid", "status": "requested", "at_epoch": int(time.time())}
    attempts.append(signed_attempt); _save(path, state)
    signed_request = {"jsonrpc": "2.0", "id": "s04-reader-invalid-uuid",
        "method": "tools/call", "params": {"name": f"{state['names']['target']}___read_s03_invocation", "arguments": {
            "target_invocation_id": "not-a-uuid", "target_timestamp_ms": target_timestamp_ms, "max_results": 1}}}
    try:
        auth = _signed_gateway_auth(profile)
        result = _post_gateway_jsonrpc(endpoint, signed_request, auth=auth)
        signed_attempt.update({"status": "response_received", **result})
    except Exception as exc:
        signed_attempt.update({"status": "outcome_unknown", "error_type": type(exc).__name__}); _save(path, state)
        raise RuntimeError("Signed invalid-UUID result is unknown; do not replay") from None
    _save(path, state)
    print(json.dumps({"requests": attempts}, ensure_ascii=False, indent=2))


def _post_gateway_jsonrpc(endpoint: str, payload: dict[str, Any], auth: Any = None) -> dict[str, Any]:
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
               "MCP-Protocol-Version": "2024-11-05"}
    with httpx.Client(auth=auth, timeout=httpx.Timeout(10, connect=5), follow_redirects=False,
                      headers=headers) as client:
        response = client.post(endpoint, json=payload)
    body = response.content[:2048].decode("utf-8", errors="replace")
    return {"http_status": response.status_code, "content_type": response.headers.get("content-type"),
            "body": body}


def _signed_gateway_auth(profile: str | None) -> Any:
    from mcp_proxy_for_aws.sigv4_helper import SigV4HTTPXAuth
    session = boto3.Session(profile_name=profile, region_name=REGION)
    credentials = session.get_credentials()
    if credentials is None:
        raise RuntimeError("No AWS credentials available for the signed Gateway request")
    return SigV4HTTPXAuth(credentials, "bedrock-agentcore", REGION)


def resume(run_id: str, expected_account: str, expected_region: str, profile: str | None) -> None:
    """Read exact current resources after an uncertain write; never send writes."""
    path, state = _load(run_id)
    c = _clients(profile)
    _identity(c, expected_account, expected_region)
    if state.get("account_id") != expected_account:
        raise RuntimeError("Saved state belongs to another account")
    pending = [(name, value) for name, value in state.get("operations", {}).items()
               if value.get("status") in {"requested", "outcome_unknown"}]
    if len(pending) != 1:
        status(run_id, expected_account, expected_region, profile)
        print("No single uncertain write was available for reconciliation; no write was sent.")
        return
    step, record = pending[0]
    request = record.get("request", {})
    response: dict[str, Any] | None = None
    if step.startswith("put_"):
        head = c["s3"].head_object(Bucket=request["Bucket"], Key=request["Key"], ExpectedBucketOwner=expected_account)
        metadata = head.get("Metadata", {})
        expected = request.get("Metadata", {}).get("sha256")
        if metadata.get("sha256") != expected:
            raise RuntimeError("S3 object exists but metadata does not match this run; preserve state")
        response = {"VersionId": head.get("VersionId"), "ETag": head.get("ETag"), "ContentLength": head.get("ContentLength")}
        state["owned"].setdefault("objects", {})[step.removeprefix("put_")] = request["Key"]
    elif record.get("operation") == "iam.create_role":
        role = c["iam"].get_role(RoleName=request["RoleName"]).get("Role", {})
        role_tags = {item.get("Key"): item.get("Value") for item in role.get("Tags", [])}
        if role.get("Description") != request.get("Description") or role_tags != state.get("ownership_tag"):
            raise RuntimeError("Exact IAM role does not match the saved create request")
        response = {"Role": {"Arn": role.get("Arn"), "RoleName": role.get("RoleName")}}
        state["owned"].setdefault("roles", []).append(request["RoleName"])
    elif record.get("operation") == "logs.create_log_group":
        groups = c["logs"].describe_log_groups(logGroupNamePrefix=request["logGroupName"]).get("logGroups", [])
        if not any(item.get("logGroupName") == request["logGroupName"] for item in groups):
            raise RuntimeError("Exact log group is absent; no write was replayed")
        tags = c["logs"].list_tags_log_group(logGroupName=request["logGroupName"]).get("tags", {})
        if tags != state.get("ownership_tag"):
            raise RuntimeError("Exact log group lacks this run's ownership tag; preserve it")
        response = {"logGroupName": request["logGroupName"]}
        state["owned"]["lambda_log_group"] = request["logGroupName"]
    elif record.get("operation") == "lambda.create_function":
        conf = c["lambda"].get_function(FunctionName=request["FunctionName"])["Configuration"]
        tags = c["lambda"].list_tags(Resource=conf["FunctionArn"]).get("Tags", {})
        package_hash = state.get("packages", {}).get("lambda", {}).get("sha256")
        expected_code_hash = base64.b64encode(bytes.fromhex(package_hash)).decode("ascii") if package_hash else None
        if (conf.get("Role") != request.get("Role") or conf.get("Description") != request.get("Description")
                or tags != state.get("ownership_tag") or conf.get("CodeSha256") != expected_code_hash):
            raise RuntimeError("Exact Lambda does not match the saved create request")
        response = {"FunctionName": conf.get("FunctionName"), "FunctionArn": conf.get("FunctionArn")}
        state["owned"]["lambda"] = request["FunctionName"]
    elif record.get("operation") == "bedrock-agentcore-control.create_gateway":
        pages = c["bedrock-agentcore-control"].get_paginator("list_gateways").paginate(maxResults=100)
        matches = [item for page in pages for item in page.get("gateways", []) if item.get("name") == request["name"]]
        if len(matches) != 1 or matches[0].get("roleArn") != request.get("roleArn"):
            raise RuntimeError("Gateway was not uniquely reconciled against the saved name and role")
        gw = matches[0]
        gw_readback = c["bedrock-agentcore-control"].get_gateway(gatewayIdentifier=gw.get("gatewayId"))
        tags = c["bedrock-agentcore-control"].list_tags_for_resource(resourceArn=gw_readback.get("gatewayArn")).get("tags", {})
        if (tags != state.get("ownership_tag") or gw_readback.get("roleArn") != request.get("roleArn")
                or gw_readback.get("authorizerType") != request.get("authorizerType")
                or gw_readback.get("protocolType") != request.get("protocolType")):
            raise RuntimeError("Gateway lacks this run's ownership tag; preserve it")
        response = {"gatewayId": gw.get("gatewayId"), "gatewayArn": gw.get("gatewayArn"), "gatewayUrl": gw.get("gatewayUrl")}
        state.update({"gateway_id": gw.get("gatewayId"), "gateway_arn": gw.get("gatewayArn"), "gateway_url": gw.get("gatewayUrl")})
        state["owned"]["gateway"] = {"id": gw.get("gatewayId"), "arn": gw.get("gatewayArn")}
    elif record.get("operation") == "bedrock-agentcore-control.create_agent_runtime":
        pages = c["bedrock-agentcore-control"].get_paginator("list_agent_runtimes").paginate(maxResults=100)
        matches = [item for page in pages for item in page.get("agentRuntimes", [])
                   if item.get("agentRuntimeName") == request["agentRuntimeName"]]
        if len(matches) != 1 or matches[0].get("roleArn") != request.get("roleArn"):
            raise RuntimeError("Runtime was not uniquely reconciled against the saved name and role")
        runtime = matches[0]
        runtime_readback = c["bedrock-agentcore-control"].get_agent_runtime(agentRuntimeId=runtime.get("agentRuntimeId"))
        tags = c["bedrock-agentcore-control"].list_tags_for_resource(resourceArn=runtime_readback.get("agentRuntimeArn")).get("tags", {})
        if (tags != state.get("ownership_tag") or runtime_readback.get("roleArn") != request.get("roleArn")
                or runtime_readback.get("agentRuntimeArtifact") != request.get("agentRuntimeArtifact")
                or runtime_readback.get("networkConfiguration") != request.get("networkConfiguration")
                or runtime_readback.get("protocolConfiguration") != request.get("protocolConfiguration")):
            raise RuntimeError("Runtime lacks this run's ownership tag; preserve it")
        response = {key: runtime.get(key) for key in ("agentRuntimeId", "agentRuntimeArn", "agentRuntimeVersion")}
        state.update({"runtime_id": response.get("agentRuntimeId"), "runtime_arn": response.get("agentRuntimeArn"),
                      "runtime_version": response.get("agentRuntimeVersion")})
        state["owned"]["runtime"] = {"id": state["runtime_id"], "arn": state["runtime_arn"]}
    else:
        status(run_id, expected_account, expected_region, profile)
        print(f"Uncertain {record.get('operation')} requires manual comparison to the saved request; no write was replayed.")
        return
    record.update({"status": "accepted", "response": response, "reconciled_read_only": True})
    _save(path, state)
    print(f"Read-only reconciliation matched {step}; saved the observed response. Run deploy again to continue remaining accepted steps.")


def cleanup(run_id: str, expected_account: str, expected_region: str, profile: str | None) -> None:
    path, state = _load(run_id)
    c = _clients(profile, writes=True)
    _identity(c, expected_account, expected_region)
    if state.get("account_id") != expected_account:
        raise RuntimeError("Saved state belongs to another account")
    if state.get("sessions") and not state.get("stop_attempts", [{}])[-1].get("status") == "stopped":
        raise RuntimeError("Stop and confirm the exact saved S04 session before cleanup")
    owned = state.get("owned", {})
    tag = state.get("ownership_tag")
    operations = state.get("operations", {})
    if owned.get("runtime") and operations.get("delete_runtime", {}).get("status") != "accepted":
        runtime = c["bedrock-agentcore-control"].get_agent_runtime(agentRuntimeId=owned["runtime"]["id"])
        tags = c["bedrock-agentcore-control"].list_tags_for_resource(resourceArn=runtime.get("agentRuntimeArn")).get("tags", {})
        expected_update = operations.get("update_runtime", {}).get("request", {})
        if (runtime.get("agentRuntimeArn") != owned["runtime"]["arn"] or tags != tag
                or runtime.get("agentRuntimeVersion") != state.get("runtime_version")
                or runtime.get("agentRuntimeArtifact") != expected_update.get("agentRuntimeArtifact")):
            raise RuntimeError("Runtime ARN/tag mismatch; cleanup stopped before any deletion")
        group_name = runtime_log_group(owned["runtime"]["id"])
        group_arn = runtime_log_group_arn(owned["runtime"]["id"], expected_account)
        owned["runtime_log_group"] = group_name
        policies = _runtime_log_policies(c["logs"], group_arn)
        if len(policies) > 1:
            raise RuntimeError("Multiple resource-scoped policies exist for the exact owned Runtime log group; cleanup stopped before any deletion")
        current_policy = policies[0] if policies else None
        saved_policy = owned.get("runtime_log_policy")
        if saved_policy and current_policy:
            if (current_policy.get("policyName") != saved_policy.get("policyName")
                    or current_policy.get("resourceArn") != group_arn
                    or current_policy.get("policyScope") != "RESOURCE"
                    or current_policy.get("revisionId") != saved_policy.get("revisionId")
                    or current_policy.get("policyDocument") != saved_policy.get("policyDocument")):
                raise RuntimeError("Runtime log resource policy changed since cleanup snapshot; stop and review")
        elif not saved_policy:
            if current_policy and (current_policy.get("policyScope") != "RESOURCE"
                    or current_policy.get("resourceArn") != group_arn
                    or not current_policy.get("policyName") or not current_policy.get("revisionId")
                    or not isinstance(current_policy.get("policyDocument"), str)
                    or not current_policy.get("policyDocument")):
                raise RuntimeError("Exact Runtime log resource policy identity is incomplete; cleanup stopped")
            owned["runtime_log_policy"] = current_policy
        _save(path, state)
    if owned.get("gateway") and operations.get("delete_gateway", {}).get("status") != "accepted":
        gateway = c["bedrock-agentcore-control"].get_gateway(gatewayIdentifier=owned["gateway"]["id"])
        tags = c["bedrock-agentcore-control"].list_tags_for_resource(resourceArn=gateway.get("gatewayArn")).get("tags", {})
        if gateway.get("gatewayArn") != owned["gateway"]["arn"] or tags != tag:
            raise RuntimeError("Gateway ARN/tag mismatch; cleanup stopped before any deletion")
    if owned.get("gateway_target") and operations.get("delete_gateway_target", {}).get("status") != "accepted":
        target = c["bedrock-agentcore-control"].get_gateway_target(
            gatewayIdentifier=state["gateway_id"], targetId=owned["gateway_target"])
        if target.get("description") != f"S04 S03 reader target run {run_id}":
            raise RuntimeError("Gateway target run description mismatch; cleanup stopped before any deletion")
    if owned.get("lambda") and operations.get("delete_lambda", {}).get("status") != "accepted":
        config = c["lambda"].get_function(FunctionName=owned["lambda"])["Configuration"]
        tags = c["lambda"].list_tags(Resource=config["FunctionArn"]).get("Tags", {})
        package_hash = state.get("packages", {}).get("lambda", {}).get("sha256")
        expected_code_hash = base64.b64encode(bytes.fromhex(package_hash)).decode("ascii") if package_hash else None
        if (config.get("Description") != f"S04 S03 read-only invocation summary run {run_id}" or tags != tag
                or config.get("CodeSha256") != expected_code_hash):
            raise RuntimeError("Lambda ownership marker mismatch; cleanup stopped before any deletion")
    for role in owned.get("roles", []):
        if operations.get("delete_role_" + role, {}).get("status") == "accepted":
            continue
        value = c["iam"].get_role(RoleName=role).get("Role", {})
        role_tags = {item.get("Key"): item.get("Value") for item in value.get("Tags", [])}
        if role_tags != tag:
            raise RuntimeError(f"IAM role {role} ownership tag mismatch; cleanup stopped before any deletion")
    if owned.get("lambda_log_group") and operations.get("delete_lambda_log_group", {}).get("status") != "accepted":
        tags = c["logs"].list_tags_log_group(logGroupName=owned["lambda_log_group"]).get("tags", {})
        if tags != tag:
            raise RuntimeError("Lambda log group ownership tag mismatch; cleanup stopped before any deletion")
    packages = state.get("packages", {})
    pending_owned_objects = any(operations.get("delete_s3_" + key, {}).get("status") != "accepted"
                                for key in owned.get("objects", {}))
    if pending_owned_objects:
        _require_unversioned_bucket(c["s3"], state["bucket"], expected_account)
    for key, object_key in owned.get("objects", {}).items():
        step = "delete_s3_" + key
        if operations.get(step, {}).get("status") == "accepted":
            continue
        digest = packages.get(key, {}).get("sha256")
        head = c["s3"].head_object(Bucket=state["bucket"], Key=object_key, ExpectedBucketOwner=expected_account)
        if head.get("Metadata", {}).get("sha256") != digest:
            raise RuntimeError(f"S3 object {object_key} hash mismatch; cleanup stopped before any deletion")
    steps: list[tuple[str, str, dict[str, Any], Callable[[], dict[str, Any]]]] = []
    if owned.get("runtime"):
        request = {"agentRuntimeId": owned["runtime"]["id"]}
        steps.append(("delete_runtime", "bedrock-agentcore-control.delete_agent_runtime", request,
                      lambda request=request: c["bedrock-agentcore-control"].delete_agent_runtime(**request)))
    if owned.get("runtime_log_policy"):
        policy = owned["runtime_log_policy"]
        request = {"policyName": policy["policyName"], "resourceArn": policy["resourceArn"],
                   "expectedRevisionId": policy["revisionId"]}
        steps.append(("delete_runtime_log_policy", "logs.delete_resource_policy", request,
                      lambda request=request: c["logs"].delete_resource_policy(**request)))
    if owned.get("runtime_log_group"):
        request = {"logGroupName": owned["runtime_log_group"]}
        steps.append(("delete_runtime_log_group", "logs.delete_log_group", request,
                      lambda request=request: c["logs"].delete_log_group(**request)))
    if owned.get("gateway_target") and state.get("gateway_id"):
        request = {"gatewayIdentifier": state["gateway_id"], "targetId": owned["gateway_target"]}
        steps.append(("delete_gateway_target", "bedrock-agentcore-control.delete_gateway_target", request,
                      lambda request=request: c["bedrock-agentcore-control"].delete_gateway_target(**request)))
    if owned.get("gateway"):
        request = {"gatewayIdentifier": owned["gateway"]["id"]}
        steps.append(("delete_gateway", "bedrock-agentcore-control.delete_gateway", request,
                      lambda request=request: c["bedrock-agentcore-control"].delete_gateway(**request)))
    if owned.get("lambda"):
        request = {"FunctionName": owned["lambda"]}
        steps.append(("delete_lambda", "lambda.delete_function", request,
                      lambda request=request: c["lambda"].delete_function(**request)))
    for role in reversed(owned.get("roles", [])):
        policy_req = {"RoleName": role, "PolicyName": "S03ReaderLeastPrivilege"}
        remove_req = {"RoleName": role}
        steps.append(("delete_policy_" + role, "iam.delete_role_policy", policy_req,
                      lambda policy_req=policy_req: c["iam"].delete_role_policy(**policy_req)))
        steps.append(("delete_role_" + role, "iam.delete_role", remove_req,
                      lambda remove_req=remove_req: c["iam"].delete_role(**remove_req)))
    if owned.get("lambda_log_group"):
        request = {"logGroupName": owned["lambda_log_group"]}
        steps.append(("delete_lambda_log_group", "logs.delete_log_group", request,
                      lambda request=request: c["logs"].delete_log_group(**request)))
    for key, object_key in owned.get("objects", {}).items():
        request = {"Bucket": state["bucket"], "Key": object_key, "ExpectedBucketOwner": expected_account}
        steps.append(("delete_s3_" + key, "s3.delete_object", request,
                      lambda request=request: c["s3"].delete_object(**request)))
    for step, operation, request, call in steps:
        if state["operations"].get(step, {}).get("status") == "accepted":
            continue
        if operation == "iam.delete_role_policy" and f"policy_{request['RoleName']}" not in state["operations"]:
            continue
        if operation == "logs.delete_resource_policy":
            current_policies = _runtime_log_policies(c["logs"], request["resourceArn"])
            saved_policy = owned.get("runtime_log_policy")
            if not current_policies:
                state["operations"][step] = {"operation": operation, "request": request,
                    "status": "accepted", "response": {"status": "ABSENT"}, "reconciled_read_only": True}
                _save(path, state)
                continue
            if (len(current_policies) != 1 or not saved_policy
                    or current_policies[0] != saved_policy):
                raise RuntimeError("Runtime log resource policy no longer matches the exact cleanup snapshot; stop before deletion")
        if operation == "logs.delete_log_group":
            if _exact_log_group(c["logs"], request["logGroupName"]) is None:
                state["operations"][step] = {"operation": operation, "request": request,
                    "status": "accepted", "response": {"status": "ABSENT"}, "reconciled_read_only": True}
                _save(path, state)
                continue
        _write_once(path, state, step, operation, request, call)
        if step == "delete_runtime":
            _wait_not_found(lambda: c["bedrock-agentcore-control"].get_agent_runtime(agentRuntimeId=request["agentRuntimeId"]), "reader Runtime")
        elif operation == "logs.delete_resource_policy":
            def exact_policy():
                policies_now = _runtime_log_policies(c["logs"], request["resourceArn"])
                if policies_now:
                    raise RuntimeError("A resource-scoped policy remains for the exact Runtime log group")
                return {"status": "ABSENT"}
            _wait_not_found(exact_policy, "owned Runtime log resource policy")
        elif operation == "logs.delete_log_group":
            def exact_group_runtime():
                return _exact_log_group(c["logs"], request["logGroupName"]) or {"status": "ABSENT"}
            _wait_not_found(exact_group_runtime, "owned Runtime DEFAULT log group")
        elif step == "delete_gateway_target":
            _wait_not_found(lambda: c["bedrock-agentcore-control"].get_gateway_target(
                gatewayIdentifier=request["gatewayIdentifier"], targetId=request["targetId"]), "Gateway target")
        elif step == "delete_gateway":
            _wait_not_found(lambda: c["bedrock-agentcore-control"].get_gateway(gatewayIdentifier=request["gatewayIdentifier"]), "Gateway")
        elif step == "delete_lambda":
            _wait_not_found(lambda: c["lambda"].get_function(FunctionName=request["FunctionName"]), "reader Lambda")
        elif operation == "iam.delete_role_policy":
            _wait_not_found(lambda: c["iam"].get_role_policy(RoleName=request["RoleName"], PolicyName=request["PolicyName"]), "owned role policy")
        elif operation == "iam.delete_role":
            _wait_not_found(lambda: c["iam"].get_role(RoleName=request["RoleName"]), "owned IAM role")
        elif operation == "logs.delete_log_group":
            def exact_group():
                values = c["logs"].describe_log_groups(logGroupNamePrefix=request["logGroupName"]).get("logGroups", [])
                return next((value for value in values if value.get("logGroupName") == request["logGroupName"]), {"status": "ABSENT"})
            _wait_not_found(exact_group, "owned Lambda log group")
        elif operation == "s3.delete_object":
            _wait_not_found(lambda: c["s3"].head_object(Bucket=request["Bucket"], Key=request["Key"], ExpectedBucketOwner=expected_account), "owned package object")
    state["phase"] = "cleanup_requested"
    _save(path, state)
    print("Cleanup requests recorded for exact owned resources only. Verify each resource is absent with status before ending the run.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("preflight")
    p.add_argument("--run-id", required=True); p.add_argument("--expected-account", required=True); p.add_argument("--expected-region", required=True)
    p.add_argument("--s03-log-group", required=True)
    p = commands.add_parser("deploy")
    p.add_argument("--run-id", required=True); p.add_argument("--expected-account", required=True); p.add_argument("--expected-region", required=True)
    p.add_argument("--reader-lambda-zip", type=Path, required=True); p.add_argument("--reader-runtime-zip", type=Path, required=True)
    p = commands.add_parser("status"); p.add_argument("--run-id", required=True); p.add_argument("--expected-account", required=True); p.add_argument("--expected-region", required=True)
    for name in ("resume", "stop", "cleanup"):
        p = commands.add_parser(name); p.add_argument("--run-id", required=True); p.add_argument("--expected-account", required=True); p.add_argument("--expected-region", required=True)
    p = commands.add_parser("invoke"); p.add_argument("--run-id", required=True); p.add_argument("--expected-account", required=True); p.add_argument("--expected-region", required=True); p.add_argument("--invocation-id", required=True); p.add_argument("--timestamp-ms", type=int, required=True)
    p = commands.add_parser("negative"); p.add_argument("--run-id", required=True); p.add_argument("--expected-account", required=True); p.add_argument("--expected-region", required=True)
    args = parser.parse_args()
    try:
        if args.command == "preflight": preflight(args.run_id, args.expected_account, args.expected_region, args.s03_log_group, args.profile)
        elif args.command == "deploy": deploy(args.run_id, args.expected_account, args.expected_region, args.reader_lambda_zip, args.reader_runtime_zip, args.profile)
        elif args.command == "status": status(args.run_id, args.expected_account, args.expected_region, args.profile)
        elif args.command == "resume": resume(args.run_id, args.expected_account, args.expected_region, args.profile)
        elif args.command == "invoke": invoke(args.run_id, args.expected_account, args.expected_region, args.invocation_id, args.timestamp_ms, args.profile)
        elif args.command == "negative": negative(args.run_id, args.expected_account, args.expected_region, args.profile)
        elif args.command == "stop": stop(args.run_id, args.expected_account, args.expected_region, args.profile)
        else: cleanup(args.run_id, args.expected_account, args.expected_region, args.profile)
    except (ClientError, OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr); return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
