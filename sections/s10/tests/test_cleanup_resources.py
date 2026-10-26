from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "cleanup_resources.py"
SPEC = importlib.util.spec_from_file_location("cleanup_resources", SCRIPT)
cleanup = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(cleanup)


def valid_plan():
    return {
        "account_id": "123456789012",
        "region": "ap-northeast-1",
        "runtimes": [{"id": "udemy_agentcore_s08-example", "endpoints": [{"name": "DEFAULT", "live_version": "2", "status": "READY"}], "role_name": "udemy-agentcore-s08-runtime"}],
        "gateways": [{"id": "udemy-agentcore-s04-gateway-example", "targets": [{"id": "target-id", "name": "udemy-agentcore-s04-cw-target", "target_type": "LAMBDA", "lambda_arn": "arn:aws:lambda:ap-northeast-1:123456789012:function:udemy-agentcore-s04-cw-reader"}], "role_name": "udemy-agentcore-s04-gateway-role"}],
        "lambda_functions": ["udemy-agentcore-s04-cw-reader"],
        "lambda_role_name": "udemy-agentcore-s04-cw-reader-role",
        "buckets": ["udemy-agentcore-s08-apne1-123456789012-ab12cd"],
        "bucket_objects": {"udemy-agentcore-s08-apne1-123456789012-ab12cd": [{"key": "s08/a.zip", "version_id": "v1", "kind": "version", "size": 1, "etag": '"test"'}]},
        "log_groups": [
            "/aws/bedrock-agentcore/runtimes/udemy_agentcore_s08-example-DEFAULT",
            "/udemy/agentcore/s04/synthetic/catalog-api",
        ],
        "roles": ["udemy-agentcore-s08-runtime", "udemy-agentcore-s04-gateway-role", "udemy-agentcore-s04-cw-reader-role"],
        "role_trust_policies": {
            "udemy-agentcore-s08-runtime": {"Version": "2012-10-17", "Statement": []},
            "udemy-agentcore-s04-gateway-role": {"Version": "2012-10-17", "Statement": []},
            "udemy-agentcore-s04-cw-reader-role": {"Version": "2012-10-17", "Statement": []},
        },
    }


def test_plan_requires_account_region_and_course_allowlist():
    plan = valid_plan()
    cleanup.validate_plan(plan, "123456789012", "ap-northeast-1")

    plan = valid_plan()
    plan["gateways"][0]["targets"][0]["lambda_arn"] = "arn:aws:lambda:ap-northeast-1:123456789012:function:other-function"
    try:
        cleanup.validate_plan(plan, "123456789012", "ap-northeast-1")
    except cleanup.PlanError:
        pass
    else:
        raise AssertionError("Gateway target must name the exact S04 Lambda ARN")

    for expected_account, region in (("999999999999", "ap-northeast-1"), ("123456789012", "us-east-1")):
        try:
            cleanup.validate_plan(plan, expected_account, region)
        except cleanup.PlanError:
            pass
        else:
            raise AssertionError("mismatched account or region must stop cleanup")

    plan["buckets"] = ["bedrock-agentcore-runtime-123456789012"]
    try:
        cleanup.validate_plan(plan, "123456789012", "ap-northeast-1")
    except cleanup.PlanError:
        pass
    else:
        raise AssertionError("managed runtime storage must never enter the course allowlist")

    plan = valid_plan()
    plan["buckets"] = ["udemy-agentcore-s03-apne1-3311c1bd2676f03497687dfe871046af"]
    plan["bucket_objects"] = {plan["buckets"][0]: []}
    cleanup.validate_plan(plan, "123456789012", "ap-northeast-1")

    plan = valid_plan()
    plan["buckets"] = ["udemy-agentcore-s03-apne1-3311c1bd2676f03497687dfe871046af"]
    plan["bucket_objects"] = {plan["buckets"][0]: []}
    cleanup.validate_plan(plan, "123456789012", "ap-northeast-1")


