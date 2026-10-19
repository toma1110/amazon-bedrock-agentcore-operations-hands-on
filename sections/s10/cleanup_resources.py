"""Guarded cleanup for the course's own AgentCore practice resources.

The plan is a local, human-reviewed inventory. This module deliberately does
not discover resources by broad account-wide name searches.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable


REGION = "ap-northeast-1"
RUNTIME_SECTIONS = {"s03", "s07", "s08", "s09"}
ROLE_POLICIES = {
    "udemy-agentcore-s03-runtime": {"S03RuntimeExecution", "S04GatewayInvoke"},
    "udemy-agentcore-s04-gateway-role": {"S04InvokeReaderLambda"},
    "udemy-agentcore-s04-cw-reader-role": {"S04CloudWatchRead"},
    "udemy-agentcore-s07-runtime": {"S07RuntimeAccess"},
    "udemy-agentcore-s08-runtime": {"S08RuntimeExecution"},
    "udemy-agentcore-s09-runtime": {"S09RuntimeExecution"},
}


class PlanError(ValueError):
    """The inventory is incomplete, ambiguous, or outside the course allowlist."""


def _unique_strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise PlanError(f"{label} must be a list of non-empty strings")
    if len(set(value)) != len(value):
        raise PlanError(f"{label} contains duplicates")
    return value


def validate_plan(plan: dict[str, Any], expected_account: str, region: str) -> None:
    """Validate exact owner-supplied identities before any AWS client is used."""
    if not re.fullmatch(r"\d{12}", expected_account):
        raise PlanError("expected account must be the 12-digit training account ID")
    if region != REGION or plan.get("region") != REGION:
        raise PlanError(f"this lesson is limited to {REGION}")
    if plan.get("account_id") != expected_account:
        raise PlanError("plan account_id must exactly match --expected-account")

    runtimes = plan.get("runtimes", [])
    if not isinstance(runtimes, list):
        raise PlanError("runtimes must be a list")
    seen_runtime_ids: set[str] = set()
    for runtime in runtimes:
        rid = runtime.get("id", "")
        if not isinstance(rid, str) or not any(rid.startswith(f"udemy_agentcore_{s}-") for s in RUNTIME_SECTIONS):
            raise PlanError(f"runtime is outside the S03/S07/S08/S09 allowlist: {rid!r}")
        if rid in seen_runtime_ids:
            raise PlanError(f"duplicate runtime: {rid}")
        seen_runtime_ids.add(rid)
        endpoints = runtime.get("endpoints")
        if not endpoints or not isinstance(endpoints, list):
            raise PlanError(f"runtime {rid} must list every endpoint to be removed")
        names = [ep.get("name") for ep in endpoints if isinstance(ep, dict)]
        if len(names) != len(endpoints) or not all(isinstance(name, str) and name for name in names):
            raise PlanError(f"runtime {rid} endpoints need exact names")
        if any(not (isinstance(ep.get("live_version"), int) or (isinstance(ep.get("live_version"), str) and ep["live_version"].isdigit())) or not isinstance(ep.get("status"), str) for ep in endpoints):
            raise PlanError(f"runtime {rid} endpoints need exact live_version and status")
        if len(set(names)) != len(names):
            raise PlanError(f"runtime {rid} has duplicate endpoint names")

    gateways = plan.get("gateways", [])
    if not isinstance(gateways, list):
        raise PlanError("gateways must be a list")
    for gateway in gateways:
        if not str(gateway.get("id", "")).startswith("udemy-agentcore-s04-gateway-"):
            raise PlanError("only an S04 course Gateway is allowed")
        targets = gateway.get("targets")
        if not isinstance(targets, list) or not targets:
            raise PlanError("each Gateway must list its exact targets")
        for target in targets:
            if not isinstance(target, dict) or not str(target.get("id", "")) or not str(target.get("name", "")).startswith("udemy-agentcore-s04-cw-target") or target.get("target_type") != "LAMBDA" or not isinstance(target.get("lambda_arn"), str):
                raise PlanError("Gateway target is outside the S04 allowlist")
            expected_lambda_arn = f"arn:aws:lambda:{REGION}:{expected_account}:function:udemy-agentcore-s04-cw-reader"
            if target["lambda_arn"] != expected_lambda_arn:
                raise PlanError("Gateway target Lambda is outside the exact S04 allowlist")

    lambdas = _unique_strings(plan.get("lambda_functions", []), "lambda_functions")
    if any(name != "udemy-agentcore-s04-cw-reader" for name in lambdas):
        raise PlanError("only the S04 synthetic CloudWatch reader Lambda is allowed")

    buckets = _unique_strings(plan.get("buckets", []), "buckets")
    for name in buckets:
        s03_shared_bucket = bool(re.fullmatch(r"udemy-agentcore-s03-apne1-[0-9a-f]{32}", name))
        section_bucket = any(name.startswith(f"udemy-agentcore-{s}-apne1-{expected_account}") for s in ("s07", "s08", "s09"))
        if not (s03_shared_bucket or section_bucket):
            raise PlanError(f"bucket is outside the course artifact allowlist: {name}")
    bucket_objects = plan.get("bucket_objects")
    if not isinstance(bucket_objects, dict) or set(bucket_objects) != set(buckets):
        raise PlanError("bucket_objects must give an exact object-version inventory for every bucket")
    for bucket, objects in bucket_objects.items():
        if not isinstance(objects, list):
            raise PlanError(f"bucket_objects[{bucket}] must be a list")
        seen: set[tuple[str, str, str]] = set()
        for item in objects:
            if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k] for k in ("key", "version_id", "kind")) or not isinstance(item.get("size"), int) or item["size"] < 0 or not isinstance(item.get("etag"), str):
                raise PlanError(f"bucket_objects[{bucket}] entries need exact key, version_id, kind, size, and etag")
            if item["kind"] not in {"version", "delete-marker"}:
                raise PlanError(f"invalid S3 object inventory kind in {bucket}")
            if item["kind"] == "delete-marker" and (item["size"] != 0 or item["etag"] != ""):
                raise PlanError(f"delete markers have no content size or ETag: {item['key']}")
            if not _allowed_course_object(bucket, item["key"], item["kind"], item["size"]):
                raise PlanError(f"object is outside the course artifact allowlist: {item['key']}")
            identity = (item["key"], item["version_id"], item["kind"])
            if identity in seen:
                raise PlanError(f"duplicate object version in {bucket}: {identity}")
            seen.add(identity)

    logs = _unique_strings(plan.get("log_groups", []), "log_groups")
    runtime_names = {f"/aws/bedrock-agentcore/runtimes/{r['id']}-{ep['name']}" for r in runtimes for ep in r["endpoints"]}
    synthetic_prefix = "/udemy/agentcore/s04/synthetic/"
    if any(name not in runtime_names and not (name.startswith(synthetic_prefix) and name.count("/") == 5) for name in logs):
        raise PlanError("log group is outside an exact course Runtime log or S04 synthetic-log path")

    roles = _unique_strings(plan.get("roles", []), "roles")
    if any(role not in ROLE_POLICIES for role in roles):
        raise PlanError("IAM role is outside the course role allowlist")
    required_roles = {r.get("role_name") for r in runtimes} | {g.get("role_name") for g in gateways}
    required_roles |= {plan.get("lambda_role_name")} if lambdas else set()
    if not required_roles.issubset(set(roles)):
        raise PlanError("all Runtime, Gateway, and Lambda roles must be listed for final role cleanup")
    trust = plan.get("role_trust_policies")
    if not isinstance(trust, dict) or set(trust) != set(roles) or any(not isinstance(doc, dict) for doc in trust.values()):
        raise PlanError("role_trust_policies must contain the approved trust-policy snapshot for every role")


def deletion_steps(plan: dict[str, Any]) -> list[str]:
    """Readable preview in dependency order; IAM roles are always last."""
    steps: list[str] = []
    for runtime in plan.get("runtimes", []):
        for endpoint in runtime["endpoints"]:
            steps.append(f"delete endpoint {runtime['id']} / {endpoint['name']}")
    for runtime in plan.get("runtimes", []):
        steps.append(f"delete Runtime {runtime['id']} (all versions)")
    for gateway in plan.get("gateways", []):
        for target in gateway["targets"]:
            steps.append(f"delete Gateway target {gateway['id']} / {target['id']}")
        steps.append(f"delete Gateway {gateway['id']}")
    for name in plan.get("lambda_functions", []):
        steps.append(f"delete Lambda {name}")
    for bucket in plan.get("buckets", []):
        steps.append(f"delete all object versions and delete markers, then bucket {bucket}")
    for name in plan.get("log_groups", []):
        steps.append(f"delete log group {name} (after evidence capture)")
    for role in plan.get("roles", []):
        steps.append(f"delete inline policies then role {role}")
    return steps


def _pages(client: Any, operation: str, result_key: str, **kwargs: Any) -> Iterable[dict[str, Any]]:
    token = None
    while True:
        request = dict(kwargs)
        if token:
            request["nextToken"] = token
        response = getattr(client, operation)(**request)
        yield from response.get(result_key, [])
        token = response.get("nextToken")
        if not token:
            break


def _is_not_found(error: Exception) -> bool:
    response = getattr(error, "response", {})
    code = response.get("Error", {}).get("Code", "")
    return code in {"ResourceNotFoundException", "ResourceNotFound", "NoSuchEntity", "NoSuchBucket", "NotFound", "404"}


def _wait_absent(check_present: Callable[[], bool], label: str, timeout: int = 900, interval: int = 5) -> None:
    deadline = time.monotonic() + timeout
    while check_present():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out waiting for absence: {label}")
        time.sleep(interval)


def _course_prefixes(bucket: str) -> tuple[str, ...]:
    section = next((s for s in ("s03", "s07", "s08", "s09") if f"-{s}-" in bucket), None)
    if section == "s03":
        return ("s03/", "s04/")
    if section == "s07":
        return ("s07/",)
    return (f"{section}/",) if section else ()


def _allowed_course_object(bucket: str, key: str, kind: str, size: int) -> bool:
    if any(key.startswith(prefix) for prefix in _course_prefixes(bucket)):
        return True
    section = next((s for s in ("s03", "s07", "s08", "s09") if f"-{s}-" in bucket), None)
    # Some S07 package builds leave zero-byte hash-named directory objects at
    # bucket root. Permit only that structural form; exact snapshot still gates.
    return section == "s07" and kind == "version" and size == 0 and bool(re.fullmatch(r"[0-9a-f]{64}/", key))


def _live_bucket_inventory(s3: Any, bucket: str, expected_owner: str) -> tuple[list[dict[str, str]], set[tuple[str, str, str, int, str]]]:
    paginator = s3.get_paginator("list_object_versions")
    versions: list[dict[str, str]] = []
    actual: set[tuple[str, str, str, int, str]] = set()
    for page in paginator.paginate(Bucket=bucket, ExpectedBucketOwner=expected_owner):
        for item in page.get("Versions", []):
            key = item["Key"]
            if not _allowed_course_object(bucket, key, "version", item["Size"]):
                raise PlanError(f"unexpected object key in course bucket {bucket}: {key}")
            etag = item.get("ETag", "")
            versions.append({"Key": key, "VersionId": item["VersionId"]})
            actual.add((key, item["VersionId"], "version", item["Size"], etag))
        for item in page.get("DeleteMarkers", []):
            key = item["Key"]
            if not _allowed_course_object(bucket, key, "delete-marker", 0):
                raise PlanError(f"unexpected delete-marker key in course bucket {bucket}: {key}")
            versions.append({"Key": key, "VersionId": item["VersionId"]})
            actual.add((key, item["VersionId"], "delete-marker", 0, ""))
    return versions, actual


def _expected_bucket_inventory(expected_objects: list[dict[str, Any]]) -> set[tuple[str, str, str, int, str]]:
    return {(item["key"], item["version_id"], item["kind"], item["size"], item["etag"]) for item in expected_objects}


def _delete_bucket_objects(s3: Any, bucket: str, expected_objects: list[dict[str, str]], expected_owner: str) -> None:
    versions, actual = _live_bucket_inventory(s3, bucket, expected_owner)
    expected = _expected_bucket_inventory(expected_objects)
    if actual != expected:
        raise PlanError(f"object-version inventory changed for {bucket}: expected {len(expected)}, found {len(actual)}")
    for offset in range(0, len(versions), 1000):
        objects = [{"Key": item["Key"], "VersionId": item["VersionId"]} for item in versions[offset:offset + 1000]]
        s3.delete_objects(Bucket=bucket, ExpectedBucketOwner=expected_owner, Delete={"Objects": objects, "Quiet": True})
    paginator = s3.get_paginator("list_object_versions")
    remaining = list(paginator.paginate(Bucket=bucket, ExpectedBucketOwner=expected_owner))
    if any(page.get("Versions") or page.get("DeleteMarkers") for page in remaining):
        raise RuntimeError(f"S3 versions or delete markers remain in {bucket}; bucket was not deleted")
    s3.delete_bucket(Bucket=bucket, ExpectedBucketOwner=expected_owner)
    try:
        s3.head_bucket(Bucket=bucket, ExpectedBucketOwner=expected_owner)
    except Exception as error:
        if not _is_not_found(error):
            raise
    else:
        raise RuntimeError(f"bucket still exists after deletion: {bucket}")


def _preflight(plan: dict[str, Any], session: Any, expected_account: str) -> None:
    """Read-only whole-plan validation. Must finish before the first mutation."""
    control = session.client("bedrock-agentcore-control", region_name=REGION)
    for runtime in plan.get("runtimes", []):
        actual = list(_pages(control, "list_agent_runtime_endpoints", "runtimeEndpoints", agentRuntimeId=runtime["id"]))
        expected_endpoints = {(ep["name"], str(ep["live_version"]), ep["status"]) for ep in runtime["endpoints"]}
        actual_endpoints = {(ep["name"], str(ep["liveVersion"]), ep["status"]) for ep in actual}
        if actual_endpoints != expected_endpoints:
            raise PlanError(f"endpoint inventory changed for {runtime['id']}")
        details = control.get_agent_runtime(agentRuntimeId=runtime["id"])
        role = session.client("iam").get_role(RoleName=runtime["role_name"])["Role"]
        if details.get("roleArn") != role["Arn"]:
            raise PlanError(f"Runtime role binding changed for {runtime['id']}")

    for gateway in plan.get("gateways", []):
        targets = list(_pages(control, "list_gateway_targets", "items", gatewayIdentifier=gateway["id"]))
        expected_targets = {(target["id"], target["name"], target["target_type"]) for target in gateway["targets"]}
        actual_targets = {(target["targetId"], target["name"], target["targetType"]) for target in targets}
        if actual_targets != expected_targets:
            raise PlanError(f"Gateway target inventory changed for {gateway['id']}")
        for target in gateway["targets"]:
            detail = control.get_gateway_target(gatewayIdentifier=gateway["id"], targetId=target["id"])
            lambda_arn = detail.get("targetConfiguration", {}).get("mcp", {}).get("lambda", {}).get("lambdaArn")
            if detail.get("name") != target["name"] or lambda_arn != target["lambda_arn"]:
                raise PlanError(f"Gateway target Lambda binding changed for {gateway['id']}/{target['id']}")
        details = control.get_gateway(gatewayIdentifier=gateway["id"])
        role = session.client("iam").get_role(RoleName=gateway["role_name"])["Role"]
        if details.get("roleArn") != role["Arn"]:
            raise PlanError(f"Gateway role binding changed for {gateway['id']}")

    lambda_client = session.client("lambda", region_name=REGION)
    iam = session.client("iam")
    if plan.get("lambda_functions"):
        lambda_role = iam.get_role(RoleName=plan["lambda_role_name"])["Role"]
        for function in plan["lambda_functions"]:
            details = lambda_client.get_function_configuration(FunctionName=function)
            expected_arn = f"arn:aws:lambda:{REGION}:{expected_account}:function:{function}"
            if details.get("FunctionArn") != expected_arn or details.get("Role") != lambda_role["Arn"]:
                raise PlanError(f"Lambda execution-role binding changed for {function}")

    s3 = session.client("s3", region_name=REGION)
    for bucket in plan.get("buckets", []):
        _, actual = _live_bucket_inventory(s3, bucket, expected_account)
        if actual != _expected_bucket_inventory(plan["bucket_objects"][bucket]):
            raise PlanError(f"object-version inventory changed for {bucket}")

    logs = session.client("logs", region_name=REGION)
    log_names = set(plan.get("log_groups", []))
    found_logs: set[str] = set()
    paginator = logs.get_paginator("describe_log_groups")
    for page in paginator.paginate():
        found_logs.update(group["logGroupName"] for group in page.get("logGroups", []) if group["logGroupName"] in log_names)
    if found_logs != log_names:
        raise PlanError(f"log group inventory changed: expected {sorted(log_names)}, found {sorted(found_logs)}")

    for role_name in plan.get("roles", []):
        role = iam.get_role(RoleName=role_name)["Role"]
        if role.get("AssumeRolePolicyDocument") != plan["role_trust_policies"][role_name]:
            raise PlanError(f"role trust policy changed for {role_name}")
        if role.get("PermissionsBoundary"):
            raise PlanError(f"role {role_name} has a permissions boundary")
        if iam.list_attached_role_policies(RoleName=role_name).get("AttachedPolicies"):
            raise PlanError(f"role {role_name} has attached managed policies")
        if iam.list_instance_profiles_for_role(RoleName=role_name).get("InstanceProfiles"):
            raise PlanError(f"role {role_name} is attached to an instance profile")
        inline = set(iam.list_role_policies(RoleName=role_name).get("PolicyNames", []))
        if inline != ROLE_POLICIES[role_name]:
            raise PlanError(f"role {role_name} inline policies changed: {sorted(inline)}")


def execute(plan: dict[str, Any], session: Any, expected_account: str, region: str, *, wait_timeout: int = 900, wait_interval: int = 5) -> None:
    validate_plan(plan, expected_account, region)
    actual_account = session.client("sts").get_caller_identity()["Account"]
    if actual_account != expected_account:
        raise PlanError(f"caller account {actual_account} does not match --expected-account")

    _preflight(plan, session, expected_account)
    control = session.client("bedrock-agentcore-control", region_name=REGION)
    # Endpoints detach before their parent runtimes. Re-list until absent.
    for runtime in plan.get("runtimes", []):
        deleted_endpoints: set[str] = set()
        for endpoint in runtime["endpoints"]:
            current = list(_pages(control, "list_agent_runtime_endpoints", "runtimeEndpoints", agentRuntimeId=runtime["id"]))
            expected_remaining = {
                (ep["name"], str(ep["live_version"]), ep["status"])
                for ep in runtime["endpoints"] if ep["name"] not in deleted_endpoints
            }
            actual_remaining = {(ep["name"], str(ep["liveVersion"]), ep["status"]) for ep in current}
            if actual_remaining != expected_remaining:
                raise PlanError(f"endpoint inventory drifted before delete for {runtime['id']}")
            control.delete_agent_runtime_endpoint(agentRuntimeId=runtime["id"], endpointName=endpoint["name"])
            _wait_absent(lambda r=runtime, e=endpoint: any(x["name"] == e["name"] for x in _pages(control, "list_agent_runtime_endpoints", "runtimeEndpoints", agentRuntimeId=r["id"])), f"endpoint {runtime['id']}/{endpoint['name']}", wait_timeout, wait_interval)
            deleted_endpoints.add(endpoint["name"])
    for runtime in plan.get("runtimes", []):
        details = control.get_agent_runtime(agentRuntimeId=runtime["id"])
        role = session.client("iam").get_role(RoleName=runtime["role_name"])["Role"]
        if details.get("roleArn") != role["Arn"]:
            raise PlanError(f"Runtime role binding changed before delete for {runtime['id']}")
        control.delete_agent_runtime(agentRuntimeId=runtime["id"])
        def runtime_present(rid: str = runtime["id"]) -> bool:
            try:
                control.get_agent_runtime(agentRuntimeId=rid)
                return True
            except Exception as error:
                if _is_not_found(error):
                    return False
                raise
        _wait_absent(runtime_present, f"Runtime {runtime['id']}", wait_timeout, wait_interval)

    for gateway in plan.get("gateways", []):
        actual = list(_pages(control, "list_gateway_targets", "items", gatewayIdentifier=gateway["id"]))
        expected = {target["id"] for target in gateway["targets"]}
        if {target["targetId"] for target in actual} != expected:
            raise PlanError(f"Gateway target inventory changed for {gateway['id']}")
        deleted_targets: set[str] = set()
        for target in gateway["targets"]:
            current = list(_pages(control, "list_gateway_targets", "items", gatewayIdentifier=gateway["id"]))
            expected_remaining = {(t["id"], t["name"], t["target_type"]) for t in gateway["targets"] if t["id"] not in deleted_targets}
            actual_remaining = {(t["targetId"], t["name"], t["targetType"]) for t in current}
            if actual_remaining != expected_remaining:
                raise PlanError(f"Gateway target inventory drifted before delete for {gateway['id']}")
            detail = control.get_gateway_target(gatewayIdentifier=gateway["id"], targetId=target["id"])
            lambda_arn = detail.get("targetConfiguration", {}).get("mcp", {}).get("lambda", {}).get("lambdaArn")
            if detail.get("name") != target["name"] or lambda_arn != target["lambda_arn"]:
                raise PlanError(f"Gateway target Lambda binding changed before delete for {gateway['id']}/{target['id']}")
            control.delete_gateway_target(gatewayIdentifier=gateway["id"], targetId=target["id"])
            _wait_absent(lambda g=gateway, t=target: any(x["targetId"] == t["id"] for x in _pages(control, "list_gateway_targets", "items", gatewayIdentifier=g["id"])), f"Gateway target {gateway['id']}/{target['id']}", wait_timeout, wait_interval)
            deleted_targets.add(target["id"])
        details = control.get_gateway(gatewayIdentifier=gateway["id"])
        role = session.client("iam").get_role(RoleName=gateway["role_name"])["Role"]
        if details.get("roleArn") != role["Arn"]:
            raise PlanError(f"Gateway role binding changed before delete for {gateway['id']}")
        control.delete_gateway(gatewayIdentifier=gateway["id"])
        def gateway_present(gid: str = gateway["id"]) -> bool:
            try:
                control.get_gateway(gatewayIdentifier=gid)
                return True
            except Exception as error:
                if _is_not_found(error):
                    return False
                raise
        _wait_absent(gateway_present, f"Gateway {gateway['id']}", wait_timeout, wait_interval)

    lambdas = session.client("lambda", region_name=REGION)
    for name in plan.get("lambda_functions", []):
        role = session.client("iam").get_role(RoleName=plan["lambda_role_name"])["Role"]
        details = lambdas.get_function_configuration(FunctionName=name)
        expected_arn = f"arn:aws:lambda:{REGION}:{expected_account}:function:{name}"
        if details.get("FunctionArn") != expected_arn or details.get("Role") != role["Arn"]:
            raise PlanError(f"Lambda execution-role binding changed before delete for {name}")
        lambdas.delete_function(FunctionName=name)
        def lambda_present(function_name: str = name) -> bool:
            try:
                lambdas.get_function(FunctionName=function_name)
                return True
            except Exception as error:
                if _is_not_found(error):
                    return False
                raise
        _wait_absent(lambda_present, f"Lambda {name}", wait_timeout, wait_interval)

    s3 = session.client("s3", region_name=REGION)
    for bucket in plan.get("buckets", []):
        _delete_bucket_objects(s3, bucket, plan["bucket_objects"][bucket], expected_account)

    logs = session.client("logs", region_name=REGION)
    for name in plan.get("log_groups", []):
        logs.delete_log_group(logGroupName=name)
        def log_group_present(group: str = name) -> bool:
            return any(item["logGroupName"] == group for item in _pages(logs, "describe_log_groups", "logGroups", logGroupNamePrefix=group))
        _wait_absent(log_group_present, f"log group {name}", wait_timeout, wait_interval)

    # IAM is last. Stop if an attached managed policy, boundary, or instance
    # profile indicates that the role has use beyond this course plan.
    iam = session.client("iam")
    for role_name in plan.get("roles", []):
        role = iam.get_role(RoleName=role_name)["Role"]
        if role.get("AssumeRolePolicyDocument") != plan["role_trust_policies"][role_name]:
            raise PlanError(f"role trust policy changed before delete for {role_name}")
        if role.get("PermissionsBoundary"):
            raise PlanError(f"role {role_name} has a permissions boundary")
        if iam.list_attached_role_policies(RoleName=role_name).get("AttachedPolicies"):
            raise PlanError(f"role {role_name} has attached managed policies")
        if iam.list_instance_profiles_for_role(RoleName=role_name).get("InstanceProfiles"):
            raise PlanError(f"role {role_name} is attached to an instance profile")
        inline = set(iam.list_role_policies(RoleName=role_name).get("PolicyNames", []))
        if inline != ROLE_POLICIES[role_name]:
            raise PlanError(f"role {role_name} inline policies changed: {sorted(inline)}")
        for policy_name in sorted(inline):
            iam.delete_role_policy(RoleName=role_name, PolicyName=policy_name)
        iam.delete_role(RoleName=role_name)
        def role_present(name: str = role_name) -> bool:
            try:
                iam.get_role(RoleName=name)
                return True
            except Exception as error:
                if _is_not_found(error):
                    return False
                raise
        _wait_absent(role_present, f"IAM role {role_name}", wait_timeout, wait_interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path, help="local JSON inventory reviewed before cleanup")
    parser.add_argument("--expected-account", required=True, help="12-digit training account ID confirmed from STS")
    parser.add_argument("--region", required=True, help="must be ap-northeast-1")
    parser.add_argument("--execute", action="store_true", help="perform the displayed deletions; default is dry-run")
    args = parser.parse_args(argv)
    try:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        validate_plan(plan, args.expected_account, args.region)
        print(json.dumps({"account_id": args.expected_account, "region": args.region, "mode": "execute" if args.execute else "dry-run", "steps": deletion_steps(plan)}, indent=2))
        if not args.execute:
            return 0
        import boto3

        session = boto3.Session(region_name=REGION)
        execute(plan, session, args.expected_account, args.region)
    except (OSError, json.JSONDecodeError, PlanError, TimeoutError) as error:
        print(f"Stopped safely: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
