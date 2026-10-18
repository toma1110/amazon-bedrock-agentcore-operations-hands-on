"""Interactive S08 lecture runner for real, bounded Runtime operations."""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import sys
import time
import uuid
from pathlib import Path

import boto3
from botocore.config import Config

REGION = "ap-northeast-1"
RUNTIME_NAME_PREFIX = "udemy_agentcore_s08-"
ROLE_NAME = "udemy-agentcore-s08-runtime"
ENDPOINT = "s08_training"
QUESTION = "catalog-api の5xxを調べる順序を教えてください。"
WAIT_SECONDS = 255  # Plus at most one 45s read timeout at the final poll boundary.
CONTROL_CONFIG = Config(connect_timeout=5, read_timeout=45, retries={"mode": "standard", "total_max_attempts": 1})
POLL_SECONDS = 5
EXPECTED_VERSION = {"compare": "1", "rollback": "2"}


def runtime_id_from_arn(runtime_arn: str, expected_account: str) -> str:
    parts = runtime_arn.split(":", 5)
    if len(parts) != 6 or parts[2] != "bedrock-agentcore" or parts[3] != REGION or parts[4] != expected_account:
        raise ValueError("Runtime ARNのaccountまたはregionが指定値と一致しません")
    resource = parts[5]
    if not resource.startswith("runtime/" + RUNTIME_NAME_PREFIX):
        raise ValueError("指定されたARNはこの演習専用のS08 Runtimeではありません")
    runtime_id = resource.removeprefix("runtime/")
    if "/" in runtime_id:
        raise ValueError("Runtime本体のARNを指定してください。endpoint ARNは指定できません")
    return runtime_id


def verify_target(expected_account: str, region: str) -> tuple[boto3.Session, str]:
    if region != REGION:
        raise ValueError(f"この演習のregionは{REGION}です")
    if len(expected_account) != 12 or not expected_account.isdigit():
        raise ValueError("--expected-accountには12桁のAWS account IDを指定してください")
    session = boto3.Session(region_name=region)
    actual = session.client("sts", config=CONTROL_CONFIG).get_caller_identity()["Account"]
    if actual != expected_account:
        raise ValueError("AWS accountが確認値と一致しません。処理を停止しました")
    return session, actual


def _runtime_state(control, runtime_id: str, runtime_arn: str, account: str) -> dict:
    runtime = control.get_agent_runtime(agentRuntimeId=runtime_id)
    if runtime.get("agentRuntimeArn") != runtime_arn:
        raise ValueError("Runtime ARNが指定値と一致しません")
    if runtime.get("status") != "READY":
        raise ValueError(f"Runtime is not READY: {runtime.get('status')}")
    expected_role = f"arn:aws:iam::{account}:role/{ROLE_NAME}"
    if runtime.get("roleArn") != expected_role:
        raise ValueError("Runtime execution roleがS08専用roleと一致しません")
    return runtime


def _versions(control, runtime_id: str) -> list[str]:
    values = control.list_agent_runtime_versions(agentRuntimeId=runtime_id).get("agentRuntimes", [])
    return sorted((str(item.get("agentRuntimeVersion")) for item in values), key=int)


def _endpoint(control, runtime_id: str, endpoint_name: str, expected_version: str) -> dict:
    state = control.get_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName=endpoint_name)
    if state.get("status") != "READY" or str(state.get("liveVersion")) != expected_version:
        raise ValueError(
            f"Endpoint {endpoint_name} is not READY on version {expected_version}; "
            f"status={state.get('status')} liveVersion={state.get('liveVersion')}"
        )
    return state


def _wait_endpoint(control, runtime_id: str, endpoint_name: str, expected_version: str) -> dict:
    deadline = time.monotonic() + WAIT_SECONDS
    started = time.monotonic()
    last: dict = {}
    while time.monotonic() < deadline:
        last = control.get_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName=endpoint_name)
        if last.get("status") == "READY" and str(last.get("liveVersion")) == expected_version:
            return last
        if last.get("status") in {"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED", "DELETING"}:
            break
        print(
            f"Waiting for {endpoint_name}: status={last.get('status')} "
            f"liveVersion={last.get('liveVersion')} targetVersion={last.get('targetVersion')} "
            f"elapsed={int(time.monotonic() - started)}s/{WAIT_SECONDS}s",
            flush=True,
        )
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(POLL_SECONDS, remaining))
    raise RuntimeError(
        f"Endpoint did not reach READY/version {expected_version} within the {WAIT_SECONDS}s poll budget; "
        f"last status={last.get('status')} liveVersion={last.get('liveVersion')}"
    )