def test_preview_places_dependencies_before_roles():
    plan = valid_plan()
    plan["runtimes"][0]["endpoints"].append({"name": "staging", "live_version": "2", "status": "READY"})
    steps = cleanup.deletion_steps(plan)
    assert steps[0] == "delete endpoint udemy_agentcore_s08-example / staging"
    assert steps[1] == "delete Runtime udemy_agentcore_s08-example (all versions; includes DEFAULT endpoint)"
    assert "delete endpoint udemy_agentcore_s08-example / DEFAULT" not in steps
    assert steps[2].startswith("delete Gateway target")
    assert steps[-1].startswith("delete inline policies then role")
    assert all(step.startswith("delete inline policies then role") for step in steps[-3:])


class _NotFound(Exception):
    response = {"Error": {"Code": "ResourceNotFoundException"}}


class _RuntimeControl:
    def __init__(self, endpoints):
        self.endpoints = [dict(endpoint) for endpoint in endpoints]
        self.runtime_present = True
        self.calls = []

    def list_agent_runtime_endpoints(self, **_kwargs):
        self.calls.append(("list-endpoints", [endpoint["name"] for endpoint in self.endpoints]))
        return {"runtimeEndpoints": [
            {"name": endpoint["name"], "liveVersion": str(endpoint["live_version"]), "status": endpoint["status"]}
            for endpoint in self.endpoints
        ]}

    def delete_agent_runtime_endpoint(self, *, endpointName, **_kwargs):
        self.calls.append(("delete-endpoint", endpointName))
        self.endpoints = [endpoint for endpoint in self.endpoints if endpoint["name"] != endpointName]

    def get_agent_runtime(self, **_kwargs):
        self.calls.append(("get-runtime", self.runtime_present))
        if not self.runtime_present:
            raise _NotFound()
        return {"roleArn": "arn:aws:iam::123456789012:role/udemy-agentcore-s09-runtime"}

    def delete_agent_runtime(self, **_kwargs):
        self.calls.append(("delete-runtime", None))
        self.runtime_present = False
        self.endpoints = []


class _RuntimeIam:
    def __init__(self, trust_policy):
        self.trust_policy = trust_policy
        self.role_present = True
        self.calls = []

    def get_role(self, **_kwargs):
        self.calls.append(("get-role", self.role_present))
        if not self.role_present:
            raise _NotFound()
        return {"Role": {
            "Arn": "arn:aws:iam::123456789012:role/udemy-agentcore-s09-runtime",
            "AssumeRolePolicyDocument": self.trust_policy,
        }}

    def list_attached_role_policies(self, **_kwargs):
        return {"AttachedPolicies": []}

    def list_instance_profiles_for_role(self, **_kwargs):
        return {"InstanceProfiles": []}

    def list_role_policies(self, **_kwargs):
        return {"PolicyNames": ["S09RuntimeExecution"]}

    def delete_role_policy(self, **kwargs):
        self.calls.append(("delete-role-policy", kwargs["PolicyName"]))

    def delete_role(self, **_kwargs):
        self.calls.append(("delete-role", None))
        self.role_present = False


class _RuntimeSession:
    def __init__(self, control, iam):
        self.control = control
        self.iam = iam

    def client(self, name, **_kwargs):
        if name == "sts":
            return self
        if name == "bedrock-agentcore-control":
            return self.control
        if name == "iam":
            return self.iam
        return object()

    def get_caller_identity(self):
        return {"Account": "123456789012"}


def runtime_only_plan(endpoints):
    trust = {"Version": "2012-10-17", "Statement": []}
    return {
        "account_id": "123456789012",
        "region": "ap-northeast-1",
        "runtimes": [{
            "id": "udemy_agentcore_s09-example",
            "endpoints": endpoints,
            "role_name": "udemy-agentcore-s09-runtime",
        }],
        "gateways": [],
        "lambda_functions": [],
        "buckets": [],
        "bucket_objects": {},
        "log_groups": [],
        "roles": ["udemy-agentcore-s09-runtime"],
        "role_trust_policies": {"udemy-agentcore-s09-runtime": trust},
    }


