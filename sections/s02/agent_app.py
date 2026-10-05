"""A single Strands agent with a bounded, read-only synthetic runbook tool."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

import boto3
from strands import Agent, tool
from strands.models import BedrockModel

SECTION_DIR = Path(__file__).resolve().parent
RUNBOOKS_PATH = SECTION_DIR / "runbooks.json"
DEFAULT_REGION = "ap-northeast-1"
MODEL_ID = "amazon.nova-lite-v1:0"
MAX_OUTPUT_TOKENS = 512
MAX_AGENT_TURNS = 4
MAX_TOTAL_OUTPUT_TOKENS = 2_048
MAX_TOTAL_TOKENS = 6_000

SYSTEM_PROMPT = """あなたは教材用の運用調査アシスタントです。
渡された質問に答えるために必要な場合だけ lookup_runbook ツールを使ってください。
ツールを使う前に service と topic の両方が質問に明示されているか確認します。
どちらかが分からない場合、値を推測したりツールを呼んだりせず、不足している情報を質問してください。
ツール結果は合成教材データです。実際のAWSリソースや障害を観測したものとして説明しません。
ツール結果にあるのは調査時の確認順序だけです。各確認の結果や実システムの状態は分かりません。
確認順序を示すことは、原因の特定・復旧・適切な対応を保証するものではありません。
取得データにない原因、確認結果、復旧方法を補って断定せず、分からないことは明示してください。
回答では確認できた内容と、まだ確認できない原因を分けて説明してください。
"""

_tool_trace: list[dict[str, Any]] = []


def _load_runbooks() -> list[dict[str, Any]]:
    return json.loads(RUNBOOKS_PATH.read_text(encoding="utf-8"))


def find_runbook(service: str, topic: str) -> dict[str, Any]:
    """Return one exact service/topic match from the fixed local sample."""
    runbooks = _load_runbooks()
    allowed_pairs = {(item["service"], item["topic"]) for item in runbooks}
    if (service, topic) not in allowed_pairs:
        return {
            "found": False,
            "service": service,
            "topic": topic,
            "source": "sections/s02/runbooks.json",
            "message": "この組み合わせに一致する教材データはありません。",
        }
    for runbook in runbooks:
        if runbook["service"] == service and runbook["topic"] == topic:
            return {"found": True, **runbook}
    return {
        "found": False,
        "service": service,
        "topic": topic,
        "source": "sections/s02/runbooks.json",
        "message": "この組み合わせに一致する教材データはありません。",
    }


@tool
def lookup_runbook(service: str, topic: str) -> str:
    """Look up a fixed runbook by exact service and topic identifiers.

    Use only when the question explicitly provides both identifiers. This tool
    reads one bundled JSON file and cannot access AWS APIs, the network, or files
    outside that file.

    Args:
        service: Exact service key, such as catalog-api or report-worker.
        topic: Exact topic key, such as 5xx or throttle.
    """
    record = find_runbook(service=service, topic=topic)
    _tool_trace.append({"arguments": {"service": service, "topic": topic}, "result": record})
    return json.dumps(record, ensure_ascii=False)


def _make_agent(region: str) -> Agent:
    model = BedrockModel(
        model_id=MODEL_ID,
        region_name=region,
        temperature=0,
        max_tokens=MAX_OUTPUT_TOKENS,
    )
    return Agent(
        model=model,
        tools=[lookup_runbook],
        system_prompt=SYSTEM_PROMPT,
        callback_handler=None,
    )


def _display_answer(result: object) -> str:
    """Hide provider-emitted thinking blocks from the learner-facing output."""
    return re.sub(
        r"<thinking\b[^>]*>.*?</thinking>",
        "",
        str(result),
        flags=re.DOTALL | re.IGNORECASE,
    ).strip()


def run_question(question: str, region: str) -> None:
    if not question.strip() or len(question) > 2_000:
        raise ValueError("質問は1〜2,000文字で入力してください。")
    _tool_trace.clear()
    session = boto3.Session(region_name=region)
    identity = session.client("sts").get_caller_identity()
    print(f"AWS account: {identity['Account']}")
    print(f"Region: {region}")
    print(f"Bedrock model: {MODEL_ID}")
    print(f"Question: {question}")

    result = _make_agent(region)(
        question,
        limits={
            "turns": MAX_AGENT_TURNS,
            "output_tokens": MAX_TOTAL_OUTPUT_TOKENS,
            "total_tokens": MAX_TOTAL_TOKENS,
        },
    )
    usage = result.metrics.accumulated_usage
    print(
        "\nToken usage (Strands agent invocation): "
        f"input={usage.get('inputTokens')}, "
        f"output={usage.get('outputTokens')}, "
        f"total={usage.get('totalTokens')}"
    )
    print("\nTool calls and returned data:")
    print(json.dumps(_tool_trace, ensure_ascii=False, indent=2) if _tool_trace else "(no tool call)")
    print("\nAgent answer:")
    print(_display_answer(result))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the S02 local read-only agent.")
    parser.add_argument("--question", help="Question to send to the agent")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run one complete question and one question that lacks required details",
    )
    args = parser.parse_args()
    region = os.environ.get("AWS_DEFAULT_REGION", DEFAULT_REGION)

    if args.demo:
        run_question("catalog-api の5xxを調べる順序を教えてください。", region)
        print("\n" + "=" * 72 + "\n")
        run_question("エラーが出ています。どの手順で調べればよいですか？", region)
    elif args.question:
        run_question(args.question, region)
    else:
        parser.error("--question または --demo を指定してください")


if __name__ == "__main__":
    main()
