from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

S05_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(S05_DIR))

import memory_lab as lab


ACCOUNT = "111122223333"
ROLE_A = f"arn:aws:iam::{ACCOUNT}:role/udemy-agentcore-s05-learner-a"
ROLE_B = f"arn:aws:iam::{ACCOUNT}:role/udemy-agentcore-s05-learner-b"
ADMIN = f"arn:aws:iam::{ACCOUNT}:role/udemy-agentcore-s05-exercise-admin"
MEMORY_ID = "s05-memory-abc1234567"


def role_args(**extra):
    return SimpleNamespace(
        profile=None,
        region=lab.REGION,
        expected_account=ACCOUNT,
        actor_a_role_arn=ROLE_A,
        actor_b_role_arn=ROLE_B,
        admin_role_arn=ADMIN,
        memory_id=MEMORY_ID,
        strategy_id="s05-preference-strategy",
        **extra,
    )


def assumed(role: str, session_name: str = "lab-session") -> str:
    role_name = role.rsplit("/", 1)[-1]
    return f"arn:aws:sts::{ACCOUNT}:assumed-role/{role_name}/{session_name}"


@pytest.mark.parametrize(
    ("arn", "expected"),
    [
        (assumed(ROLE_A), ROLE_A),
        (assumed(ROLE_B, "different-session"), ROLE_B),
    ],
)
def test_assumed_role_arn_maps_to_exact_iam_role(arn, expected):
    assert lab._assumed_role_arn(arn, ACCOUNT) == expected


@pytest.mark.parametrize(
    "arn",
    [
        "arn:aws:iam::111122223333:user/student",
        "arn:aws:sts::999999999999:assumed-role/udemy-agentcore-s05-learner-a/lab-session",
        "arn:aws:sts::111122223333:assumed-role/udemy-agentcore-s05-learner-a",
        "arn:aws:sts::111122223333:assumed-role/path/udemy-agentcore-s05-learner-a/lab-session",
    ],
)
def test_assumed_role_mapping_rejects_non_exact_or_unexpected_principal(arn):
    with pytest.raises(ValueError):
        lab._assumed_role_arn(arn, ACCOUNT)


class FakeSts:
    def __init__(self, arn=assumed(ROLE_A), account=ACCOUNT):
        self.arn = arn
        self.account = account

    def get_caller_identity(self):
        return {"Account": self.account, "Arn": self.arn}


class FakeSession:
    def __init__(self, arn=assumed(ROLE_A), account=ACCOUNT):
        self.sts = FakeSts(arn, account)

    def client(self, name):
        assert name == "sts"
        return self.sts


def test_identity_derives_actor_from_sts_role_not_cli_actor_value():
    actor, identity = lab._expected_identity(FakeSession(), ACCOUNT, ROLE_A, ROLE_B)
    assert actor == "learner-a"
    assert identity["Arn"] == assumed(ROLE_A)


def test_identity_rejects_unmapped_role_and_wrong_account():
    with pytest.raises(ValueError, match="not one of the two"):
        lab._expected_identity(FakeSession(assumed(ADMIN)), ACCOUNT, ROLE_A, ROLE_B)
    with pytest.raises(ValueError, match="differs"):
        lab._expected_identity(FakeSession(account="999999999999"), ACCOUNT, ROLE_A, ROLE_B)


def test_region_and_memory_id_are_bounded():
    with pytest.raises(ValueError, match="limited"):
        lab._session(None, "us-east-1")
    with pytest.raises(ValueError, match="exact S05"):
        lab._validate_memory_id("some-other-memory-id")


def test_ask_cli_requires_and_parses_exact_memory_id_before_agent_execution(monkeypatch):
    captured = []
    monkeypatch.setattr(lab, "ask_agent", lambda args: captured.append(args))
    monkeypatch.setattr(sys, "argv", [
        "memory_lab.py", "ask", "--expected-account", ACCOUNT,
        "--actor-a-role-arn", ROLE_A, "--actor-b-role-arn", ROLE_B,
        "--memory-id", MEMORY_ID, "--strategy-id", "s05-preference-strategy",
        "--memory-mode", "off", "--gateway-endpoint",
        "https://example.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com/mcp",
        "--question", "Summarize the supplied UTC interval.",
    ])

    lab.main()

    assert len(captured) == 1
    assert captured[0].memory_id == MEMORY_ID
    assert captured[0].strategy_id == "s05-preference-strategy"


