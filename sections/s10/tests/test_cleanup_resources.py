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
        "bucket_versioning": {"udemy-agentcore-s08-apne1-123456789012-ab12cd": "Enabled"},
        "bucket_mfa_delete": {"udemy-agentcore-s08-apne1-123456789012-ab12cd": "NotConfigured"},
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
    plan["bucket_versioning"] = {plan["buckets"][0]: "Enabled"}
    plan["bucket_mfa_delete"] = {plan["buckets"][0]: "NotConfigured"}
    cleanup.validate_plan(plan, "123456789012", "ap-northeast-1")

    plan = valid_plan()
    plan["buckets"] = ["udemy-agentcore-s03-apne1-3311c1bd2676f03497687dfe871046af"]
    plan["bucket_objects"] = {plan["buckets"][0]: []}
    plan["bucket_versioning"] = {plan["buckets"][0]: "Enabled"}
    plan["bucket_mfa_delete"] = {plan["buckets"][0]: "NotConfigured"}
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
    def __init__(self, trust_policy, inline_policies=None):
        self.trust_policy = trust_policy
        self.inline_policies = inline_policies or ["S09ReadOnlyInvestigation"]
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
        return {"PolicyNames": self.inline_policies}

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
        if name == "logs":
            return _EmptyLogs()
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
        "bucket_versioning": {},
        "bucket_mfa_delete": {},
        "bucket_objects": {},
        "log_groups": [],
        "roles": ["udemy-agentcore-s09-runtime"],
        "role_trust_policies": {"udemy-agentcore-s09-runtime": trust},
    }


class _EmptyLogs:
    def get_paginator(self, operation):
        assert operation == "describe_log_groups"
        return _Paginator([{"logGroups": []}])


def test_s09_runtime_policy_allowlist_accepts_current_name_and_rejects_stale_or_extra():
    trust = {"Version": "2012-10-17", "Statement": []}
    plan = runtime_only_plan([{"name": "DEFAULT", "live_version": "5", "status": "READY"}])
    control = _RuntimeControl(plan["runtimes"][0]["endpoints"])
    current = _RuntimeIam(trust, ["S09ReadOnlyInvestigation"])
    cleanup._preflight(plan, _RuntimeSession(control, current), "123456789012")

    for inline_policies in (["S09RuntimeExecution"], ["S09ReadOnlyInvestigation", "UnexpectedExtra"]):
        control = _RuntimeControl(plan["runtimes"][0]["endpoints"])
        iam = _RuntimeIam(trust, inline_policies)
        try:
            cleanup._preflight(plan, _RuntimeSession(control, iam), "123456789012")
        except cleanup.PlanError as error:
            assert "inline policies changed" in str(error)
        else:
            raise AssertionError(f"stale or additional inline policies must fail closed: {inline_policies}")


def test_s09_runtime_log_group_requires_exact_planned_runtime_endpoint():
    plan = runtime_only_plan([{"name": "DEFAULT", "live_version": "5", "status": "READY"}])
    plan["runtimes"][0]["id"] = "udemy_agentcore_s09-CNgv51GApE"
    exact = "/aws/bedrock-agentcore/runtimes/udemy_agentcore_s09-CNgv51GApE-DEFAULT"
    plan["log_groups"] = [exact]
    cleanup.validate_plan(plan, "123456789012", "ap-northeast-1")

    plan["log_groups"] = ["/aws/bedrock-agentcore/runtimes/udemy_agentcore_s09-other-DEFAULT"]
    try:
        cleanup.validate_plan(plan, "123456789012", "ap-northeast-1")
    except cleanup.PlanError as error:
        assert "log group is outside" in str(error)
    else:
        raise AssertionError("foreign S09 Runtime log group must fail exact plan-link check")


def tagged_s03_plan(tag="n03a"):
    role = f"udemy-s03-{tag}-runtime"
    bucket = f"udemy-s03-{tag}-apne1-" + "a" * 32
    trust = {"Version": "2012-10-17", "Statement": []}
    return {
        "account_id": "123456789012",
        "region": "ap-northeast-1",
        "runtimes": [{"id": f"udemy_s03_{tag}-QN4TboFC6a", "endpoints": [
            {"name": "DEFAULT", "live_version": "2", "status": "READY"}], "role_name": role}],
        "gateways": [], "lambda_functions": [], "buckets": [bucket],
        "bucket_versioning": {bucket: "Unversioned"},
        "bucket_mfa_delete": {bucket: "NotConfigured"},
        "bucket_objects": {bucket: [
            {"key": f"s03/{tag}/runtime.zip", "version_id": "null", "kind": "version", "size": 10, "etag": '"s03"'},
            {"key": "s04/s03-reader/0123456789/runtime-" + "b" * 64 + ".zip", "version_id": "null", "kind": "version", "size": 20, "etag": '"s04"'},
        ]},
        "log_groups": [], "roles": [role], "role_trust_policies": {role: trust},
    }


