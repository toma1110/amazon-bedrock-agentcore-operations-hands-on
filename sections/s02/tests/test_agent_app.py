import sys
from pathlib import Path

from strands import Agent
from strands.handlers.callback_handler import null_callback_handler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_app
from agent_app import _display_answer, find_runbook


def test_exact_service_and_topic_returns_the_matching_synthetic_source():
    result = find_runbook("catalog-api", "5xx")

    assert result["found"] is True
    assert result["service"] == "catalog-api"
    assert result["topic"] == "5xx"
    assert result["source"] == "sections/s02/runbooks.json"
    assert result["checks"]
    assert "合成教材データ" in result["note"]


def test_unknown_pair_returns_not_found_without_searching_elsewhere():
    result = find_runbook("unknown-service", "5xx")

    assert result["found"] is False
    assert result["service"] == "unknown-service"
    assert result["topic"] == "5xx"
    assert result["source"] == "sections/s02/runbooks.json"


def test_known_keys_with_an_unsupported_pair_do_not_return_a_record():
    result = find_runbook("catalog-api", "throttle")

    assert result["found"] is False
    assert result["service"] == "catalog-api"
    assert result["topic"] == "throttle"
    assert result["source"] == "sections/s02/runbooks.json"


def test_display_answer_hides_provider_thinking_block():
    result = _display_answer(
        "<thinking>表示対象外の教材用テキスト</thinking>回答本文"
    )

    assert result == "回答本文"
    assert "<thinking>" not in result


def test_display_answer_preserves_normal_answer_without_thinking_block():
    result = _display_answer(
        "回答本文です。"
    )

    assert result == "回答本文です。"


def test_display_answer_keeps_tagless_provider_answer_unchanged():
    result = _display_answer("確認手順は3点です。")

    assert result == "確認手順は3点です。"


def test_make_agent_returns_constructed_agent_with_nonprinting_callback(monkeypatch):
    class OfflineModel:
        stateful = False

    offline_model = OfflineModel()
    monkeypatch.setattr(agent_app, "BedrockModel", lambda **kwargs: offline_model)

    result = agent_app._make_agent("ap-northeast-1")

    assert isinstance(result, Agent)
    assert result.model is offline_model
    assert result.callback_handler is null_callback_handler