def test_ask_cli_rejects_missing_memory_id_before_agent_execution(monkeypatch):
    called = []
    monkeypatch.setattr(lab, "ask_agent", lambda _args: called.append(True))
    monkeypatch.setattr(sys, "argv", [
        "memory_lab.py", "ask", "--expected-account", ACCOUNT,
        "--actor-a-role-arn", ROLE_A, "--actor-b-role-arn", ROLE_B,
        "--strategy-id", "s05-preference-strategy", "--memory-mode", "off",
        "--gateway-endpoint",
        "https://example.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com/mcp",
        "--question", "Summarize the supplied UTC interval.",
    ])

    with pytest.raises(SystemExit) as error:
        lab.main()

    assert error.value.code == 2
    assert called == []


def test_access_denied_probe_requires_expected_aws_denial():
    exc = ClientError({"Error": {"Code": "AccessDeniedException"}}, "ListEvents")
    lab._expect_access_denied(lambda: (_ for _ in ()).throw(exc), "test probe")
    with pytest.raises(RuntimeError, match="unexpectedly succeeded"):
        lab._expect_access_denied(lambda: None, "test probe")
    other = ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "ListEvents")
    with pytest.raises(RuntimeError, match="expected AccessDenied"):
        lab._expect_access_denied(lambda: (_ for _ in ()).throw(other), "test probe")


def test_save_event_uses_only_sts_derived_actor_and_fixed_session(monkeypatch, capsys):
    calls = []

    class Memory:
        def create_event(self, **kwargs):
            calls.append(kwargs)
            return {"event": {"actorId": "learner-a", "sessionId": lab.SESSION_ID,
                              "eventId": "100#abc"}}

    class Session:
        def client(self, name):
            assert name == "bedrock-agentcore"
            return Memory()

    monkeypatch.setattr(lab, "_checked_session", lambda _args: (Session(), "learner-a", {"Account": ACCOUNT}))
    lab.save_event(role_args())
    assert len(calls) == 1
    assert calls[0]["actorId"] == "learner-a"
    assert calls[0]["sessionId"] == lab.SESSION_ID
    assert "dashboard preference" in calls[0]["payload"][0]["conversational"]["content"]["text"]
    assert '"event_id": "100#abc"' in capsys.readouterr().out


def test_read_records_uses_own_actor_namespace_and_never_gets_record_by_id(monkeypatch, capsys):
    calls = []

    class Memory:
        def retrieve_memory_records(self, **kwargs):
            calls.append(kwargs)
            return {"memoryRecordSummaries": [{"memoryRecordId": "mem-demo"}]}

    class Session:
        def client(self, name):
            assert name == "bedrock-agentcore"
            return Memory()

    monkeypatch.setattr(lab, "_checked_session", lambda _args: (Session(), "learner-b", {"Account": ACCOUNT}))
    lab.read_records(role_args())
    assert calls[0]["namespace"] == "/actors/learner-b/"
    assert calls[0]["memoryId"] == MEMORY_ID
    output = capsys.readouterr().out
    assert '"namespace": "/actors/learner-b/"' in output


def _write_state(path, *, event_a="event-a", event_b="event-b", record_a="record-a", record_b="record-b"):
    path.write_text(json.dumps({
        "account_id": ACCOUNT,
        "region": lab.REGION,
        "memory_id": MEMORY_ID,
        "event_ids": {"learner-a": event_a, "learner-b": event_b},
        "record_ids": {"learner-a": record_a, "learner-b": record_b},
    }), encoding="utf-8")