def test_tagged_s03_plan_accepts_current_run_tag_and_shared_s04_object_prefix():
    cleanup.validate_plan(tagged_s03_plan(), "123456789012", "ap-northeast-1")
    cleanup.validate_plan(tagged_s03_plan("default1"), "123456789012", "ap-northeast-1")


def test_tagged_s03_plan_rejects_reserved_default_tag():
    plan = tagged_s03_plan("default")
    try:
        cleanup.validate_plan(plan, "123456789012", "ap-northeast-1")
    except cleanup.PlanError as error:
        assert "outside the S03/S07/S08/S09 allowlist" in str(error)
    else:
        raise AssertionError("reserved default tag must retain the fixed S03 namespace")


def test_tagged_s03_plan_rejects_mismatched_runtime_role_or_bucket_tag():
    plan = tagged_s03_plan()
    plan["runtimes"][0]["id"] = "udemy_s03_oct27a-QN4TboFC6a"
    try:
        cleanup.validate_plan(plan, "123456789012", "ap-northeast-1")
    except cleanup.PlanError as error:
        assert "exact run-tag role" in str(error)
    else:
        raise AssertionError("Runtime ID tag must match its exact S03 role")

    plan = tagged_s03_plan()
    plan["buckets"] = ["udemy-s03-n03b-apne1-" + "a" * 32]
    plan["bucket_versioning"] = {plan["buckets"][0]: "Unversioned"}
    plan["bucket_mfa_delete"] = {plan["buckets"][0]: "NotConfigured"}
    plan["bucket_objects"] = {plan["buckets"][0]: []}
    try:
        cleanup.validate_plan(plan, "123456789012", "ap-northeast-1")
    except cleanup.PlanError as error:
        assert "does not match a planned tagged S03 Runtime" in str(error)
    else:
        raise AssertionError("tagged bucket must match its planned Runtime tag")


def test_tagged_s03_plan_rejects_malformed_names_and_unrelated_keys():
    for bad_runtime_id in ("udemy_s03_N03a-QN4TboFC6a", "udemy_s03_toolongtag-QN4TboFC6a", "udemy_s03_n03a-short"):
        plan = tagged_s03_plan()
        plan["runtimes"][0]["id"] = bad_runtime_id
        try:
            cleanup.validate_plan(plan, "123456789012", "ap-northeast-1")
        except cleanup.PlanError:
            pass
        else:
            raise AssertionError(f"malformed tagged S03 Runtime must fail: {bad_runtime_id}")

    plan = tagged_s03_plan()
    plan["buckets"] = ["udemy-s03-n03a-apne1-" + "A" * 32]
    plan["bucket_versioning"] = {plan["buckets"][0]: "Unversioned"}
    plan["bucket_mfa_delete"] = {plan["buckets"][0]: "NotConfigured"}
    plan["bucket_objects"] = {plan["buckets"][0]: []}
    try:
        cleanup.validate_plan(plan, "123456789012", "ap-northeast-1")
    except cleanup.PlanError as error:
        assert "bucket is outside" in str(error)
    else:
        raise AssertionError("malformed tagged S03 bucket suffix must fail")

    plan = tagged_s03_plan()
    plan["bucket_objects"][plan["buckets"][0]].append({
        "key": "s03/other/runtime.zip", "version_id": "null", "kind": "version", "size": 1, "etag": '"other"',
    })
    try:
        cleanup.validate_plan(plan, "123456789012", "ap-northeast-1")
    except cleanup.PlanError as error:
        assert "object is outside" in str(error)
    else:
        raise AssertionError("tagged S03 bucket must reject another run tag's key")


class _TaggedS03Control:
    def list_agent_runtime_endpoints(self, **_kwargs):
        return {"runtimeEndpoints": [{"name": "DEFAULT", "liveVersion": "2", "status": "READY"}]}

    def get_agent_runtime(self, **_kwargs):
        return {"roleArn": "arn:aws:iam::123456789012:role/udemy-s03-n03a-runtime"}


class _TaggedS03Iam:
    def __init__(self, trust, policies):
        self.trust = trust
        self.policies = policies

    def get_role(self, **_kwargs):
        return {"Role": {"Arn": "arn:aws:iam::123456789012:role/udemy-s03-n03a-runtime", "AssumeRolePolicyDocument": self.trust}}

    def list_attached_role_policies(self, **_kwargs):
        return {"AttachedPolicies": []}

    def list_instance_profiles_for_role(self, **_kwargs):
        return {"InstanceProfiles": []}

    def list_role_policies(self, **_kwargs):
        return {"PolicyNames": self.policies}


