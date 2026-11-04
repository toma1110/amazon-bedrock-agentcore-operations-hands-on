"""Print a read-only summary of the isolated S03 reader deployment sequence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

REGION = "ap-northeast-1"
RUN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,19}$")
LOG_RE = re.compile(r"^/aws/bedrock-agentcore/runtimes/(?P<runtime_id>[A-Za-z0-9_-]+)-DEFAULT$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def build_plan(*, account: str, run_id: str, bucket: str, s03_log_group: str,
               s03_artifact_key: str, s03_artifact_sha256: str,
               lambda_zip: Path, runtime_zip: Path) -> dict:
    if not re.fullmatch(r"[0-9]{12}", account):
        raise ValueError("account must be a 12-digit AWS account ID")
    names = names_for_run(run_id)
    match = LOG_RE.fullmatch(s03_log_group) if isinstance(s03_log_group, str) else None
    if not match:
        raise ValueError("s03_log_group must be the exact S03 DEFAULT Runtime log group")
    if not bucket or "/" in bucket or not s03_artifact_key:
        raise ValueError("pass the existing S03 bucket and exact artifact key")
    if not SHA_RE.fullmatch(s03_artifact_sha256):
        raise ValueError("s03_artifact_sha256 must be the saved 64-character S03 ZIP SHA-256")
    for path in (lambda_zip, runtime_zip):
        if not path.is_file():
            raise FileNotFoundError(path)
    lambda_hash, runtime_hash = sha256_file(lambda_zip), sha256_file(runtime_zip)
    return {
        "mode": "READ_ONLY_PREVIEW_NO_AWS_CLIENT_NO_EXECUTION",
        "account": account,
        "region": REGION,
        "run_id": run_id,
        "resources": names,
        "borrowed_s03_artifact": {"runtime_id": match.group("runtime_id"), "log_group": s03_log_group,
            "bucket": bucket, "key": s03_artifact_key, "sha256": s03_artifact_sha256,
            "owned_by_this_run": False, "cleanup_action": "none"},
        "candidate_packages": {
            "lambda": {"path": str(lambda_zip), "sha256": lambda_hash, "bucket": bucket,
                "key": f"{names['s3_prefix']}/lambda-{lambda_hash}.zip"},
            "reader_runtime": {"path": str(runtime_zip), "sha256": runtime_hash, "bucket": bucket,
                "key": f"{names['s3_prefix']}/reader-{runtime_hash}.zip"},
        },
        "sequence": [
            "Read-only: confirm caller account/region, S03 Runtime READY/version, exact S03 artifact key and SHA-256, shared bucket versioning is neither Enabled nor Suspended, and no run-specific resource/log-group name or S3-prefix collision.",
            "Upload only the two S04 reader packages under the unique run prefix; do not copy or delete the borrowed S03 package.",
            "Create the dedicated Lambda, Gateway/target, and roles with exact run-specific names and read-only S03 Logs access; grant the Runtime role logs:PutResourcePolicy only to the /aws/bedrock-agentcore/runtimes/<run-runtime-name>-* log-group ARN prefix; configure the Runtime with exposed Gateway tool name <target-name>___read_s03_invocation.",
            "Create a new S04 Runtime using the borrowed S03 artifact and wait for READY.",
            "Update only the new S04 Runtime to the reader artifact and Gateway URL/tool name; preserve network, protocol, MMDSv2, lifecycle, and role configuration, then verify READY/readback.",
            "Invoke once with the exact S03 invocation ID/timestamp, save the result, and stop only the saved S04 session. Send two direct Gateway negative requests without Runtime/model use.",
            "Cleanup only after the saved S04 session is stopped: snapshot the exact CloudWatch Logs policy (policy name, revision, document, and exact DEFAULT log-group ARN) using DescribeResourcePolicies(policyScope=RESOURCE, resourceArn=<exact DEFAULT log-group ARN>); stop if more than one policy or any identity mismatch exists. Delete the run-owned S04 Runtime and confirm it is absent, then delete only that exact saved resource-scoped policy (using its revision ID) and confirm it is absent, then delete the exact /aws/bedrock-agentcore/runtimes/<returned Runtime ID>-DEFAULT group and confirm it is absent. Also delete only the run-owned Gateway target/Gateway, Lambda, its Lambda log group, dedicated roles/policies, and the two uploaded candidate objects. Retain the S03 Runtime, role, log group, bucket, and borrowed S03 object. If bucket versioning is Enabled or Suspended, stop without changing it; object status checks do not verify old versions.",
        ],
        "scope": {"s03_mutations": 0, "model_invocations_by_preview": 0, "aws_writes_by_preview": 0},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--s03-log-group", required=True)
    parser.add_argument("--s03-artifact-key", required=True)
    parser.add_argument("--s03-artifact-sha256", required=True)
    parser.add_argument("--lambda-zip", required=True, type=Path)
    parser.add_argument("--runtime-zip", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(build_plan(account=args.account, run_id=args.run_id, bucket=args.bucket,
        s03_log_group=args.s03_log_group, s03_artifact_key=args.s03_artifact_key,
        s03_artifact_sha256=args.s03_artifact_sha256, lambda_zip=args.lambda_zip,
        runtime_zip=args.runtime_zip), indent=2))


if __name__ == "__main__":
    main()