def test_admin_delete_record_requires_admin_and_uses_exact_actor_namespace(monkeypatch, capsys, tmp_path):
    calls = []
    state_file = tmp_path / "s05-state.json"
    _write_state(state_file)
    monkeypatch.setattr(lab, "STATE_FILE", state_file)

    class Memory:
        def delete_memory_record(self, **kwargs):
            calls.append(kwargs)
            return {"memoryRecordId": kwargs["memoryRecordId"]}

    class Session:
        def client(self, name):
            assert name == "bedrock-agentcore"
            return Memory()

    monkeypatch.setattr(lab, "_admin_session", lambda _args: Session())
    args = role_args(owner="b", record_id="record-b")
    lab.admin_delete_record(args)
    assert calls == [{"memoryId": MEMORY_ID, "memoryRecordId": "record-b",
                      "namespace": "/actors/learner-b/"}]
    assert '"namespace": "/actors/learner-b/"' in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["event", "record"])
def test_admin_delete_rejects_unknown_or_wrong_owner_id_before_any_api_call(monkeypatch, tmp_path, kind):
    state_file = tmp_path / "s05-state.json"
    _write_state(state_file)
    monkeypatch.setattr(lab, "STATE_FILE", state_file)
    monkeypatch.setattr(lab, "_admin_session", lambda _args: pytest.fail("must reject before AWS calls"))
    args = role_args(owner="a", event_id="event-b", record_id="record-b")
    delete = lab.admin_delete_event if kind == "event" else lab.admin_delete_record
    with pytest.raises(ValueError, match="not the exact saved ID"):
        delete(args)


@pytest.mark.parametrize(
    ("state_values", "expected_error"),
    [
        (None, "Cannot read private S05 state"),
        ("not-json", "Cannot read private S05 state"),
        ({"account_id": "999900001111"}, "does not match the requested account"),
        ({"region": "us-east-1"}, "does not match the requested account"),
        ({"memory_id": "another-memory"}, "does not match the requested account"),
    ],
)
def test_admin_delete_rejects_missing_or_mismatched_state_before_any_api_call(
    monkeypatch, tmp_path, state_values, expected_error
):
    state_file = tmp_path / "s05-state.json"
    if state_values == "not-json":
        state_file.write_text("{broken", encoding="utf-8")
    elif state_values is not None:
        state = {"account_id": ACCOUNT, "region": lab.REGION, "memory_id": MEMORY_ID,
                 "event_ids": {"learner-a": "event-a"}}
        state.update(state_values)
        state_file.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(lab, "STATE_FILE", state_file)
    monkeypatch.setattr(lab, "_admin_session", lambda _args: pytest.fail("must reject before AWS calls"))
    args = role_args(owner="a", event_id="event-a")
    with pytest.raises(ValueError, match=expected_error):
        lab.admin_delete_event(args)


def test_admin_delete_event_uses_state_bound_exact_event_id(monkeypatch, tmp_path, capsys):
    calls = []
    state_file = tmp_path / "s05-state.json"
    _write_state(state_file)
    monkeypatch.setattr(lab, "STATE_FILE", state_file)

    class Memory:
        def delete_event(self, **kwargs):
            calls.append(kwargs)
            return {"eventId": kwargs["eventId"]}

    class Session:
        def client(self, name):
            assert name == "bedrock-agentcore"
            return Memory()

    monkeypatch.setattr(lab, "_admin_session", lambda _args: Session())
    args = role_args(owner="a", event_id="event-a")
    lab.admin_delete_event(args)
    assert calls == [{"memoryId": MEMORY_ID, "actorId": "learner-a",
                      "sessionId": lab.SESSION_ID, "eventId": "event-a"}]
    assert '"deleted_event_id": "event-a"' in capsys.readouterr().out


def test_strategy_has_single_actor_namespace_and_finite_ttl():
    strategy = lab.STRATEGY["userPreferenceMemoryStrategy"]
    assert strategy["namespaceTemplates"] == ["/actors/{actorId}/"]
    assert "recordExpirySpecification" not in strategy
    assert lab.SESSION_ID == "s05-course-demo"