class _TaggedS03Session:
    def __init__(self, trust, policies):
        self.control = _TaggedS03Control()
        self.iam = _TaggedS03Iam(trust, policies)

    def client(self, name, **_kwargs):
        if name == "bedrock-agentcore-control":
            return self.control
        if name == "iam":
            return self.iam
        if name == "logs":
            return _EmptyLogs()
        return object()


def test_tagged_s03_preflight_requires_only_exact_s03_policy_name():
    plan = tagged_s03_plan()
    plan["buckets"] = []
    plan["bucket_versioning"] = {}
    plan["bucket_mfa_delete"] = {}
    plan["bucket_objects"] = {}
    trust = plan["role_trust_policies"]["udemy-s03-n03a-runtime"]
    cleanup._preflight(plan, _TaggedS03Session(trust, ["S03RuntimeExecution"]), "123456789012")

    for policies in (["UnrelatedPolicy"], ["S03RuntimeExecution", "S04GatewayInvoke"]):
        try:
            cleanup._preflight(plan, _TaggedS03Session(trust, policies), "123456789012")
        except cleanup.PlanError as error:
            assert "inline policies changed" in str(error)
        else:
            raise AssertionError(f"unrelated or additional tagged S03 policies must fail closed: {policies}")


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
    def __init__(self, pages, versioning="Enabled", mfa_delete=None, delete_response=None):
        self.paginator = _Paginator(pages)
        self.deleted = []
        self.bucket_deleted = False
        self.versioning = versioning
        self.mfa_delete = mfa_delete
        self.delete_response = delete_response or {}

    def get_bucket_versioning(self, **_kwargs):
        response = {} if self.versioning == "Unversioned" else {"Status": self.versioning}
        if self.mfa_delete is not None:
            response["MFADelete"] = self.mfa_delete
        return response

    def get_paginator(self, operation):
        assert operation == "list_object_versions"
        return self.paginator

    def delete_objects(self, **kwargs):
        self.deleted.extend(kwargs["Delete"]["Objects"])
        return self.delete_response

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
    ], "123456789012", "Enabled", "NotConfigured")
    assert client.deleted == [
        {"Key": "s08/a.zip", "VersionId": "old-v"},
        {"Key": "s08/old.zip", "VersionId": "marker-v"},
    ]
    assert client.bucket_deleted


def test_s3_cleanup_stops_on_unexpected_key_and_never_deletes_bucket():
    client = _S3([{"Versions": [{"Key": "other-course/data.zip", "VersionId": "v1", "Size": 1, "ETag": '"other"'}]}])
    try:
        cleanup._delete_bucket_objects(client, "udemy-agentcore-s08-apne1-123456789012-ab12cd", [], "123456789012", "Enabled", "NotConfigured")
    except cleanup.PlanError:
        pass
    else:
        raise AssertionError("unexpected object must stop cleanup")
    assert client.deleted == []
    assert not client.bucket_deleted


def test_s3_cleanup_stops_on_added_key_or_version_before_any_delete():
    client = _S3([{"Versions": [{"Key": "s08/foo/new.zip", "VersionId": "new-v", "Size": 2, "ETag": '"new"'}]}])
    try:
        cleanup._delete_bucket_objects(client, "udemy-agentcore-s08-apne1-123456789012-ab12cd", [], "123456789012", "Enabled", "NotConfigured")
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
        cleanup._delete_bucket_objects(client, bucket, [], "123456789012", "Enabled", "NotConfigured")
    except cleanup.PlanError:
        pass
    else:
        raise AssertionError("known S07 folder marker is allowed by prefix but must still be in the exact snapshot")
    assert client.deleted == []
    assert not client.bucket_deleted

    client = _S3([{"Versions": [{"Key": "other-course/", "VersionId": "bad", "Size": 0, "ETag": '"empty"'}]}])
    try:
        cleanup._delete_bucket_objects(client, bucket, [], "123456789012", "Enabled", "NotConfigured")
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
        ], "123456789012", "Enabled", "NotConfigured")
    except cleanup.PlanError:
        pass
    else:
        raise AssertionError("unexpected marker on a later page must stop cleanup")
    assert client.deleted == []
    assert not client.bucket_deleted


def test_s3_plan_requires_versioning_state_for_each_bucket():
    plan = valid_plan()
    del plan["bucket_versioning"]
    try:
        cleanup.validate_plan(plan, "123456789012", "ap-northeast-1")
    except cleanup.PlanError as error:
        assert "bucket_versioning" in str(error)
    else:
        raise AssertionError("plan must record bucket versioning state")


