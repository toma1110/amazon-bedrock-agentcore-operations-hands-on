"""Local S05 AgentCore Memory exercise with caller-derived actor identities.

The lab uses short-term events and a User Preference strategy. It deliberately
does not invoke or update the S03/S04 AgentCore Runtime.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

REGION = "ap-northeast-1"
MEMORY_NAME = "udemy_agentcore_s05_memory"
DOCUMENTED_DEFAULT_MEMORY_QUOTA = 150
STATE_FILE = Path(__file__).resolve().parents[2] / ".local" / "s05-state.json"
STRATEGY_NAME = "S05UserPreferences"
SESSION_ID = "s05-course-demo"
ACTORS = {"a": "learner-a", "b": "learner-b"}
STRATEGY = {
    "userPreferenceMemoryStrategy": {
        "name": STRATEGY_NAME,
        "description": "Extract non-sensitive synthetic display preferences for the S05 lab.",
        "namespaceTemplates": ["/actors/{actorId}/"],
    }
}


def _session(profile: str | None, region: str) -> boto3.Session:
    if region != REGION:
        raise ValueError(f"This exercise is limited to {REGION}.")
    return boto3.Session(profile_name=profile, region_name=region)


def _expected_identity(
    session: boto3.Session,
    expected_account: str,
    actor_a_role_arn: str,
    actor_b_role_arn: str,
) -> tuple[str, dict[str, str]]:
    if not re.fullmatch(r"\d{12}", expected_account):
        raise ValueError("Expected account must be a 12-digit learning-account ID.")
    identity = session.client("sts").get_caller_identity()
    if identity.get("Account") != expected_account:
        raise ValueError("Caller account differs from the expected learning account.")
    role_arn = _assumed_role_arn(identity.get("Arn", ""), expected_account)
    roles = {actor_a_role_arn: ACTORS["a"], actor_b_role_arn: ACTORS["b"]}
    actor = roles.get(role_arn)
    if actor is None:
        raise ValueError("Caller is not one of the two configured S05 learner roles.")
    return actor, identity


def _assumed_role_arn(caller_arn: str, expected_account: str) -> str:
    """Convert an STS assumed-role ARN to its exact IAM role ARN (no role paths)."""
    match = re.fullmatch(
        rf"arn:aws:sts::{re.escape(expected_account)}:assumed-role/([^/]+)/[^/]+",
        caller_arn,
    )
    if not match:
        raise ValueError("Use temporary credentials for an assumed S05 IAM role.")
    role_name = match.group(1)
    return f"arn:aws:iam::{expected_account}:role/{role_name}"


def _checked_session(args: argparse.Namespace) -> tuple[boto3.Session, str, dict[str, str]]:
    session = _session(args.profile, args.region)
    actor, identity = _expected_identity(
        session, args.expected_account, args.actor_a_role_arn, args.actor_b_role_arn
    )
    return session, actor, identity


def _validate_memory_id(memory_id: str) -> None:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,99}-[A-Za-z0-9]{10}", memory_id):
        raise ValueError("Supply the exact S05 AgentCore Memory ID from setup.")


def whoami(args: argparse.Namespace) -> None:
    session, actor, identity = _checked_session(args)
    del session
    print(json.dumps({"account": identity["Account"], "role": identity["Arn"], "actor": actor}, indent=2))


def save_event(args: argparse.Namespace) -> None:
    session, actor, identity = _checked_session(args)
    _validate_memory_id(args.memory_id)
    result = session.client("bedrock-agentcore").create_event(
        memoryId=args.memory_id,
        actorId=actor,
        sessionId=SESSION_ID,
        eventTimestamp=datetime.now(timezone.utc),
        payload=[{
            "conversational": {
                "role": "USER",
                "content": {"text": "Remember my dashboard preference: show UTC timestamps before the affected service."},
            }
        }],
    )
    event = result.get("event", {})
    if event.get("actorId") != actor or event.get("sessionId") != SESSION_ID or not event.get("eventId"):
        raise RuntimeError("CreateEvent returned an unexpected actor, session, or event ID.")
    print(json.dumps({"account": identity["Account"], "actor": actor, "session_id": SESSION_ID,
                      "event_id": event["eventId"], "status": "created"}, indent=2))


def list_events(args: argparse.Namespace, *, actor: str | None = None) -> list[dict[str, Any]]:
    session, own_actor, _ = _checked_session(args)
    _validate_memory_id(args.memory_id)
    requested_actor = actor or own_actor
    result = session.client("bedrock-agentcore").list_events(
        memoryId=args.memory_id,
        actorId=requested_actor,
        sessionId=SESSION_ID,
        includePayloads=True,
        maxResults=20,
    )
    return result.get("events", [])


def read_events(args: argparse.Namespace) -> None:
    _, actor, _ = _checked_session(args)
    events = list_events(args)
    print(json.dumps({"actor": actor, "session_id": SESSION_ID,
                      "events": events}, default=str, ensure_ascii=False, indent=2))


def list_records(args: argparse.Namespace, *, namespace: str | None = None) -> list[dict[str, Any]]:
    session, own_actor, _ = _checked_session(args)
    _validate_memory_id(args.memory_id)
    requested_namespace = namespace or f"/actors/{own_actor}/"
    result = session.client("bedrock-agentcore").retrieve_memory_records(
        memoryId=args.memory_id,
        namespace=requested_namespace,
        maxResults=5,
        searchCriteria={"memoryStrategyId": args.strategy_id,
                        "searchQuery": "dashboard display preference UTC timestamps", "topK": 5},
    )
    return result.get("memoryRecordSummaries", [])


def read_records(args: argparse.Namespace) -> None:
    session, actor, _ = _checked_session(args)
    records = list_records(args)
    print(json.dumps({"actor": actor, "namespace": f"/actors/{actor}/", "records": records},
                     default=str, ensure_ascii=False, indent=2))


def _validate_invocation_id(value: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}", value):
        raise ValueError("Target invocation ID must be a UUID copied from the S03 logs.")
    return value


def _validate_timestamp_ms(value: str) -> int:
    if not re.fullmatch(r"\d{13}", str(value)):
        raise ValueError("Target timestamp must be the exact 13-digit Unix millisecond value from S03 logs.")
    timestamp = int(value)
    now_ms = int(time.time() * 1000)
    if timestamp > now_ms or timestamp < now_ms - 7 * 24 * 60 * 60 * 1000:
        raise ValueError("Target S03 invocation must be from the last 7 days and not in the future.")
    return timestamp


def _bound_gateway_config(endpoint: str, tool_name: str) -> None:
    """Compare CLI values with the learner's private S04/S05 state before AWS/model calls.

    This local file is a consistency check, not a security boundary; IAM on the
    configured Gateway ARN remains the authorization boundary.
    """
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        expected_endpoint = state["gateway_endpoint"].rstrip("/")
        expected_tool = state["gateway_tool_name"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(f"Cannot read exact Gateway endpoint/tool from private S05 state at {STATE_FILE}.") from exc
    if endpoint.rstrip("/") != expected_endpoint or tool_name != expected_tool:
        raise ValueError("Gateway endpoint/tool differs from the values copied from the private S04/S05 state; no AWS or model call was made.")


def _parse_reader_summary(text_result: str, invocation_id: str, timestamp_ms: int) -> dict[str, Any]:
    """Allowlist the bounded S04 reader response; fail closed on unknown schemas."""
    try:
        payload = json.loads(text_result)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("S04 reader returned malformed JSON; stop without retrying.") from exc
    if (not isinstance(payload, dict) or payload.get("ok") is not True
            or not isinstance(payload.get("data"), dict)):
        raise RuntimeError("S04 reader returned an unsupported response; stop without retrying.")
    data = payload["data"]
    reader = payload.get("reader_execution")
    if (not isinstance(reader, dict) or not isinstance(reader.get("lambda_request_id"), str)
            or not reader["lambda_request_id"] or len(reader["lambda_request_id"]) > 128):
        raise RuntimeError("S04 reader response has no valid reader request ID; stop without retrying.")
    found = data.get("found")
    truncated = data.get("truncated")
    if not isinstance(found, bool) or not isinstance(truncated, bool):
        raise RuntimeError("S04 reader response has no boolean truncated status; stop without retrying.")
    events = data.get("events")
    if not isinstance(events, list) or len(events) > 1:
        raise RuntimeError("S04 reader response exceeded the one-event schema; stop without retrying.")
    if not found:
        if events:
            raise RuntimeError("S04 reader response is inconsistent; stop without retrying.")
        return {"found": False, "events": [], "truncated": truncated,
                "certainty": "inconclusive; no current health or cause can be inferred"}
    if len(events) != 1 or not isinstance(events[0], dict):
        raise RuntimeError("S04 reader response is inconsistent; stop without retrying.")
    event = events[0]
    required = ("target_invocation_id", "target_timestamp_ms", "status", "tool_call_count", "tool_names")
    if any(key not in event for key in required):
        raise RuntimeError("S04 reader event is missing required S03 metadata; stop without retrying.")
    stamp = event["target_timestamp_ms"]
    if isinstance(stamp, str) and stamp.isdigit():
        stamp = int(stamp)
    if (event["event"] != "agent_invocation" or event["target_invocation_id"] != invocation_id or stamp != timestamp_ms
            or isinstance(event["status"], bool) or not isinstance(event["status"], int)
            or isinstance(event["tool_call_count"], bool) or not isinstance(event["tool_call_count"], int)
            or event["tool_call_count"] < 0 or not isinstance(event["tool_names"], list)
            or len(event["tool_names"]) > 20
            or any(not isinstance(name, str) or len(name) > 128 for name in event["tool_names"])):
        raise RuntimeError("S04 reader event does not match the selected S03 invocation schema; stop without retrying.")
    safe_event: dict[str, Any] = {"event": "agent_invocation", "invocation_id": invocation_id,
        "timestamp_ms": timestamp_ms,
        "status": event["status"], "tool_call_count": event["tool_call_count"],
        "tool_names": event["tool_names"]}
    if isinstance(event.get("tool_names_truncated"), bool):
        safe_event["tool_names_truncated"] = event["tool_names_truncated"]
    if isinstance(event.get("error_type"), str) and len(event["error_type"]) <= 128:
        safe_event["error_type"] = event["error_type"]
    return {"found": True, "events": [safe_event], "truncated": truncated,
            "reader_execution": {"lambda_request_id": reader["lambda_request_id"]},
            "certainty": "bounded S03 execution metadata only; not the prompt, answer, arguments, current health, or cause"}


def _require_complete_ask_trace(tool_trace: list[dict[str, Any]], memory_mode: str) -> None:
    """Require the bounded data and optional-memory calls before showing an answer."""
    reader_traces = [item for item in tool_trace if item.get("tool") == "S03 invocation Gateway"]
    memory_traces = [item for item in tool_trace if item.get("tool") == "AgentCore Memory"]
    if len(reader_traces) != 1:
        raise RuntimeError("The S03 reader was not called exactly once; no answer is shown.")
    reader_summary = reader_traces[0].get("result", {})
    reader_events = reader_summary.get("events", [])
    if (reader_summary.get("found") is not True or reader_summary.get("truncated") is not False
            or len(reader_events) != 1
            or reader_events[0].get("tool_names_truncated") is True):
        raise RuntimeError("The selected S03 summary is missing or incomplete; no answer is shown.")
    if memory_mode == "on" and len(memory_traces) != 1:
        raise RuntimeError("Memory ON requires exactly one preference retrieval; no answer is shown.")
    if memory_mode == "off" and memory_traces:
        raise RuntimeError("Memory OFF unexpectedly retrieved a preference; no answer is shown.")


def ask_agent(args: argparse.Namespace) -> None:
    """Invoke the S02 agent with a fixed S03 invocation lookup and optional Memory."""
    if not args.question.strip() or len(args.question) > 2_000:
        raise ValueError("Question must contain 1-2,000 characters.")
    invocation_id = _validate_invocation_id(args.target_invocation_id)
    timestamp_ms = _validate_timestamp_ms(args.target_timestamp_ms)
    if not args.gateway_tool_name.strip() or len(args.gateway_tool_name) > 256:
        raise ValueError("Supply the exact configured S03 invocation-reader tool name.")
    _bound_gateway_config(args.gateway_endpoint, args.gateway_tool_name)
    from urllib.parse import urlparse
    gateway_url = urlparse(args.gateway_endpoint)
    if (gateway_url.scheme != "https" or not gateway_url.hostname
            or not gateway_url.hostname.endswith(".gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com")
            or gateway_url.path != "/mcp"):
        raise ValueError("Use the exact configured Gateway HTTPS endpoint with the /mcp route in Tokyo.")
    session, actor, identity = _checked_session(args)
    _validate_memory_id(args.memory_id)

    import os
    import sys
    from pathlib import Path
    s02_dir = str(Path(__file__).resolve().parents[1] / "s02")
    if s02_dir not in sys.path:
        sys.path.insert(0, s02_dir)
    import agent_app
    from strands import tool
    gateway_calls = 0
    memory_calls = 0
    tool_trace: list[dict[str, Any]] = []

    @tool
    def read_s03_invocation() -> str:
        """Read only the S03 invocation summary selected by the learner."""
        nonlocal gateway_calls
        if gateway_calls >= 1:
            return json.dumps({"error": "one S03 invocation read is allowed per agent run"})
        gateway_calls += 1
        from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
        from strands.tools.mcp.mcp_client import MCPClient
        import uuid

        tool_input = {"target_invocation_id": invocation_id,
                      "target_timestamp_ms": timestamp_ms, "max_results": 2}
        mcp = MCPClient(lambda: aws_iam_streamablehttp_client(
            endpoint=args.gateway_endpoint, aws_region=REGION,
            aws_service="bedrock-agentcore", timeout=10, sse_read_timeout=20,
        ))
        with mcp:
            result = mcp.call_tool_sync(
                tool_use_id=str(uuid.uuid4()), name=args.gateway_tool_name,
                arguments=tool_input,
            )
        if result.get("status") != "success" or result.get("isError") is True:
            raise RuntimeError("S04 Gateway reader failed; stop without retrying.")
        content = result.get("content")
        texts = [item.get("text") for item in content if isinstance(item, dict) and isinstance(item.get("text"), str)] if isinstance(content, list) else []
        if len(texts) != 1:
            raise RuntimeError("S04 Gateway returned an unsupported content shape; stop without retrying.")
        safe_result = _parse_reader_summary(texts[0], invocation_id, timestamp_ms)
        tool_trace.append({"tool": "S03 invocation Gateway", "status": "success",
                           "input": {"target_invocation_id": invocation_id,
                                     "target_timestamp_ms": timestamp_ms, "max_results": 2},
                           "result": safe_result})
        return json.dumps(safe_result, ensure_ascii=False)

    tools: list[Any] = [read_s03_invocation]
    if args.memory_mode == "on":
        @tool
        def recall_my_preference() -> str:
            """Retrieve this authenticated learner's long-term preferences only."""
            nonlocal memory_calls
            if memory_calls >= 1:
                return json.dumps({"error": "one Memory retrieval is allowed per agent run"})
            memory_calls += 1
            records = list_records(args)
            tool_trace.append({"tool": "AgentCore Memory", "namespace": f"/actors/{actor}/",
                               "record_count": len(records),
                               "record_ids": [item.get("memoryRecordId") for item in records
                                              if item.get("memoryRecordId")]})
            return json.dumps({"actor": actor, "namespace": f"/actors/{actor}/",
                               "records": records}, default=str, ensure_ascii=False)
        tools.append(recall_my_preference)

    prompt = agent_app.SYSTEM_PROMPT + """
S05 memory behavior:
The configured Memory mode for this run is {args.memory_mode!r}.
When the configured mode is 'on', call recall_my_preference exactly once before
answering; use it only for the learner's display preference. When it is 'off', do
not call it. Follow the configured mode stated here; do not infer it from tool
availability.
Use read_s03_invocation for the learner-selected S03 invocation summary when asked.
That summary is not the latest S09 incident log and cannot establish current health,
service impact, or a root cause. A missing or truncated result is inconclusive.
Memory may affect presentation wording only; it is never a source of operational facts.
Treat found=false, no events, or truncated=true as inconclusive. Do not infer current
Runtime health, an incident, business impact, or a cause from this bounded record.
The record contains execution metadata only, not the prompt, answer, or tool arguments.
""".format(args=args)
    print(json.dumps({"account": identity["Account"], "role": identity["Arn"],
                      "actor": actor, "memory_mode": args.memory_mode,
                      "runtime_invoked": False}, ensure_ascii=False))
    prior_profile = os.environ.get("AWS_PROFILE")
    if args.profile:
        os.environ["AWS_PROFILE"] = args.profile
    try:
        result = agent_app._make_agent(REGION, tools=tools, system_prompt=prompt)(
            args.question,
            limits={"turns": agent_app.MAX_AGENT_TURNS,
                    "output_tokens": agent_app.MAX_TOTAL_OUTPUT_TOKENS,
                    "total_tokens": agent_app.MAX_TOTAL_TOKENS},
        )
    finally:
        if prior_profile is None:
            os.environ.pop("AWS_PROFILE", None)
        else:
            os.environ["AWS_PROFILE"] = prior_profile
    usage = result.metrics.accumulated_usage
    try:
        _require_complete_ask_trace(tool_trace, args.memory_mode)
    except RuntimeError as exc:
        print("\nAgent response withheld: the required evidence was incomplete.")
        print(json.dumps({"status": "incomplete", "reason": str(exc),
                          "tools": tool_trace, "token_usage": usage},
                         default=str, ensure_ascii=False, indent=2))
        raise
    answer = agent_app._display_answer(result)
    print("\nAgent response:\n" + answer)
    print(json.dumps({"tools": tool_trace, "token_usage": usage},
                     default=str, ensure_ascii=False, indent=2))