def _invoke(session: boto3.Session, runtime_arn: str, endpoint: str, step: str, expected_variant: str) -> dict:
    client = session.client(
        "bedrock-agentcore",
        region_name=REGION,
        config=Config(
            connect_timeout=5,
            read_timeout=45,
            retries={"mode": "standard", "total_max_attempts": 1},
        ),
    )
    started = time.monotonic()
    print(f"\n[{step}] 質問: {QUESTION}", flush=True)
    print(f"送信先: {endpoint}。実際のRuntime処理を待っています…", flush=True)
    result = client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        qualifier=endpoint,
        runtimeSessionId=str(uuid.uuid4()),
        contentType="application/json",
        accept="application/json",
        payload=json.dumps({"prompt": QUESTION}, ensure_ascii=False).encode("utf-8"),
    )
    body = json.loads(result["response"].read())
    elapsed_ms = round((time.monotonic() - started) * 1_000)
    retry_attempts = result["ResponseMetadata"].get("RetryAttempts")
    print(f"HTTP status: {result['ResponseMetadata']['HTTPStatusCode']}", flush=True)
    print(f"SDK retry attempts: {retry_attempts}", flush=True)
    print(f"Runtime variant: {body.get('variant')}", flush=True)
    print(f"Model requests: {body.get('model_requests')}", flush=True)
    print(f"Measured round-trip: {elapsed_ms} ms", flush=True)
    print(f"Token usage: {json.dumps(body.get('usage', {}), ensure_ascii=False)}", flush=True)
    print(f"Request ID: {body.get('request_id')}", flush=True)
    print(f"回答: {body.get('response')}", flush=True)
    if result["ResponseMetadata"]["HTTPStatusCode"] != 200 or retry_attempts != 0:
        raise RuntimeError("Expected one successful SDK request with no retry; stop and inspect before continuing")
    model_requests = body.get("model_requests")
    output_tokens = body.get("usage", {}).get("output_tokens")
    if body.get("variant") != expected_variant:
        raise RuntimeError(f"Expected Runtime variant {expected_variant}, got {body.get('variant')}")
    if not isinstance(model_requests, int) or not 1 <= model_requests <= 2:
        raise RuntimeError(f"Expected 1–2 model requests, got {model_requests}")
    if not isinstance(output_tokens, int) or output_tokens > 512:
        raise RuntimeError(f"Expected at most 512 output tokens per invocation, got {output_tokens}")
    return body


def _update_variant(control, runtime: dict, runtime_id: str, variant: str) -> str:
    environment = dict(runtime.get("environmentVariables", {}))
    if environment.get("S08_PROMPT_VARIANT") != "baseline" or variant != "candidate":
        raise ValueError("Only the recorded baseline-to-candidate update is supported by this lecture")
    environment["S08_PROMPT_VARIANT"] = variant
    request = {
        "agentRuntimeId": runtime_id,
        "agentRuntimeArtifact": runtime["agentRuntimeArtifact"],
        "roleArn": runtime["roleArn"],
        "environmentVariables": environment,
    }
    for key in (
        "authorizerConfiguration", "capacityProviderConfiguration", "description",
        "filesystemConfigurations", "lifecycleConfiguration", "metadataConfiguration",
        "networkConfiguration", "platformVersion", "protocolConfiguration",
        "requestHeaderConfiguration",
    ):
        if runtime.get(key) is not None:
            request[key] = runtime[key]
    response = control.update_agent_runtime(**request)
    version = str(response["agentRuntimeVersion"])
    print(f"UpdateAgentRuntime accepted; new immutable version: V{version}", flush=True)
    return version


def _wait_runtime_ready(control, runtime_id: str, runtime_arn: str, account: str, expected_version: str) -> dict:
    deadline = time.monotonic() + WAIT_SECONDS
    started = time.monotonic()
    last: dict = {}
    while time.monotonic() < deadline:
        last = control.get_agent_runtime(agentRuntimeId=runtime_id)
        if last.get("status") == "READY" and str(last.get("agentRuntimeVersion")) == expected_version:
            if last.get("agentRuntimeArn") != runtime_arn:
                raise ValueError("Runtime ARN changed during version update")
            if last.get("roleArn") != f"arn:aws:iam::{account}:role/{ROLE_NAME}":
                raise ValueError("Runtime execution role changed during update")
            return last
        if last.get("status") in {"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED", "DELETING"}:
            break
        print(f"Waiting for Runtime: status={last.get('status')} version={last.get('agentRuntimeVersion')} "
              f"elapsed={int(time.monotonic() - started)}s/{WAIT_SECONDS}s", flush=True)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(POLL_SECONDS, remaining))
    raise RuntimeError(
        f"Runtime did not reach READY/version {expected_version} within the {WAIT_SECONDS}s poll budget; "
        f"last status={last.get('status')} version={last.get('agentRuntimeVersion')}"
    )


