"""Run the standalone Converse and explicit app-tool examples for S02."""

from __future__ import annotations

import argparse
import json
import os

import boto3

from agent_app import DEFAULT_REGION, MODEL_ID, _display_answer, _verified_identity, find_runbook
from converse_steps import call_model_once, read_response, run_tool_cycle


def main() -> None:
    parser = argparse.ArgumentParser(description="Run bounded S02 Converse examples.")
    parser.add_argument("--mode", choices=("plain", "tool-cycle"), required=True)
    parser.add_argument("--expected-account", required=True,
                        help="12-digit account ID for the learning account you intend to use")
    args = parser.parse_args()
    region = os.environ.get("AWS_DEFAULT_REGION", DEFAULT_REGION)

    # Use the same target checks as the Strands app before creating a Bedrock client.
    identity = _verified_identity(region, args.expected_account)
    client = boto3.Session(region_name=region).client("bedrock-runtime")
    print(f"AWS account: {identity['Account']} (expected account matched)")
    print(f"Region: {region}; Bedrock model: {MODEL_ID}")

    if args.mode == "plain":
        response = call_model_once(client, MODEL_ID, "Reply with exactly OK.", max_tokens=32)
        result = read_response(response)
        result["text"] = _display_answer(result["text"])
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    result = run_tool_cycle(
        client,
        MODEL_ID,
        "catalog-api の5xxを調べる順序を、教材runbookから確認してください。",
        lambda service, topic: find_runbook(service=service, topic=topic),
        max_tokens=256,
    )
    response_summary = read_response(result["response"])
    response_summary["text"] = _display_answer(response_summary["text"])
    print(json.dumps({
        "initial_stop_reason": result.get("initial_stop_reason"),
        "final_stop_reason": result.get("final_stop_reason"),
        "tool_calls": result["tool_calls"],
        "final_response": response_summary,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