def _expect_access_denied(call: Any, label: str) -> None:
    try:
        call()
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in {"AccessDeniedException", "AccessDenied", "UnauthorizedException"}:
            print(f"{label}: denied by AWS authorization ({code})")
            return
        raise RuntimeError(f"{label}: expected AccessDenied, received {code}.") from exc
    raise RuntimeError(f"{label}: cross-actor request unexpectedly succeeded.")


def probe_other_actor(args: argparse.Namespace) -> None:
    session, actor, _ = _checked_session(args)
    other_actor = ACTORS["b"] if actor == ACTORS["a"] else ACTORS["a"]
    client = session.client("bedrock-agentcore")
    _expect_access_denied(
        lambda: client.list_events(memoryId=args.memory_id, actorId=other_actor,
                                   sessionId=SESSION_ID, maxResults=1),
        "Short-term event probe",
    )
    _expect_access_denied(
        lambda: client.retrieve_memory_records(
            memoryId=args.memory_id, namespace=f"/actors/{other_actor}/", maxResults=1,
            searchCriteria={"memoryStrategyId": args.strategy_id,
                            "searchQuery": "dashboard preference", "topK": 1},
        ),
        "Long-term namespace probe",
    )


def create_memory(args: argparse.Namespace) -> None:
    if not args.confirm_create:
        raise ValueError("Resource creation requires --confirm-create after reviewing the exact target.")
    session = _session(args.profile, args.region)
    identity = session.client("sts").get_caller_identity()
    if identity.get("Account") != args.expected_account:
        raise ValueError("Caller account differs from the expected learning account.")
    role_arn = _assumed_role_arn(identity.get("Arn", ""), args.expected_account)
    if role_arn != args.admin_role_arn:
        raise ValueError("Only the exact S05 exercise-admin role can create the Memory resource.")
    control = session.client("bedrock-agentcore-control")
    existing: list[dict[str, Any]] = []
    token = None
    while True:
        page_args: dict[str, Any] = {"maxResults": 100}
        if token:
            page_args["nextToken"] = token
        page = control.list_memories(**page_args)
        existing.extend(page.get("memories", []))
        token = page.get("nextToken")
        if not token:
            break
    for summary in existing:
        resource = control.get_memory(memoryId=summary["id"]).get("memory", {})
        if resource.get("name") == MEMORY_NAME:
            raise RuntimeError(
                "An S05-named Memory already exists. Do not create another; reconcile its exact ID/state first."
            )
    if len(existing) >= DOCUMENTED_DEFAULT_MEMORY_QUOTA:
        raise RuntimeError(
            "Memory inventory is at/above AWS's documented default quota of 150; stop without requesting a quota increase."
        )
    try:
        result = control.create_memory(
            name=MEMORY_NAME,
            description="S05 synthetic user-preference memory exercise; no customer or production data.",
            eventExpiryDuration=3,
            memoryStrategies=[STRATEGY],
            tags={"Course": "udemy-agentcore-s05", "Owner": "course-lab"},
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        if code in {"ServiceQuotaExceededException", "QuotaExceededException", "ResourceLimitExceededException"}:
            raise RuntimeError(
                "AWS rejected Memory creation for quota/resource-limit reasons. Stop; do not request an increase or retry blindly."
            ) from exc
        raise
    memory = result.get("memory", {})
    memory_id = memory.get("id")
    memory_arn = memory.get("arn")
    if not memory_id or not memory_arn:
        raise RuntimeError("CreateMemory returned no exact resource ID/ARN; inspect current resource state before retrying.")
    if memory.get("name") not in {None, MEMORY_NAME}:
        raise RuntimeError("CreateMemory returned an unexpected resource name; stop and inspect.")
    print(json.dumps({"memory_id": memory_id, "memory_arn": memory_arn,
                      "name": MEMORY_NAME, "status": memory.get("status")}, indent=2))


def _admin_session(args: argparse.Namespace) -> boto3.Session:
    session = _session(args.profile, args.region)
    identity = session.client("sts").get_caller_identity()
    if identity.get("Account") != args.expected_account:
        raise ValueError("Caller account differs from the expected learning account.")
    if _assumed_role_arn(identity.get("Arn", ""), args.expected_account) != args.admin_role_arn:
        raise ValueError("Cleanup requires the exact S05 exercise-admin role.")
    return session


def admin_delete_event(args: argparse.Namespace) -> None:
    if args.owner not in ACTORS or not args.event_id:
        raise ValueError("Select owner a/b and provide the exact Event ID from this exercise.")
    _verify_cleanup_target(args, "event", args.event_id)
    session = _admin_session(args)
    result = session.client("bedrock-agentcore").delete_event(
        memoryId=args.memory_id, actorId=ACTORS[args.owner], sessionId=SESSION_ID, eventId=args.event_id
    )
    print(json.dumps({"deleted_event_id": result.get("eventId"), "actor": ACTORS[args.owner],
                      "session_id": SESSION_ID}, indent=2))


def admin_delete_record(args: argparse.Namespace) -> None:
    if args.owner not in ACTORS or not args.record_id:
        raise ValueError("Select owner a/b and provide the exact Memory Record ID from this exercise.")
    _verify_cleanup_target(args, "record", args.record_id)
    session = _admin_session(args)
    result = session.client("bedrock-agentcore").delete_memory_record(
        memoryId=args.memory_id,
        memoryRecordId=args.record_id,
        namespace=f"/actors/{ACTORS[args.owner]}/",
    )
    print(json.dumps({"deleted_record_id": result.get("memoryRecordId"),
                      "namespace": f"/actors/{ACTORS[args.owner]}/"}, indent=2))


def _verify_cleanup_target(args: argparse.Namespace, kind: str, supplied_id: str) -> None:
    """Fail closed unless the requested deletion matches the private run state exactly."""
    try:
        with STATE_FILE.open("r", encoding="utf-8") as state_file:
            state = json.load(state_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read private S05 state at {STATE_FILE}; no deletion was attempted.") from exc
    owner = ACTORS[args.owner]
    if (state.get("account_id") != args.expected_account
            or state.get("region") != REGION
            or state.get("memory_id") != args.memory_id):
        raise ValueError("Private S05 state does not match the requested account, region, and Memory; no deletion was attempted.")
    key = "event_ids" if kind == "event" else "record_ids"
    if not isinstance(state.get(key), dict) or state[key].get(owner) != supplied_id:
        raise ValueError(f"The supplied {kind} ID is not the exact saved ID for {owner}; no deletion was attempted.")


def _add_common(parser: argparse.ArgumentParser, *, admin: bool = False) -> None:
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--profile", help="Named AWS profile for the already-assumed exercise role.")
    parser.add_argument("--region", default=REGION)
    if admin:
        parser.add_argument("--admin-role-arn", required=True)
    else:
        parser.add_argument("--actor-a-role-arn", required=True)
        parser.add_argument("--actor-b-role-arn", required=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="S05 local AgentCore Memory identity lab")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, handler, admin in [
        ("identity", whoami, False), ("save-event", save_event, False),
        ("read-events", read_events, False), ("read-records", read_records, False),
        ("probe-other", probe_other_actor, False), ("create-memory", create_memory, True),
        ("admin-delete-event", admin_delete_event, True), ("admin-delete-record", admin_delete_record, True),
        ("ask", ask_agent, False),
    ]:
        command = sub.add_parser(name)
        _add_common(command, admin=admin)
        if name in {"save-event", "read-events", "read-records", "probe-other", "admin-delete-event", "admin-delete-record"}:
            command.add_argument("--memory-id", required=True)
        if name in {"read-records", "probe-other"}:
            command.add_argument("--strategy-id", required=True)
        if name == "ask":
            command.add_argument("--memory-id", required=True)
            command.add_argument("--strategy-id", required=True)
            command.add_argument("--memory-mode", choices=("off", "on"), required=True)
            command.add_argument("--gateway-endpoint", required=True)
            command.add_argument("--gateway-tool-name", required=True)
            command.add_argument("--target-invocation-id", required=True)
            command.add_argument("--target-timestamp-ms", required=True)
            command.add_argument("--question", required=True)
        if name == "create-memory":
            command.add_argument("--confirm-create", action="store_true")
        if name in {"admin-delete-event", "admin-delete-record"}:
            command.add_argument("--owner", choices=ACTORS, required=True)
            command.add_argument("--event-id" if name.endswith("event") else "--record-id", required=True)
        command.set_defaults(handler=handler)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