def test_s3_plan_requires_mfa_delete_snapshot_for_each_bucket():
    plan = valid_plan()
    del plan["bucket_mfa_delete"]
    try:
        cleanup.validate_plan(plan, "123456789012", "ap-northeast-1")
    except cleanup.PlanError as error:
        assert "bucket_mfa_delete" in str(error)
    else:
        raise AssertionError("plan must record GetBucketVersioning MFADelete state")


def test_s3_versioning_snapshot_preserves_native_mfa_delete_disabled_and_absent():
    bucket = "udemy-agentcore-s08-apne1-123456789012-ab12cd"
    disabled = _S3([], versioning="Enabled", mfa_delete="Disabled")
    assert cleanup._bucket_versioning_state(disabled, bucket, "123456789012") == ("Enabled", "Disabled")
    omitted = _S3([], versioning="Unversioned")
    assert cleanup._bucket_versioning_state(omitted, bucket, "123456789012") == ("Unversioned", "NotConfigured")


def test_s3_unversioned_null_version_uses_key_only_delete():
    bucket = "udemy-agentcore-s08-apne1-123456789012-ab12cd"
    client = _S3([{"Versions": [{"Key": "s08/a.zip", "VersionId": "null", "Size": 5, "ETag": '"object"'}]}], versioning="Unversioned")
    cleanup._delete_bucket_objects(client, bucket, [
        {"key": "s08/a.zip", "version_id": "null", "kind": "version", "size": 5, "etag": '"object"'},
    ], "123456789012", "Unversioned", "NotConfigured")
    assert client.deleted == [{"Key": "s08/a.zip"}]
    assert client.bucket_deleted


def test_s3_versioning_state_drift_stops_before_delete():
    client = _S3([{"Versions": [{"Key": "s08/a.zip", "VersionId": "null", "Size": 5, "ETag": '"object"'}]}], versioning="Suspended")
    try:
        cleanup._delete_bucket_objects(client, "udemy-agentcore-s08-apne1-123456789012-ab12cd", [], "123456789012", "Unversioned", "NotConfigured")
    except cleanup.PlanError as error:
        assert "versioning/MFA Delete state changed" in str(error)
    else:
        raise AssertionError("changed versioning state must stop cleanup")
    assert client.deleted == []
    assert not client.bucket_deleted


def test_s3_partial_delete_errors_stop_without_retry_or_bucket_delete():
    bucket = "udemy-agentcore-s08-apne1-123456789012-ab12cd"
    client = _S3([{"Versions": [{"Key": "s08/a.zip", "VersionId": "v1", "Size": 5, "ETag": '"object"'}]}], delete_response={"Errors": [{"Key": "s08/a.zip", "Code": "AccessDenied", "Message": "denied"}]})
    try:
        cleanup._delete_bucket_objects(client, bucket, [
            {"key": "s08/a.zip", "version_id": "v1", "kind": "version", "size": 5, "etag": '"object"'},
        ], "123456789012", "Enabled", "NotConfigured")
    except RuntimeError as error:
        assert "AccessDenied" in str(error)
    else:
        raise AssertionError("per-object API errors must be surfaced")
    assert client.deleted == [{"Key": "s08/a.zip", "VersionId": "v1"}]
    assert not client.bucket_deleted


def test_execute_stops_in_whole_plan_preflight_when_mfa_delete_enabled():
    bucket = "udemy-agentcore-s08-apne1-123456789012-ab12cd"
    plan = {
        "account_id": "123456789012",
        "region": "ap-northeast-1",
        "runtimes": [], "gateways": [], "lambda_functions": [],
        "buckets": [bucket],
        "bucket_versioning": {bucket: "Enabled"},
        "bucket_mfa_delete": {bucket: "Enabled"},
        "bucket_objects": {bucket: []},
        "log_groups": [], "roles": [], "role_trust_policies": {},
    }

    class Session:
        def __init__(self):
            self.mutations = []

        def client(self, name, **_kwargs):
            return self

        def get_caller_identity(self):
            return {"Account": "123456789012"}

        def get_bucket_versioning(self, **_kwargs):
            return {"Status": "Enabled", "MFADelete": "Enabled"}

        def delete_objects(self, **_kwargs):
            self.mutations.append("delete_objects")

        def delete_bucket(self, **_kwargs):
            self.mutations.append("delete_bucket")

    session = Session()
    try:
        cleanup.execute(plan, session, "123456789012", "ap-northeast-1")
    except cleanup.PlanError as error:
        assert "stop before any resource mutation" in str(error)
    else:
        raise AssertionError("MFA Delete must stop during whole-plan preflight")
    assert session.mutations == []


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