def test_create_memory_rejects_name_collision_before_any_write(monkeypatch):
    class Control:
        created = False

        def list_memories(self, **kwargs):
            return {"memories": [{"id": MEMORY_ID}]}

        def get_memory(self, **kwargs):
            return {"memory": {"name": lab.MEMORY_NAME}}

        def create_memory(self, **kwargs):
            self.created = True
            raise AssertionError("must not create over a colliding name")

    control = Control()

    class Session:
        def client(self, name):
            return SimpleNamespace(get_caller_identity=lambda: {
                "Account": ACCOUNT, "Arn": assumed(ADMIN)
            }) if name == "sts" else control

    monkeypatch.setattr(lab, "_session", lambda *_args, **_kwargs: Session())
    args = SimpleNamespace(profile=None, region=lab.REGION, expected_account=ACCOUNT,
                           admin_role_arn=ADMIN, confirm_create=True)
    with pytest.raises(RuntimeError, match="already exists"):
        lab.create_memory(args)
    assert control.created is False


def test_create_memory_uses_bounded_name_ttl_and_required_tags(monkeypatch, capsys):
    calls = []

    class Control:
        def list_memories(self, **kwargs):
            return {"memories": []}

        def create_memory(self, **kwargs):
            calls.append(kwargs)
            return {"memory": {"id": MEMORY_ID, "arn": f"arn:aws:bedrock-agentcore:{lab.REGION}:{ACCOUNT}:memory/{MEMORY_ID}",
                                "name": lab.MEMORY_NAME, "status": "CREATING"}}

    control = Control()

    class Session:
        def client(self, name):
            return SimpleNamespace(get_caller_identity=lambda: {
                "Account": ACCOUNT, "Arn": assumed(ADMIN)
            }) if name == "sts" else control

    monkeypatch.setattr(lab, "_session", lambda *_args, **_kwargs: Session())
    args = SimpleNamespace(profile=None, region=lab.REGION, expected_account=ACCOUNT,
                           admin_role_arn=ADMIN, confirm_create=True)
    lab.create_memory(args)
    assert len(calls) == 1
    assert calls[0]["name"] == "udemy_agentcore_s05_memory"
    assert calls[0]["eventExpiryDuration"] == 3
    assert calls[0]["tags"] == {"Course": "udemy-agentcore-s05", "Owner": "course-lab"}
    assert "recordExpirySpecification" not in calls[0]["memoryStrategies"][0]["userPreferenceMemoryStrategy"]
    assert '"status": "CREATING"' in capsys.readouterr().out


def test_create_memory_stops_at_documented_default_ceiling_without_treating_it_as_live_quota(monkeypatch):
    writes = []

    class Control:
        def list_memories(self, **kwargs):
            return {"memories": [{"id": f"existing-{index}"} for index in range(150)]}

        def get_memory(self, memoryId):
            return {"memory": {"name": f"unrelated-{memoryId}"}}

        def create_memory(self, **kwargs):
            writes.append(kwargs)
            raise AssertionError("create must not be called at the documented ceiling")

    control = Control()
    class Session:
        def client(self, name):
            return SimpleNamespace(get_caller_identity=lambda: {
                "Account": ACCOUNT, "Arn": assumed(ADMIN)
            }) if name == "sts" else control

    monkeypatch.setattr(lab, "_session", lambda *_args, **_kwargs: Session())
    args = SimpleNamespace(profile=None, region=lab.REGION, expected_account=ACCOUNT,
                           admin_role_arn=ADMIN, confirm_create=True)
    with pytest.raises(RuntimeError, match="documented default quota of 150"):
        lab.create_memory(args)
    assert writes == []


