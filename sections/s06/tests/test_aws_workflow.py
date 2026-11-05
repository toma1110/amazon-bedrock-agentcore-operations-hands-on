from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
import sys

import pytest

S06_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(S06_DIR))

import aws_workflow as workflow


class FakeS3:
    def __init__(self):
        self.head_bucket_calls = []
        self.versioning_calls = []

    def head_bucket(self, **kwargs):
        self.head_bucket_calls.append(kwargs)

    def get_bucket_versioning(self, **kwargs):
        self.versioning_calls.append(kwargs)
        return {}


class FakeIAM:
    def __init__(self, *, role_exists=False):
        self.role_exists = role_exists

    def get_role(self, **kwargs):
        if self.role_exists:
            return {"Role": {"RoleName": kwargs["RoleName"]}}
        raise workflow.ClientError(
            {"Error": {"Code": "NoSuchEntity", "Message": "missing"}}, "GetRole"
        )


class FakeControl:
    def __init__(self, runtimes=()):
        self.runtimes = list(runtimes)

    def list_agent_runtimes(self, **kwargs):
        return {"agentRuntimes": self.runtimes}


class FakeLogs:
    def describe_log_groups(self, **kwargs):
        return {"logGroups": []}


def _clients(*, role_exists=False, runtimes=()):
    s3 = FakeS3()
    return {
        "sts": type("STS", (), {"get_caller_identity": lambda self: {"Account": "123456789012"}})(),
        "s3": s3,
        "iam": FakeIAM(role_exists=role_exists),
        "control": FakeControl(runtimes),
        "logs": FakeLogs(),
    }, s3