def test_execute_removes_default_with_runtime_after_named_endpoints(monkeypatch):
    endpoints = [
        {"name": "DEFAULT", "live_version": "4", "status": "READY"},
        {"name": "staging", "live_version": "4", "status": "READY"},
    ]
    control = _RuntimeControl(endpoints)
    iam = _RuntimeIam({"Version": "2012-10-17", "Statement": []})
    monkeypatch.setattr(cleanup, "_preflight", lambda *_args: None)

    cleanup.execute(runtime_only_plan(endpoints), _RuntimeSession(control, iam), "123456789012", "ap-northeast-1", wait_interval=0)

    mutations = [call for call in control.calls if call[0].startswith("delete-")]
    assert mutations == [("delete-endpoint", "staging"), ("delete-runtime", None)]
    assert control.calls.index(("delete-endpoint", "staging")) < control.calls.index(("delete-runtime", None))
    assert control.calls.index(("delete-runtime", None)) < control.calls.index(("get-runtime", False))
    assert not any(call == ("delete-endpoint", "DEFAULT") for call in control.calls)


def test_execute_deletes_default_only_through_runtime_operation(monkeypatch):
    endpoints = [{"name": "DEFAULT", "live_version": "4", "status": "READY"}]
    control = _RuntimeControl(endpoints)
    iam = _RuntimeIam({"Version": "2012-10-17", "Statement": []})
    monkeypatch.setattr(cleanup, "_preflight", lambda *_args: None)

    cleanup.execute(runtime_only_plan(endpoints), _RuntimeSession(control, iam), "123456789012", "ap-northeast-1", wait_interval=0)

    assert not any(call[0] == "delete-endpoint" for call in control.calls)
    assert ("delete-runtime", None) in control.calls
    assert ("get-runtime", False) in control.calls


class _Paginator:
    def __init__(self, pages):
        self.pages = pages
        self.calls = 0

    def paginate(self, **_kwargs):
        self.calls += 1
        return self.pages if self.calls == 1 else []


class _S3:
    def __init__(self, pages):
        self.paginator = _Paginator(pages)
        self.deleted = []
        self.bucket_deleted = False

    def get_paginator(self, operation):
        assert operation == "list_object_versions"
        return self.paginator

    def delete_objects(self, **kwargs):
        self.deleted.extend(kwargs["Delete"]["Objects"])

    def delete_bucket(self, **kwargs):
        self.bucket_deleted = True

    def head_bucket(self, **kwargs):
        error = RuntimeError("not found")
        error.response = {"Error": {"Code": "404"}}
        raise error


def test_s3_cleanup_paginates_versions_and_delete_markers_before_bucket():
    client = _S3([
        {"Versions": [{"Key": "s08/a.zip", "VersionId": "old-v", "Size": 5, "ETag": '"object"'}]},
        {"DeleteMarkers": [{"Key": "s08/old.zip", "VersionId": "marker-v"}]},
    ])
    cleanup._delete_bucket_objects(client, "udemy-agentcore-s08-apne1-123456789012-ab12cd", [
        {"key": "s08/a.zip", "version_id": "old-v", "kind": "version", "size": 5, "etag": '"object"'},
        {"key": "s08/old.zip", "version_id": "marker-v", "kind": "delete-marker", "size": 0, "etag": ""},
    ], "123456789012")
    assert client.deleted == [
        {"Key": "s08/a.zip", "VersionId": "old-v"},
        {"Key": "s08/old.zip", "VersionId": "marker-v"},
    ]
    assert client.bucket_deleted