def _route_endpoint(control, runtime_id: str, version: str) -> dict:
    print(f"Routing {ENDPOINT} to V{version}", flush=True)
    control.update_agent_runtime_endpoint(
        agentRuntimeId=runtime_id,
        endpointName=ENDPOINT,
        agentRuntimeVersion=version,
    )
    state = _wait_endpoint(control, runtime_id, ENDPOINT, version)
    print(f"Endpoint is READY; live version V{state.get('liveVersion')}", flush=True)
    return state


def _hold(message: str, seconds: int) -> None:
    print(f"{message} {seconds}秒後に自動で続行します。", flush=True)
    time.sleep(seconds)


def _hold_with_countdown(message: str, seconds: float) -> None:
    """Keep the final result visible while the same window shows real elapsed time."""
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sys.stdout.write(f"\r{message} 残り{math.ceil(remaining)}秒   ")
        sys.stdout.flush()
        time.sleep(min(1.0, remaining))
    print(f"\r{message} 確認時間が終了しました。                 ", flush=True)


def _hold_window_until(started_at: float, minimum_window_seconds: int) -> None:
    remaining = started_at + minimum_window_seconds - time.monotonic()
    if remaining > 0:
        _hold_with_countdown("完了状態を確認できます。", remaining)


def _set_console_title(title: str | None) -> None:
    if title:
        if sys.platform != "win32":
            raise RuntimeError("--capture-console-title はWindowsのコンソールでのみ利用できます")
        ctypes.windll.kernel32.SetConsoleTitleW(title)


def run_compare(session: boto3.Session, runtime_id: str, runtime_arn: str, account: str, advance_seconds: int, final_hold_seconds: int) -> None:
    control = session.client(
        "bedrock-agentcore-control",
        region_name=REGION,
        config=CONTROL_CONFIG,
    )
    runtime = _runtime_state(control, runtime_id, runtime_arn, account)
    if runtime.get("environmentVariables", {}).get("S08_PROMPT_VARIANT") != "baseline":
        raise ValueError("L02 must start from S08_PROMPT_VARIANT=baseline")
    if _versions(control, runtime_id) != ["1"]:
        raise ValueError("L02 requires exactly V1 before the one planned Runtime update; do not rerun creation/update")
    _endpoint(control, runtime_id, ENDPOINT, "1")
    _invoke(session, runtime_arn, ENDPOINT, "L02 V1 baseline", "baseline")
    _hold("V1の回答を確認してください。", advance_seconds)
    new_version = _update_variant(control, runtime, runtime_id, "candidate")
    if new_version != "2":
        raise RuntimeError(f"Expected the first update to create V2, received V{new_version}; stop and inspect")
    _wait_runtime_ready(control, runtime_id, runtime_arn, account, "2")
    _route_endpoint(control, runtime_id, "2")
    _invoke(session, runtime_arn, ENDPOINT, "L02 V2 candidate", "candidate")
    print("L02 comparison complete: one exact question, V1 then V2.", flush=True)
    _hold_with_countdown("完了状態を確認してください。", final_hold_seconds)


def run_rollback(session: boto3.Session, runtime_id: str, runtime_arn: str, account: str, advance_seconds: int, final_hold_seconds: int, adopt_version: str) -> None:
    control = session.client(
        "bedrock-agentcore-control",
        region_name=REGION,
        config=CONTROL_CONFIG,
    )
    runtime = _runtime_state(control, runtime_id, runtime_arn, account)
    if runtime.get("environmentVariables", {}).get("S08_PROMPT_VARIANT") != "candidate":
        raise ValueError("L03 must start after L02 with S08_PROMPT_VARIANT=candidate")
    if _versions(control, runtime_id) != ["1", "2"]:
        raise ValueError("L03 requires exactly V1 and V2; do not create another version")
    _endpoint(control, runtime_id, ENDPOINT, "2")
    _invoke(session, runtime_arn, ENDPOINT, "L03 V2 current", "candidate")
    _hold("V2の回答を確認してください。", advance_seconds)
    _route_endpoint(control, runtime_id, "1")
    _invoke(session, runtime_arn, ENDPOINT, "L03 V1 restored", "baseline")
    _hold("V1の回答を確認してください。", advance_seconds)
    _route_endpoint(control, runtime_id, adopt_version)
    print(f"Decision after observing V2 and V1: s08_training adopts V{adopt_version}; DEFAULT remains V2.", flush=True)
    _hold_with_countdown("完了状態を確認してください。", final_hold_seconds)