def test_preflight_refuses_existing_state_without_overwriting_it(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    original = b'{"schema_version":1,"phase":"keep-this"}\n'
    state_path.write_bytes(original)
    monkeypatch.setattr(workflow, "STATE_PATH", state_path)
    clients, _ = _clients()
    monkeypatch.setattr(workflow, "_clients", lambda profile: clients)
    zip_path = tmp_path / "unused.zip"
    zip_path.write_bytes(b"candidate")

    with pytest.raises(RuntimeError, match="state file already exists"):
        workflow.preflight(None, "123456789012", "artifact-bucket", zip_path)

    assert state_path.read_bytes() == original
    assert clients["s3"].head_bucket_calls == []


@pytest.mark.parametrize(
    ("role_exists", "runtimes"),
    [
        (True, ()),
        (False, ({"agentRuntimeName": workflow.RUNTIME_NAME},)),
    ],
)
def test_preflight_refuses_to_adopt_existing_aws_resources(
    tmp_path, monkeypatch, role_exists, runtimes
):
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(workflow, "STATE_PATH", state_path)
    clients, _ = _clients(role_exists=role_exists, runtimes=runtimes)
    monkeypatch.setattr(workflow, "_clients", lambda profile: clients)
    zip_path = tmp_path / "candidate.zip"
    zip_path.write_bytes(b"s06-candidate")

    with pytest.raises(RuntimeError, match="already exists; do not adopt"):
        workflow.preflight(None, "123456789012", "artifact-bucket", zip_path)

    assert not state_path.exists()


def test_preflight_records_hash_addressed_key_without_aws_writes(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(workflow, "STATE_PATH", state_path)
    clients, s3 = _clients()
    monkeypatch.setattr(workflow, "_clients", lambda profile: clients)
    zip_path = tmp_path / "candidate.zip"
    payload = b"exact reviewed candidate"
    zip_path.write_bytes(payload)

    workflow.preflight(None, "123456789012", "artifact-bucket", zip_path)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    digest = hashlib.sha256(payload).hexdigest()
    assert state["zip_sha256"] == digest
    assert state["artifact_key"].startswith(f"s06/{state['run_id']}/{digest}/")
    assert state["phase"] == "preflight_complete"
    assert state["requests"] == []
    assert s3.head_bucket_calls == [{"Bucket": "artifact-bucket", "ExpectedBucketOwner": "123456789012"}]
    assert s3.versioning_calls == [{"Bucket": "artifact-bucket", "ExpectedBucketOwner": "123456789012"}]


def test_deploy_will_not_replay_a_saved_partial_write_phase(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(workflow, "STATE_PATH", state_path)
    state_path.write_text(json.dumps({
        "schema_version": 1,
        "account_id": "123456789012",
        "runtime_name": workflow.RUNTIME_NAME,
        "role_name": workflow.ROLE_NAME,
        "phase": "create_role_requested",
        "requests": [],
    }), encoding="utf-8")
    clients, _ = _clients()
    mutations = []

    class WriteTrap:
        def __getattr__(self, name):
            return lambda **kwargs: mutations.append((name, kwargs))

    clients.update({"iam": WriteTrap(), "s3": WriteTrap(), "control": WriteTrap()})
    monkeypatch.setattr(workflow, "_clients", lambda profile: clients)

    with pytest.raises(RuntimeError, match="not at a deploy-ready phase"):
        workflow.deploy(None, "123456789012")

    assert mutations == []
    assert json.loads(state_path.read_text(encoding="utf-8"))["phase"] == "create_role_requested"


def test_reconcile_is_read_only_and_requires_exact_tagged_role_and_artifact(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(workflow, "STATE_PATH", state_path)
    role_trust, role_policy = workflow._runtime_role_documents("123456789012")
    zip_bytes = b"candidate"
    state = {
        "schema_version": 1, "account_id": "123456789012", "region": workflow.REGION,
        "runtime_name": workflow.RUNTIME_NAME, "role_name": workflow.ROLE_NAME,
        "phase": "put_object_requested", "requests": [], "run_id": "s06-test-1234",
        "role_trust": role_trust, "role_policy": role_policy,
        "role_arn": "arn:aws:iam::123456789012:role/udemy_s06_obs_20261105_runtime",
        "artifact_bucket": "artifact-bucket", "artifact_key": "s06/s06-test-1234/hash/artifact.zip",
        "zip_sha256": hashlib.sha256(zip_bytes).hexdigest(), "zip_size": len(zip_bytes),
        "desired_environment": {"S06_RUN_ID": "s06-test-1234"},
        "desired_lifecycle": {"idleRuntimeSessionTimeout": 300, "maxLifetime": 900},
        "runtime_create_request": {"agentRuntimeArtifact": {"codeConfiguration": {
            "code": {"s3": {"bucket": "artifact-bucket", "prefix": "s06/s06-test-1234/hash/artifact.zip"}},
            "runtime": "PYTHON_3_13", "entryPoint": ["runtime_app.py"],
        }}},
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    mutations = []

    class IAM:
        def get_role(self, **kwargs):
            return {"Role": {
                "Arn": state["role_arn"], "Description": "Udemy S06 isolated Runtime execution role",
                "AssumeRolePolicyDocument": role_trust,
                "Tags": [{"Key": "RunId", "Value": state["run_id"]},
                         {"Key": "CourseSection", "Value": "S06"},
                         {"Key": "Purpose", "Value": "hands-on"}],
            }}

        def get_role_policy(self, **kwargs):
            return {"PolicyDocument": role_policy}

        def list_attached_role_policies(self, **kwargs):
            return {"AttachedPolicies": []}

        def __getattr__(self, name):
            return lambda **kwargs: mutations.append((name, kwargs))

    class S3:
        def head_object(self, **kwargs):
            return {"ContentLength": state["zip_size"], "Metadata": {
                "sha256": state["zip_sha256"], "run-id": state["run_id"],
                "purpose": "s06-hands-on",
            }}

        def __getattr__(self, name):
            return lambda **kwargs: mutations.append((name, kwargs))

    clients = {
        "sts": type("STS", (), {"get_caller_identity": lambda self: {"Account": "123456789012"}})(),
        "iam": IAM(), "s3": S3(),
        "control": type("Control", (), {"list_agent_runtimes": lambda self, **kwargs: {"agentRuntimes": []}})(),
        "logs": FakeLogs(),
    }
    monkeypatch.setattr(workflow, "_clients", lambda profile: clients)

    workflow.reconcile(None, "123456789012")

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["phase"] == "partial_reconciled"
    assert saved["reconciled_resources"]["role"]["arn"] == state["role_arn"]
    assert saved["reconciled_resources"]["artifact"]["sha256"] == state["zip_sha256"]
    assert saved["reconciled_resources"]["runtime"] is None
    assert mutations == []


def test_partial_cleanup_stops_if_role_changed_after_reconcile(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(workflow, "STATE_PATH", state_path)
    role_trust, role_policy = workflow._runtime_role_documents("123456789012")
    state_path.write_text(json.dumps({
        "schema_version": 1, "account_id": "123456789012", "region": workflow.REGION,
        "runtime_name": workflow.RUNTIME_NAME, "role_name": workflow.ROLE_NAME,
        "phase": "partial_reconciled", "requests": [], "run_id": "s06-test-1234",
        "role_arn": "arn:aws:iam::123456789012:role/udemy_s06_obs_20261105_runtime",
        "role_trust": role_trust, "role_policy": role_policy,
        "artifact_bucket": "artifact-bucket", "artifact_key": "s06/key.zip",
        "reconciled_resources": {"role": {"arn": "present"}, "artifact": None, "runtime": None},
    }), encoding="utf-8")
    mutations = []

    class IAM:
        def get_role(self, **kwargs):
            return {"Role": {
                "Arn": "arn:aws:iam::123456789012:role/udemy_s06_obs_20261105_runtime",
                "Description": "Udemy S06 isolated Runtime execution role",
                "AssumeRolePolicyDocument": role_trust,
                "Tags": [{"Key": "RunId", "Value": "some-other-run"}],
            }}

        def __getattr__(self, name):
            return lambda **kwargs: mutations.append((name, kwargs))

    clients = {
        "sts": type("STS", (), {"get_caller_identity": lambda self: {"Account": "123456789012"}})(),
        "iam": IAM(), "s3": object(), "control": object(), "logs": object(),
    }
    monkeypatch.setattr(workflow, "_clients", lambda profile: clients)

    with pytest.raises(RuntimeError, match="Role changed after reconcile"):
        workflow.cleanup(None, "123456789012")

    assert mutations == []


def test_reconcile_verifies_runtime_tags_and_full_configuration(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(workflow, "STATE_PATH", state_path)
    role_trust, role_policy = workflow._runtime_role_documents("123456789012")
    runtime_arn = "arn:aws:bedrock-agentcore:ap-northeast-1:123456789012:runtime/s06-test-id"
    state = {
        "schema_version": 1, "account_id": "123456789012", "region": workflow.REGION,
        "runtime_name": workflow.RUNTIME_NAME, "role_name": workflow.ROLE_NAME,
        "phase": "update_runtime_requested", "requests": [], "run_id": "s06-test-1234",
        "role_trust": role_trust, "role_policy": role_policy,
        "role_arn": "arn:aws:iam::123456789012:role/udemy_s06_obs_20261105_runtime",
        "artifact_bucket": "artifact-bucket", "artifact_key": "s06/s06-test-1234/hash/artifact.zip",
        "zip_sha256": "a" * 64, "zip_size": 9,
        "desired_environment": {"S06_RUN_ID": "s06-test-1234"},
        "desired_lifecycle": {"idleRuntimeSessionTimeout": 300, "maxLifetime": 900},
        "runtime_create_request": {"agentRuntimeArtifact": {"codeConfiguration": {
            "code": {"s3": {"bucket": "artifact-bucket", "prefix": "s06/s06-test-1234/hash/artifact.zip"}},
            "runtime": "PYTHON_3_13", "entryPoint": ["runtime_app.py"],
        }}},
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")

    class IAM:
        def get_role(self, **kwargs):
            return {"Role": {
                "Arn": state["role_arn"], "Description": "Udemy S06 isolated Runtime execution role",
                "AssumeRolePolicyDocument": role_trust,
                "Tags": [{"Key": "RunId", "Value": state["run_id"]},
                         {"Key": "CourseSection", "Value": "S06"},
                         {"Key": "Purpose", "Value": "hands-on"}],
            }}

        def get_role_policy(self, **kwargs):
            return {"PolicyDocument": role_policy}

        def list_attached_role_policies(self, **kwargs):
            return {"AttachedPolicies": []}

    runtime = {
        "agentRuntimeId": "s06-test-id", "agentRuntimeArn": runtime_arn,
        "agentRuntimeName": workflow.RUNTIME_NAME, "status": "READY",
        "roleArn": state["role_arn"], "platformVersion": "V2",
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "environmentVariables": state["desired_environment"],
        "lifecycleConfiguration": state["desired_lifecycle"],
        "metadataConfiguration": {"requireMMDSV2": True},
        "agentRuntimeArtifact": state["runtime_create_request"]["agentRuntimeArtifact"],
    }

    class Control:
        def list_agent_runtimes(self, **kwargs):
            return {"agentRuntimes": [{"agentRuntimeId": runtime["agentRuntimeId"],
                                      "agentRuntimeName": runtime["agentRuntimeName"]}]}

        def get_agent_runtime(self, **kwargs):
            return runtime

        def list_tags_for_resource(self, **kwargs):
            assert kwargs["resourceArn"] == runtime_arn
            return {"tags": {"CourseSection": "S06", "Purpose": "hands-on",
                              "RunId": state["run_id"]}}

    clients = {
        "sts": type("STS", (), {"get_caller_identity": lambda self: {"Account": "123456789012"}})(),
        "iam": IAM(), "s3": type("S3", (), {"head_object": lambda self, **kwargs: {
            "ContentLength": state["zip_size"],
            "Metadata": {"sha256": state["zip_sha256"], "run-id": state["run_id"],
                         "purpose": "s06-hands-on"},
        }})(), "logs": FakeLogs(),
        "control": Control(),
    }
    monkeypatch.setattr(workflow, "_clients", lambda profile: clients)

    workflow.reconcile(None, "123456789012")

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["phase"] == "partial_reconciled"
    assert saved["reconcile_expected_metadata"] is True
    assert saved["reconciled_resources"]["runtime"]["arn"] == runtime_arn
    assert saved["log_group"] == f"{workflow.LOG_GROUP_PREFIX}s06-test-id-DEFAULT"


def test_runtime_role_is_scoped_to_named_s06_runtime_and_log_group():
    trust, policy = workflow._runtime_role_documents("123456789012")
    serialized = json.dumps({"trust": trust, "policy": policy}, sort_keys=True)

    assert workflow.RUNTIME_NAME in serialized
    assert workflow.REGION in serialized
    assert "bedrock:InvokeModel" in serialized
    assert "arn:aws:bedrock:ap-northeast-1::foundation-model/amazon.nova-lite-v1:0" in serialized
    assert workflow.NAMESPACE in serialized
    logs_arn = f"arn:aws:logs:{workflow.REGION}:123456789012:log-group:{workflow.LOG_GROUP_PREFIX}{workflow.RUNTIME_NAME}-*"
    assert logs_arn in serialized
    assert f"{logs_arn}:log-stream:*" in serialized
    assert "s03" not in serialized.lower()
    assert "s04" not in serialized.lower()


def test_readback_persists_started_marker_before_first_aws_api_and_refuses_repeat(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(workflow, "STATE_PATH", state_path)
    requests = [
        {"sent_at": f"2026-11-05T00:0{index}:00Z", "received_at": f"2026-11-05T00:0{index}:01Z"}
        for index in range(3)
    ]
    state_path.write_text(json.dumps({
        "schema_version": 1, "account_id": "123456789012", "runtime_name": workflow.RUNTIME_NAME,
        "role_name": workflow.ROLE_NAME, "phase": "invoke_complete", "requests": requests,
    }), encoding="utf-8")
    clients_created = []

    class STS:
        def get_caller_identity(self):
            assert json.loads(state_path.read_text(encoding="utf-8"))["phase"] == "readback_started"
            raise RuntimeError("simulated account lookup interruption")

    def make_clients(profile):
        clients_created.append(profile)
        return {"sts": STS()}

    monkeypatch.setattr(workflow, "_clients", make_clients)

    with pytest.raises(RuntimeError, match="simulated account lookup interruption"):
        workflow.readback(None, "123456789012")

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["phase"] == "readback_started"
    assert saved["readback_started_at"]
    with pytest.raises(RuntimeError, match="already started; do not query again"):
        workflow.readback(None, "123456789012")
    assert clients_created == [None]


@pytest.mark.parametrize(
    ("phase", "raw_request_count"),
    [("invoke_3_uncertain", 3), ("invoke_3_requested", 2)],
)
def test_uncertain_readback_is_bounded_read_only_one_shot_and_preserves_invoke_history(
    tmp_path, monkeypatch, phase, raw_request_count
):
    state_path = tmp_path / "state.json"
    raw_path = tmp_path / "private-record.json"
    monkeypatch.setattr(workflow, "STATE_PATH", state_path)
    monkeypatch.setattr(workflow, "RAW_PATH", raw_path)
    runtime_id = "s06-exact-id"
    log_group = f"{workflow.LOG_GROUP_PREFIX}{runtime_id}-DEFAULT"
    expected_cases = ["valid", "valid-repeat", "missing-prompt"]
    expected_bodies = [
        {"prompt": workflow.FIXED_PROMPT},
        {"prompt": workflow.FIXED_PROMPT},
        {},
    ]
    requests = [
        {"sequence": i, "case": expected_cases[i - 1],
         "sent_at": f"2026-11-05T12:0{i-1}:00.000Z",
         "received_at": f"2026-11-05T12:0{i-1}:05.000Z"}
        for i in range(1, 4)
    ]
    state_path.write_text(json.dumps({
        "schema_version": 1, "account_id": "123456789012", "runtime_name": workflow.RUNTIME_NAME,
        "role_name": workflow.ROLE_NAME, "phase": phase, "requests": requests,
        "runtime_id": runtime_id, "runtime_arn": f"arn:runtime:{runtime_id}",
        "session_id": "s06-test-session", "run_id": "s06-test-run", "log_group": log_group,
        "artifact_bucket": "artifact-bucket", "artifact_key": "s06/exact.zip",
    }), encoding="utf-8")
    raw_requests = [
        {**item, "request_body": expected_bodies[index]}
        for index, item in enumerate(requests[:raw_request_count])
    ]
    if phase == "invoke_3_uncertain":
        raw_requests[-1].update({"outer_error_code": "RuntimeClientError", "outer_http_status": 424})
    raw_path.write_text(json.dumps({
        "account_id": "123456789012", "runtime_id": runtime_id,
        "session_id": "s06-test-session", "fixed_prompt": workflow.FIXED_PROMPT,
        "requests": raw_requests,
    }), encoding="utf-8")
    calls = {"retention": [], "logs": [], "metrics": [], "invoke": []}
    observed_phases = []

    class STS:
        def get_caller_identity(self):
            observed_phases.append(json.loads(state_path.read_text(encoding="utf-8"))["phase"])
            return {"Account": "123456789012"}

    class Logs:
        def describe_log_groups(self, **kwargs):
            return {"logGroups": [{"logGroupName": log_group}]}
        def put_retention_policy(self, **kwargs):
            calls["retention"].append(kwargs)
        def filter_log_events(self, **kwargs):
            calls["logs"].append(kwargs)
            return {"events": [{"message": "one bounded event"}]}

    class CloudWatch:
        def get_metric_data(self, **kwargs):
            calls["metrics"].append(kwargs)
            return {"MetricDataResults": [{"Id": "s06latency", "Values": [1.0]}]}

    class Runtime:
        def invoke_agent_runtime(self, **kwargs):
            calls["invoke"].append(kwargs)

    class IAM:
        def get_role(self, **kwargs):
            return {"Role": {"Description": "different run"}}

    clients = {
        "sts": STS(), "logs": Logs(), "cloudwatch": CloudWatch(),
        "runtime": Runtime(), "iam": IAM(),
    }
    monkeypatch.setattr(workflow, "_clients", lambda profile: clients)

    workflow.readback(None, "123456789012")

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    raw_saved = json.loads(raw_path.read_text(encoding="utf-8"))
    assert saved["phase"] == "readback_complete"
    assert saved["readback_source_phase"] == phase
    assert saved["readback_mode"] == "read_only_uncertain"
    assert saved["requests"] == requests
    assert saved["s06_readback"]["source_phase"] == phase
    assert saved["s06_readback"]["read_only"] is True
    assert saved["s06_readback"]["invoke_status_confirmed"] is False
    assert saved["s06_readback"]["next_review"] == (
        "manual independent QA; application status remains unconfirmed"
    )
    assert saved["s06_readback"]["captured_request_count"] == raw_request_count
    assert observed_phases[0] == "readback_started"
    assert raw_saved["requests"] == raw_requests
    assert raw_saved["readback"] == saved["s06_readback"]
    assert calls["retention"] == []
    assert len(calls["logs"]) == 1
    assert calls["logs"][0]["logGroupName"] == log_group
    assert calls["logs"][0]["limit"] == 100
    expected_start = datetime.fromisoformat(requests[0]["sent_at"].replace("Z", "+00:00"))
    expected_end = datetime.fromisoformat(requests[raw_request_count - 1]["received_at"].replace("Z", "+00:00"))
    assert calls["logs"][0]["startTime"] == int((expected_start.timestamp() - 60) * 1000)
    assert calls["logs"][0]["endTime"] == int((expected_end.timestamp() + 60) * 1000)
    metric_query = calls["metrics"][0]["MetricDataQueries"]
    assert calls["metrics"][0]["MaxDatapoints"] == 3
    assert metric_query[0]["MetricStat"]["Metric"]["Dimensions"] == [
        {"Name": "Section", "Value": "S06"},
        {"Name": "RunId", "Value": "s06-test-run"},
    ]

    with pytest.raises(RuntimeError, match="already started; do not query again"):
        workflow.readback(None, "123456789012")
    with pytest.raises(RuntimeError, match="READY with no saved invocation"):
        workflow.invoke(None, "123456789012")
    assert calls["invoke"] == []
    with pytest.raises(RuntimeError, match="execution role identity differs"):
        workflow.cleanup(None, "123456789012")
    assert json.loads(state_path.read_text(encoding="utf-8"))["phase"] == "readback_complete"


def test_uncertain_readback_stops_without_exact_received_raw_window(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    raw_path = tmp_path / "private-record.json"
    monkeypatch.setattr(workflow, "STATE_PATH", state_path)
    monkeypatch.setattr(workflow, "RAW_PATH", raw_path)
    state_path.write_text(json.dumps({
        "schema_version": 1, "account_id": "123456789012", "runtime_name": workflow.RUNTIME_NAME,
        "role_name": workflow.ROLE_NAME, "phase": "invoke_1_uncertain", "requests": [
            {"sequence": 1, "sent_at": "2026-11-05T12:00:00.000Z"},
        ], "runtime_id": "s06-id", "session_id": "s06-session",
    }), encoding="utf-8")
    raw_path.write_text(json.dumps({
        "account_id": "123456789012", "runtime_id": "s06-id", "session_id": "s06-session",
        "requests": [],
    }), encoding="utf-8")
    clients_created = []
    monkeypatch.setattr(workflow, "_clients", lambda profile: clients_created.append(profile))

    with pytest.raises(RuntimeError, match="bounded received RAW subset"):
        workflow.readback(None, "123456789012")

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["phase"] == "invoke_1_uncertain"
    assert not saved.get("readback_started_at")
    assert clients_created == []


@pytest.mark.parametrize("mutation", ["raw-case", "raw-body", "state-case"])
def test_uncertain_readback_rejects_raw_or_state_that_does_not_match_fixed_send_intent(
    tmp_path, monkeypatch, mutation
):
    state_path = tmp_path / "state.json"
    raw_path = tmp_path / "private-record.json"
    monkeypatch.setattr(workflow, "STATE_PATH", state_path)
    monkeypatch.setattr(workflow, "RAW_PATH", raw_path)
    sent_at = "2026-11-05T12:00:00.000Z"
    received_at = "2026-11-05T12:00:05.000Z"
    state_case = "valid"
    raw_case = "valid"
    raw_body = {"prompt": workflow.FIXED_PROMPT}
    if mutation == "raw-case":
        raw_case = "missing-prompt"
    elif mutation == "raw-body":
        raw_body = {"prompt": "different prompt"}
    else:
        state_case = "valid-repeat"
    state_path.write_text(json.dumps({
        "schema_version": 1, "account_id": "123456789012", "runtime_name": workflow.RUNTIME_NAME,
        "role_name": workflow.ROLE_NAME, "phase": "invoke_1_uncertain",
        "requests": [{"sequence": 1, "case": state_case, "sent_at": sent_at}],
        "runtime_id": "s06-id", "session_id": "s06-session",
    }), encoding="utf-8")
    raw_path.write_text(json.dumps({
        "account_id": "123456789012", "runtime_id": "s06-id", "session_id": "s06-session",
        "fixed_prompt": workflow.FIXED_PROMPT,
        "requests": [{
            "sequence": 1, "case": raw_case, "request_body": raw_body,
            "sent_at": sent_at, "received_at": received_at,
        }],
    }), encoding="utf-8")
    clients_created = []
    monkeypatch.setattr(workflow, "_clients", lambda profile: clients_created.append(profile))

    with pytest.raises(RuntimeError, match="case/body does not match the fixed send intent"):
        workflow.readback(None, "123456789012")

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["phase"] == "invoke_1_uncertain"
    assert not saved.get("readback_started_at")
    assert clients_created == []


@pytest.mark.parametrize("body_failure", ["read", "decode", "json"])
def test_invoke_response_processing_failure_is_durable_uncertain_and_never_reinvoked(
    tmp_path, monkeypatch, body_failure
):
    state_path = tmp_path / "state.json"
    raw_path = tmp_path / "private-record.json"
    monkeypatch.setattr(workflow, "STATE_PATH", state_path)
    monkeypatch.setattr(workflow, "RAW_PATH", raw_path)
    state_path.write_text(json.dumps({
        "schema_version": 1, "account_id": "123456789012", "runtime_name": workflow.RUNTIME_NAME,
        "role_name": workflow.ROLE_NAME, "phase": "ready", "requests": [],
        "runtime_id": "s06-id", "runtime_arn": "arn:runtime:s06-id", "run_id": "s06-test",
        "artifact_bucket": "artifact-bucket", "artifact_key": "s06/exact.zip",
    }), encoding="utf-8")
    calls = []

    class BrokenBody:
        def read(self):
            if body_failure == "read":
                raise ValueError("body stream read failure")
            if body_failure == "decode":
                return b"\xff"
            return b"{"

    class Runtime:
        def invoke_agent_runtime(self, **kwargs):
            calls.append(kwargs)
            return {"statusCode": 200, "response": BrokenBody()}

    clients = {
        "sts": type("STS", (), {"get_caller_identity": lambda self: {"Account": "123456789012"}})(),
        "runtime": Runtime(),
    }
    monkeypatch.setattr(workflow, "_clients", lambda profile: clients)

    with pytest.raises(RuntimeError, match="uncertain; inspect this exact request.*do not retry"):
        workflow.invoke(None, "123456789012")

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["phase"] == "invoke_1_uncertain"
    assert saved["requests"][0]["outer_error_type"] in {
        "ValueError", "UnicodeDecodeError", "JSONDecodeError",
    }
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("error", "expected_phase"),
    [
        ({"Error": {"Code": "AccessDeniedException", "Message": "denied"},
          "ResponseMetadata": {"HTTPStatusCode": 403}}, "invoke_3_uncertain"),
        ({"Error": {"Code": "ThrottlingException", "Message": "slow down"},
          "ResponseMetadata": {"HTTPStatusCode": 429}}, "invoke_3_uncertain"),
        ({"Error": {"Code": "RuntimeClientError", "Message": "runtime error"},
          "ResponseMetadata": {"HTTPStatusCode": 424}}, "invoke_3_uncertain"),
        ({"Error": {"Code": "RuntimeClientError", "Message": "Received error (400) from runtime."},
          "ResponseMetadata": {"HTTPStatusCode": 424}}, "invoke_3_recorded"),
    ],
)
def test_missing_prompt_only_records_proven_validation_and_other_client_errors_reconcile_without_reinvoke(
    tmp_path, monkeypatch, error, expected_phase
):
    state_path = tmp_path / "state.json"
    raw_path = tmp_path / "private-record.json"
    monkeypatch.setattr(workflow, "STATE_PATH", state_path)
    monkeypatch.setattr(workflow, "RAW_PATH", raw_path)
    monkeypatch.setattr(workflow.time, "sleep", lambda seconds: None)
    state_path.write_text(json.dumps({
        "schema_version": 1, "account_id": "123456789012", "runtime_name": workflow.RUNTIME_NAME,
        "role_name": workflow.ROLE_NAME, "phase": "ready", "requests": [],
        "runtime_id": "s06-id", "runtime_arn": "arn:runtime:s06-id", "run_id": "s06-test",
        "artifact_bucket": "artifact-bucket", "artifact_key": "s06/exact.zip",
    }), encoding="utf-8")
    calls = []

    class Runtime:
        def invoke_agent_runtime(self, **kwargs):
            calls.append(kwargs)
            if len(calls) < 3:
                return {"statusCode": 200, "response": type("Body", (), {"read": lambda self: b'{"ok":true}'})()}
            raise workflow.ClientError(error, "InvokeAgentRuntime")

    clients = {
        "sts": type("STS", (), {"get_caller_identity": lambda self: {"Account": "123456789012"}})(),
        "runtime": Runtime(),
    }
    monkeypatch.setattr(workflow, "_clients", lambda profile: clients)

    if expected_phase == "invoke_3_uncertain":
        with pytest.raises(RuntimeError, match="uncertain; inspect this exact request.*do not retry"):
            workflow.invoke(None, "123456789012")
    else:
        workflow.invoke(None, "123456789012")

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["phase"] == ("invoke_complete" if expected_phase == "invoke_3_recorded" else expected_phase)
    assert len(calls) == 3
    if expected_phase == "invoke_3_uncertain":
        assert saved["requests"][2]["validation_status_evidence"].startswith("unconfirmed")
        with pytest.raises(RuntimeError, match="READY with no saved invocation"):
            workflow.invoke(None, "123456789012")
        assert len(calls) == 3

        class MissingResources:
            def get_role(self, **kwargs):
                raise workflow.ClientError({"Error": {"Code": "NoSuchEntity"}}, "GetRole")
            def head_object(self, **kwargs):
                raise workflow.ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")
            def list_agent_runtimes(self, **kwargs):
                return {"agentRuntimes": []}
            def get_agent_runtime(self, **kwargs):
                raise workflow.ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "GetAgentRuntime")
            def describe_log_groups(self, **kwargs):
                return {"logGroups": []}

        clients.update({
            "iam": MissingResources(), "s3": MissingResources(),
            "control": MissingResources(), "logs": MissingResources(),
        })
        workflow.reconcile(None, "123456789012")
        reconciled = json.loads(state_path.read_text(encoding="utf-8"))
        assert reconciled["phase"] == "partial_reconciled"
        assert reconciled["reconcile_from_phase"] == "invoke_3_uncertain"
        workflow.cleanup(None, "123456789012")
        assert json.loads(state_path.read_text(encoding="utf-8"))["phase"] == "resources_deleted"
        assert len(calls) == 3
    else:
        assert saved["requests"][2]["original_status_code"] == 400
        assert saved["requests"][2]["validation_status_evidence"] == (
            "RuntimeClientError/424 with original application status 400"
        )
    assert raw_path.exists()


def test_partial_cleanup_waits_out_deleting_runtime_before_removing_log_group(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(workflow, "STATE_PATH", state_path)
    runtime_id = "s06-id"
    runtime_arn = f"arn:aws:bedrock-agentcore:{workflow.REGION}:123456789012:runtime/{runtime_id}"
    group_name = f"{workflow.LOG_GROUP_PREFIX}{runtime_id}-DEFAULT"
    artifact = {"codeConfiguration": {"code": {
        "s3": {"bucket": "artifact-bucket", "prefix": "s06/exact.zip"},
        "runtime": "PYTHON_3_13", "entryPoint": ["runtime_app.py"],
    }}}
    state = {
        "schema_version": 1, "account_id": "123456789012", "region": workflow.REGION,
        "runtime_name": workflow.RUNTIME_NAME, "role_name": workflow.ROLE_NAME,
        "phase": "partial_reconciled", "run_id": "s06-test", "role_arn": "arn:aws:iam::123456789012:role/s06",
        "runtime_id": runtime_id, "runtime_arn": runtime_arn, "log_group": group_name,
        "artifact_bucket": "artifact-bucket", "artifact_key": "s06/exact.zip",
        "desired_environment": {"S06_RUN_ID": "s06-test"},
        "desired_lifecycle": {"idleRuntimeSessionTimeout": 300, "maxLifetime": 900},
        "runtime_create_request": {"agentRuntimeArtifact": artifact},
        "reconcile_expected_metadata": True,
        "reconciled_resources": {
            "role": None, "artifact": None,
            "runtime": {"id": runtime_id, "arn": runtime_arn, "status": "DELETING"},
            "log_group": {"name": group_name},
        },
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    events = []
    runtime = {
        "agentRuntimeId": runtime_id, "agentRuntimeArn": runtime_arn,
        "agentRuntimeName": workflow.RUNTIME_NAME, "status": "DELETING",
        "roleArn": state["role_arn"], "platformVersion": "V2",
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "environmentVariables": state["desired_environment"],
        "lifecycleConfiguration": state["desired_lifecycle"],
        "metadataConfiguration": {"requireMMDSV2": True},
        "agentRuntimeArtifact": artifact,
    }

    class Control:
        def get_agent_runtime(self, **kwargs):
            events.append("get-runtime")
            return runtime

        def list_tags_for_resource(self, **kwargs):
            return {"tags": {"CourseSection": "S06", "Purpose": "hands-on", "RunId": state["run_id"]}}

        def delete_agent_runtime(self, **kwargs):
            raise AssertionError("must not resend DeleteAgentRuntime while status is DELETING")

    class Logs:
        present = True

        def describe_log_groups(self, **kwargs):
            events.append("describe-log-group")
            return {"logGroups": [{"logGroupName": group_name}] if self.present else []}

        def delete_log_group(self, **kwargs):
            events.append("delete-log-group")
            self.present = False

    logs = Logs()
    clients = {"control": Control(), "logs": logs}
    monkeypatch.setattr(workflow, "_wait_runtime_absent", lambda control, rid: events.append("wait-runtime-absent"))

    workflow._cleanup_reconciled_partial(clients, state, "123456789012")

    assert events.index("wait-runtime-absent") < events.index("delete-log-group")
    assert logs.present is False
    assert json.loads(state_path.read_text(encoding="utf-8"))["phase"] == "resources_deleted"


def test_partial_cleanup_removes_orphaned_exact_log_group_after_runtime_absence(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(workflow, "STATE_PATH", state_path)
    runtime_id = "s06-gone"
    group_name = f"{workflow.LOG_GROUP_PREFIX}{runtime_id}-DEFAULT"
    state = {
        "schema_version": 1, "account_id": "123456789012", "region": workflow.REGION,
        "runtime_name": workflow.RUNTIME_NAME, "role_name": workflow.ROLE_NAME,
        "phase": "partial_reconciled", "run_id": "s06-test", "runtime_id": runtime_id,
        "log_group": group_name, "artifact_bucket": "artifact-bucket", "artifact_key": "s06/key.zip",
        "reconciled_resources": {"role": None, "artifact": None, "runtime": None,
                                  "log_group": {"name": group_name}},
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    events = []

    class Control:
        def get_agent_runtime(self, **kwargs):
            raise workflow.ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "GetAgentRuntime")

    class Logs:
        present = True

        def describe_log_groups(self, **kwargs):
            return {"logGroups": [{"logGroupName": group_name}] if self.present else []}

        def delete_log_group(self, **kwargs):
            events.append("delete-log-group")
            self.present = False

    logs = Logs()
    workflow._cleanup_reconciled_partial(
        {"control": Control(), "logs": logs}, state, "123456789012"
    )

    assert events == ["delete-log-group"]
    assert not logs.present
