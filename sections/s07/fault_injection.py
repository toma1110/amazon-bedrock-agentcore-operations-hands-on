"""Bounded S07 tool failures and a loopback-only external API stub."""

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
from typing import Any, Callable

MAX_TOOL_ARGUMENTS = 4_096
MAX_RETRIES = 1
MAX_DELAY_MS = 1_000
MAX_TIMEOUT_MS = 500


class ToolInputError(ValueError):
    """Raised when a tool request does not match the small S07 schema."""


class ToolExecutionError(RuntimeError):
    """Raised when the tool implementation fails after input validation."""


class ExternalStubError(RuntimeError):
    def __init__(self, status: int, attempts: int) -> None:
        super().__init__(f"local API stub returned HTTP {status}")
        self.status = status
        self.attempts = attempts


def validate_tool_arguments(arguments: Any) -> dict[str, str]:
    """Validate a bounded S07 tool input without forwarding arbitrary data."""
    if not isinstance(arguments, dict):
        raise ToolInputError("tool arguments must be a JSON object")
    try:
        encoded = json.dumps(arguments, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ToolInputError("tool arguments must be JSON-compatible") from exc
    if len(encoded) > MAX_TOOL_ARGUMENTS:
        raise ToolInputError("tool arguments exceed the 4096-byte limit")
    if set(arguments) != {"service", "topic"}:
        raise ToolInputError("tool arguments must contain only service and topic")
    service = arguments.get("service")
    topic = arguments.get("topic")
    if not isinstance(service, str) or service not in {"catalog-api", "report-worker"}:
        raise ToolInputError("service must be catalog-api or report-worker")
    if not isinstance(topic, str) or topic not in {"5xx", "throttle"}:
        raise ToolInputError("topic must be 5xx or throttle")
    return {"service": service, "topic": topic}


def call_checked_tool(
    arguments: Any,
    operation: Callable[[str, str], Any],
    *,
    inject_internal_error: bool = False,
) -> Any:
    """Run the same tool path with distinct input and implementation failures."""
    checked = validate_tool_arguments(arguments)
    if inject_internal_error:
        # The message intentionally contains no learner input or provider data.
        raise ToolExecutionError("simulated tool implementation failure")
    try:
        return operation(checked["service"], checked["topic"])
    except ToolExecutionError:
        raise
    except Exception as exc:
        raise ToolExecutionError("tool implementation failed") from exc


def _loopback_server(status: int, delay_ms: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            time.sleep(delay_ms / 1_000)
            body = json.dumps({"synthetic": True, "status": status}).encode("utf-8")
            self.send_response(status)
            if status == 429:
                self.send_header("Retry-After", "0.1")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                # A client that hit its bounded timeout may have closed the socket.
                return

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    return server


def call_local_api_stub(
    scenario: str,
    *,
    timeout_ms: int = 250,
    max_retries: int = MAX_RETRIES,
) -> dict[str, Any]:
    """Call a temporary localhost-only API stub with bounded retries/timeouts.

    The listener binds to 127.0.0.1 on an ephemeral port. No external host,
    credentials, or learner-controlled URL is accepted.
    """
    scenarios = {"success": (200, 0), "429": (429, 0), "5xx": (503, 0), "delay": (200, 750)}
    if scenario not in scenarios:
        raise ToolInputError("scenario must be success, 429, 5xx, or delay")
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or not 1 <= timeout_ms <= MAX_TIMEOUT_MS:
        raise ToolInputError(f"timeout_ms must be between 1 and {MAX_TIMEOUT_MS}")
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or not 0 <= max_retries <= MAX_RETRIES:
        raise ToolInputError(f"max_retries must be between 0 and {MAX_RETRIES}")

    status, delay_ms = scenarios[scenario]
    if delay_ms > MAX_DELAY_MS:
        raise ToolInputError(f"delay_ms must not exceed {MAX_DELAY_MS}")
    server = _loopback_server(status, delay_ms)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    endpoint = f"http://127.0.0.1:{server.server_port}/sample"
    attempts = 0
    started = time.monotonic()
    try:
        while True:
            attempts += 1
            try:
                with urlopen(endpoint, timeout=timeout_ms / 1_000) as response:
                    body = json.loads(response.read(1_024))
                    return {
                        "status": response.status,
                        "attempts": attempts,
                        "elapsed_ms": round((time.monotonic() - started) * 1_000, 1),
                        "body": body,
                        "destination": "localhost",
                    }
            except HTTPError as exc:
                if exc.code == 429:
                    retry_after = exc.headers.get("Retry-After", "0")
                    try:
                        wait_seconds = min(float(retry_after), 0.25)
                    except ValueError:
                        wait_seconds = 0.0
                    if attempts <= max_retries and wait_seconds > 0:
                        time.sleep(wait_seconds)
                        continue
                    raise ExternalStubError(exc.code, attempts) from None
                if 500 <= exc.code <= 599 and attempts <= max_retries:
                    time.sleep(0.1)
                    continue
                raise ExternalStubError(exc.code, attempts) from None
            except (TimeoutError, URLError) as exc:
                if isinstance(exc, URLError) and not isinstance(exc.reason, TimeoutError):
                    raise ToolExecutionError("local API stub connection failed") from None
                raise TimeoutError(f"local API stub timed out after {timeout_ms} ms") from None
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=1)