def run_offline_probe(display_seconds: int = 0, start_delay_seconds: int = 8) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "s02"))
    import agent_app

    print("LOCAL DATA PREVIEW: AWS Runtimeには接続しません。L02/L03の実行結果ではありません。", flush=True)
    print(f"質問: {QUESTION}", flush=True)
    _hold("質問を確認してください。", start_delay_seconds)
    started = time.monotonic()
    print("教材のrunbookを読み込みます…", flush=True)
    record = agent_app.find_runbook("catalog-api", "5xx")
    elapsed_ms = round((time.monotonic() - started) * 1_000)
    print(f"runbook読込時間: {elapsed_ms} ms", flush=True)
    print("教材データ: " + json.dumps(record, ensure_ascii=False, indent=2), flush=True)
    if display_seconds:
        _hold_with_countdown("結果を確認してください。", display_seconds)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Recordable S08 comparison and rollback runner.")
    parser.add_argument("--expected-account", help="12-digit training AWS account ID")
    parser.add_argument("--runtime-arn", help="Exact S08 Runtime ARN")
    parser.add_argument("--region", default=REGION)
    parser.add_argument("--exercise", choices=("compare", "rollback", "offline-probe"), required=True)
    parser.add_argument("--adopt-version", choices=("1", "2"), help="L03で両版を観察した後、次Section向けendpointに残す版")
    parser.add_argument("--advance-after-seconds", type=int, default=8, help="result確認後の自動進行までの秒数 (1–30)")
    parser.add_argument("--display-seconds", type=int, default=0, help="offline-probe結果の表示保持時間 (0–120)")
    parser.add_argument("--capture-start-delay-seconds", type=int, default=8, help="offline-probeの質問表示後、結果表示までの時間 (1–30)")
    parser.add_argument("--final-hold-seconds", type=int, default=15, help="lecture完了画面の保持時間 (1–120)")
    parser.add_argument("--minimum-window-seconds", type=int, default=0, help="run開始から最低限ウィンドウを保持する秒数 (0–1200)")
    parser.add_argument("--capture-console-title", help="このプロセスのWindowsコンソールへ設定する録画用の一意なtitle")
    args = parser.parse_args()
    if not 1 <= args.advance_after_seconds <= 30:
        parser.error("--advance-after-seconds は1–30です")
    if not 0 <= args.display_seconds <= 120:
        parser.error("--display-seconds は0–120です")
    if not 1 <= args.final_hold_seconds <= 120:
        parser.error("--final-hold-seconds は1–120です")
    if not 0 <= args.minimum_window_seconds <= 1200:
        parser.error("--minimum-window-seconds は0–1200です")
    if not 1 <= args.capture_start_delay_seconds <= 30:
        parser.error("--capture-start-delay-seconds は1–30です")
    _set_console_title(args.capture_console_title)
    if args.exercise == "offline-probe":
        run_offline_probe(args.display_seconds, args.capture_start_delay_seconds)
        return
    if not args.expected_account or not args.runtime_arn:
        parser.error("AWS exercise requires --expected-account and --runtime-arn")
    if args.exercise == "rollback" and args.adopt_version is None:
        parser.error("rollback requires --adopt-version 1 or 2 after the V2/V1 comparison")
    if args.exercise == "compare" and args.adopt_version is not None:
        parser.error("--adopt-version is only available after the L03 comparison")
    run_started = time.monotonic()
    session, account = verify_target(args.expected_account, args.region)
    runtime_id = runtime_id_from_arn(args.runtime_arn, account)
    print(f"AWS account verified: {account}")
    print(f"Region: {REGION}")
    print(f"Exact S08 Runtime: {runtime_id}")
    if args.exercise == "compare":
        run_compare(session, runtime_id, args.runtime_arn, account, args.advance_after_seconds, args.final_hold_seconds)
    else:
        run_rollback(session, runtime_id, args.runtime_arn, account, args.advance_after_seconds, args.final_hold_seconds, args.adopt_version)
    _hold_window_until(run_started, args.minimum_window_seconds)


if __name__ == "__main__":
    main()
