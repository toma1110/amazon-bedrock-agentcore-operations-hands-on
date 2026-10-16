from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

S03_DIR = Path(__file__).resolve().parents[1]
S02_DIR = S03_DIR.parent / "s02"
sys.path.insert(0, str(S03_DIR))
sys.path.insert(0, str(S02_DIR))

import agent_app
import gateway_tools


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.lock = Lock()

    def call_tool_sync(self, **kwargs: object) -> dict[str, object]:
        with self.lock:
            self.calls.append(kwargs)
        return {"status": "success", "content": [{"text": json.dumps({"ok": True, "data": {}})}]}


def _call(tool: object) -> object:
    return tool(
        data_type="logs",
        service="catalog-api",
        start_time="2026-10-16T00:00:00Z",
        end_time="2026-10-16T00:01:00Z",
        max_results=1,
    )


def test_gateway_tool_rejects_calls_after_four_before_network_request():
    client = FakeClient()
    reader = gateway_tools.create_reader_tool(client, "target___read_synthetic_cloudwatch")
    gateway_tools.reset_remote_call_budget()
    agent_app._tool_trace.clear()

    for _ in range(7):
        _call(reader)

    assert len(client.calls) == gateway_tools.MAX_REMOTE_CALLS_PER_INVOCATION == 4
    assert sum(
        item["result"].get("error") == "S04 Gateway call limit reached for this Runtime invocation"
        for item in agent_app._tool_trace
    ) == 3


def test_gateway_tool_budget_is_atomic_for_parallel_tool_use_requests():
    client = FakeClient()
    reader = gateway_tools.create_reader_tool(client, "target___read_synthetic_cloudwatch")
    gateway_tools.reset_remote_call_budget()
    agent_app._tool_trace.clear()

    with ThreadPoolExecutor(max_workers=12) as executor:
        list(executor.map(lambda _: _call(reader), range(12)))

    assert len(client.calls) == gateway_tools.MAX_REMOTE_CALLS_PER_INVOCATION == 4


def test_gateway_budget_resets_for_the_next_runtime_invocation():
    client = FakeClient()
    reader = gateway_tools.create_reader_tool(client, "target___read_synthetic_cloudwatch")
    gateway_tools.reset_remote_call_budget()
    for _ in range(4):
        _call(reader)

    gateway_tools.reset_remote_call_budget()
    for _ in range(4):
        _call(reader)

    assert len(client.calls) == 8
