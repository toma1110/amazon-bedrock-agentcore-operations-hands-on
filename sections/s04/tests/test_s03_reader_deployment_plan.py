from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

S04_DIR = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("s03_reader_deployment_plan", S04_DIR / "s03_reader_deployment_plan.py")
assert SPEC is not None and SPEC.loader is not None
plan_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = plan_module
SPEC.loader.exec_module(plan_module)


def _plan(tmp_path: Path) -> dict:
    lambda_zip = tmp_path / "lambda.zip"
    runtime_zip = tmp_path / "runtime.zip"
    lambda_zip.write_bytes(b"lambda test package")
    runtime_zip.write_bytes(b"runtime test package")
    return plan_module.build_plan(account="123456789012", run_id="r-test", bucket="artifact-bucket",
        s03_log_group="/aws/bedrock-agentcore/runtimes/s03-runtime-123-DEFAULT",
        s03_artifact_key="s03/runtime.zip", s03_artifact_sha256="a" * 64,
        lambda_zip=lambda_zip, runtime_zip=runtime_zip)


def test_plan_is_preview_only_names_are_unique_and_runtime_uses_valid_name_characters(tmp_path):
    plan = _plan(tmp_path)
    other = plan_module.names_for_run("r-test-other")
    assert plan["mode"] == "READ_ONLY_PREVIEW_NO_AWS_CLIENT_NO_EXECUTION"
    assert plan["scope"] == {"s03_mutations": 0, "model_invocations_by_preview": 0, "aws_writes_by_preview": 0}
    assert plan["resources"]["runtime"].startswith("udemy_agentcore_s04r")
    assert plan["resources"]["runtime"].replace("_", "").isalnum()
    assert plan["resources"]["runtime"] != other["runtime"]
    assert plan["borrowed_s03_artifact"]["owned_by_this_run"] is False
    assert plan["borrowed_s03_artifact"]["cleanup_action"] == "none"
    assert len(plan["sequence"]) == 7


def test_plan_shows_two_stage_runtime_use_and_exact_package_hashes(tmp_path):
    plan = _plan(tmp_path)
    assert [item["sha256"] for item in plan["candidate_packages"].values()] == [
        plan_module.sha256_file(Path(item["path"])) for item in plan["candidate_packages"].values()]
    assert plan["borrowed_s03_artifact"]["key"] == "s03/runtime.zip"
    assert any("borrowed S03 artifact" in step for step in plan["sequence"])
    assert any("reader artifact" in step for step in plan["sequence"])
    assert any("<target-name>___read_s03_invocation" in step for step in plan["sequence"])


@pytest.mark.parametrize("run_id", ["", "UPPER", "bad/name", "x" * 21, "with_underscore"])
def test_plan_rejects_invalid_run_id(tmp_path, run_id):
    lambda_zip = tmp_path / "lambda.zip"; lambda_zip.write_bytes(b"lambda")
    runtime_zip = tmp_path / "runtime.zip"; runtime_zip.write_bytes(b"runtime")
    with pytest.raises(ValueError, match="run_id"):
        plan_module.build_plan(account="123456789012", run_id=run_id, bucket="artifact-bucket",
            s03_log_group="/aws/bedrock-agentcore/runtimes/s03-runtime-123-DEFAULT",
            s03_artifact_key="s03/runtime.zip", s03_artifact_sha256="a" * 64,
            lambda_zip=lambda_zip, runtime_zip=runtime_zip)


def test_plan_rejects_wrong_account_region_inputs_and_invalid_s03_artifact_identity(tmp_path):
    lambda_zip = tmp_path / "lambda.zip"; lambda_zip.write_bytes(b"lambda")
    runtime_zip = tmp_path / "runtime.zip"; runtime_zip.write_bytes(b"runtime")
    params = {"account": "123456789012", "run_id": "r-test", "bucket": "artifact-bucket",
        "s03_log_group": "/aws/bedrock-agentcore/runtimes/s03-runtime-123-DEFAULT",
        "s03_artifact_key": "s03/runtime.zip", "s03_artifact_sha256": "a" * 64,
        "lambda_zip": lambda_zip, "runtime_zip": runtime_zip}
    for updates in ({"account": "bad"}, {"s03_log_group": "/other/group"},
                    {"s03_artifact_sha256": "not-a-sha"}, {"s03_artifact_key": ""}):
        with pytest.raises(ValueError):
            plan_module.build_plan(**(params | updates))