def test_s3_cleanup_stops_on_unexpected_key_and_never_deletes_bucket():
    client = _S3([{"Versions": [{"Key": "other-course/data.zip", "VersionId": "v1", "Size": 1, "ETag": '"other"'}]}])
    try:
        cleanup._delete_bucket_objects(client, "udemy-agentcore-s08-apne1-123456789012-ab12cd", [], "123456789012")
    except cleanup.PlanError:
        pass
    else:
        raise AssertionError("unexpected object must stop cleanup")
    assert client.deleted == []
    assert not client.bucket_deleted


def test_s3_cleanup_stops_on_added_key_or_version_before_any_delete():
    client = _S3([{"Versions": [{"Key": "s08/foo/new.zip", "VersionId": "new-v", "Size": 2, "ETag": '"new"'}]}])
    try:
        cleanup._delete_bucket_objects(client, "udemy-agentcore-s08-apne1-123456789012-ab12cd", [], "123456789012")
    except cleanup.PlanError:
        pass
    else:
        raise AssertionError("new object version must stop cleanup")
    assert client.deleted == []
    assert not client.bucket_deleted


def test_s07_generated_root_prefix_markers_are_allowed_but_exact_snapshot_is_required():
    bucket = "udemy-agentcore-s07-apne1-123456789012"
    known_marker = {"Key": "b3dd3a9a433e1f88fd7799dfecb89633fdd1aa18007e20beb425b02e6a3d97fd/", "VersionId": "marker-v", "Size": 0, "ETag": '"empty"'}
    client = _S3([{"Versions": [known_marker]}])
    try:
        cleanup._delete_bucket_objects(client, bucket, [], "123456789012")
    except cleanup.PlanError:
        pass
    else:
        raise AssertionError("known S07 folder marker is allowed by prefix but must still be in the exact snapshot")
    assert client.deleted == []
    assert not client.bucket_deleted

    client = _S3([{"Versions": [{"Key": "other-course/", "VersionId": "bad", "Size": 0, "ETag": '"empty"'}]}])
    try:
        cleanup._delete_bucket_objects(client, bucket, [], "123456789012")
    except cleanup.PlanError:
        pass
    else:
        raise AssertionError("unrelated S07-bucket key must stop cleanup")
    assert client.deleted == []
    assert not client.bucket_deleted


def test_s3_cleanup_detects_marker_drift_on_later_page():
    client = _S3([
        {"Versions": [{"Key": "s08/a.zip", "VersionId": "v1", "Size": 1, "ETag": '"one"'}]},
        {"DeleteMarkers": [{"Key": "s08/a.zip", "VersionId": "unexpected-marker"}]},
    ])
    try:
        cleanup._delete_bucket_objects(client, "udemy-agentcore-s08-apne1-123456789012-ab12cd", [
            {"key": "s08/a.zip", "version_id": "v1", "kind": "version", "size": 1, "etag": '"one"'},
        ], "123456789012")
    except cleanup.PlanError:
        pass
    else:
        raise AssertionError("unexpected marker on a later page must stop cleanup")
    assert client.deleted == []
    assert not client.bucket_deleted


def test_execute_finishes_whole_plan_preflight_before_any_mutation(monkeypatch):
    calls = []

    class Session:
        def client(self, name, **kwargs):
            calls.append(("client", name))
            if name == "sts":
                return self
            raise AssertionError("preflight rejection must occur before service clients are opened")

        def get_caller_identity(self):
            return {"Account": "123456789012"}

    def reject_preflight(plan, session, expected_account):
        calls.append(("preflight", None))
        raise cleanup.PlanError("inventory drift")

    monkeypatch.setattr(cleanup, "_preflight", reject_preflight)
    try:
        cleanup.execute(valid_plan(), Session(), "123456789012", "ap-northeast-1")
    except cleanup.PlanError:
        pass
    else:
        raise AssertionError("preflight drift must stop execution")
    assert calls == [("client", "sts"), ("preflight", None)]