def test_create_memory_quota_error_is_a_stop_not_a_retry_or_increase(monkeypatch):
    class Control:
        def list_memories(self, **kwargs):
            return {"memories": []}

        def create_memory(self, **kwargs):
            raise ClientError({"Error": {"Code": "ServiceQuotaExceededException"}}, "CreateMemory")

    control = Control()
    class Session:
        def client(self, name):
            return SimpleNamespace(get_caller_identity=lambda: {
                "Account": ACCOUNT, "Arn": assumed(ADMIN)
            }) if name == "sts" else control

    monkeypatch.setattr(lab, "_session", lambda *_args, **_kwargs: Session())
    args = SimpleNamespace(profile=None, region=lab.REGION, expected_account=ACCOUNT,
                           admin_role_arn=ADMIN, confirm_create=True)
    with pytest.raises(RuntimeError, match="Stop; do not request an increase"):
        lab.create_memory(args)


@pytest.mark.parametrize("mode", ["off", "on"])
def test_ask_agent_has_invocation_local_trace_and_shows_gateway_input_and_result(
    mode, monkeypatch, capsys
):
    import os

    tool_modules = {}
    proxy_root = types.ModuleType("mcp_proxy_for_aws")
    proxy_client = types.ModuleType("mcp_proxy_for_aws.client")
    proxy_client.aws_iam_streamablehttp_client = lambda **kwargs: kwargs
    strands_tools = types.ModuleType("strands.tools")
    strands_mcp = types.ModuleType("strands.tools.mcp")
    strands_mcp_client = types.ModuleType("strands.tools.mcp.mcp_client")

    class FakeMCP:
        def __init__(self, _factory):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def call_tool_sync(self, **kwargs):
            tool_modules["gateway_call"] = kwargs
            return {"status": "success", "content": [{"text": '{"source":{"service":"catalog-api","log_group":"/aws/lambda/s04-synthetic"},"events":[{"timestamp":"2026-10-16T01:00:00Z","message":"synthetic 5xx"}]}'}]}

    strands_mcp_client.MCPClient = FakeMCP
    monkeypatch.setitem(sys.modules, "mcp_proxy_for_aws", proxy_root)
    monkeypatch.setitem(sys.modules, "mcp_proxy_for_aws.client", proxy_client)
    monkeypatch.setitem(sys.modules, "strands.tools", strands_tools)
    monkeypatch.setitem(sys.modules, "strands.tools.mcp", strands_mcp)
    monkeypatch.setitem(sys.modules, "strands.tools.mcp.mcp_client", strands_mcp_client)

    import agent_app

    class FakeUsage:
        accumulated_usage = {"inputTokens": 10, "outputTokens": 5}

    class FakeResult:
        metrics = FakeUsage()

    class FakeAgent:
        def __init__(self, tools):
            self.tools = tools

        def __call__(self, *_args, **_kwargs):
            self.tools[0]("2026-10-16T01:00:00Z", "2026-10-16T02:00:00Z", "logs")
            if mode == "on":
                self.tools[1]()
            return FakeResult()

    def fake_make_agent(*_args, tools, **_kwargs):
        tool_modules["agent_tools"] = tools
        return FakeAgent(tools)

    monkeypatch.setattr(agent_app, "_make_agent", fake_make_agent)
    monkeypatch.setattr(agent_app, "_display_answer", lambda _result: "answer")
    monkeypatch.setattr(lab, "_checked_session", lambda _args: (
        SimpleNamespace(), "learner-a", {"Account": ACCOUNT, "Arn": assumed(ROLE_A)}
    ))
    memory_calls = []
    monkeypatch.setattr(lab, "list_records", lambda _args: memory_calls.append(True) or [
        {"memoryRecordId": "pref-a", "content": "UTC timestamps first"}
    ])
    args = role_args(memory_mode=mode, question="Show logs between 1 and 2 UTC",
                     gateway_endpoint="https://example.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com/mcp")

    lab.ask_agent(args)
    output = capsys.readouterr().out
    assert '"start_time": "2026-10-16T01:00:00Z"' in output
    assert '"end_time": "2026-10-16T02:00:00Z"' in output
    assert '"result_summary": "{\\"source\\"' in output
    assert len(tool_modules["agent_tools"]) == (1 if mode == "off" else 2)
    assert len(memory_calls) == (0 if mode == "off" else 1)
    assert "_tool_trace" not in lab.__dict__
