"""MCP argument-validation failures must not echo submitted input.

FastMCP's default handling of a tool call whose arguments fail validation
returns the pydantic error text to the caller and logs it at WARNING, and both
carry the submitted input (and, for an unexpected argument, its name). A
credential an agent puts in the wrong field would therefore reach the server
log. These tests drive the real app through an in-memory MCP client with a
credential-shaped fixture in every position the input can travel and assert
that neither the response nor any log record contains it, while the error still
names the parameter and the rule.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import pytest
from fastmcp import Client

from data_olympus.server import build_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

# Credential-shaped, never a real credential.
FAKE = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
# A recognizable fragment of FAKE that pydantic's truncated repr keeps.
FRAGMENTS = (FAKE, FAKE[:8], FAKE[-16:])


_FORMATTER = logging.Formatter("%(levelname)s %(name)s %(message)s")


class _Capture(logging.Handler):
    """Keeps each record fully formatted, traceback included: a leak can sit in
    ``exc_info`` where ``getMessage()`` never looks."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []
        self.info_and_above: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("fastmcp.client"):
            # The in-process test client's own trace, not the server's log.
            return
        formatted = _FORMATTER.format(record)
        self.messages.append(formatted)
        if record.levelno >= logging.INFO:
            self.info_and_above.append(formatted)


@pytest.fixture
def captured_logs() -> Iterator[_Capture]:
    handler = _Capture()
    # The fastmcp logger does not propagate to root, so attach to both.
    loggers = [logging.getLogger(), logging.getLogger("fastmcp"), logging.getLogger("mcp")]
    saved = [(lg, lg.level) for lg in loggers]
    for lg in loggers:
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        for lg, level in saved:
            lg.removeHandler(handler)
            lg.setLevel(level)


def _app(tmp_git_kb: Path, tmp_path: Path) -> Any:
    return build_app(
        kb_main_path=tmp_git_kb,
        kb_index_path=tmp_path / "idx.db",
        sync_interval_sec=60, staleness_degraded_sec=600, bootstrap_now=True,
        kb_remote_url=str(tmp_git_kb),
        worktree_root=str(tmp_path / "wts"),
        pending_root=str(tmp_path / "pending"),
        push_queue_root=str(tmp_path / "pushq"),
        audit_log_path=str(tmp_path / "audit.log"),
    )


def _memory_args(**overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "text": "note", "tags": [], "source_session": "s",
        "agent_identity": "claude", "confidence": 0.3,
    }
    args.update(overrides)
    return args


CASES: dict[str, tuple[str, dict[str, Any], str]] = {
    # name: (tool, arguments, parameter the error must still name)
    "wrong_type_scalar": ("kb_propose_memory", _memory_args(confidence=FAKE), "confidence"),
    "nested_object_in_string": (
        "kb_propose_memory", _memory_args(text={"nested": FAKE}), "text",
    ),
    "unexpected_argument_value": (
        "kb_propose_memory", _memory_args(contest={"contradicts": [FAKE]}), "unexpected",
    ),
    "unexpected_argument_name": ("kb_propose_memory", _memory_args(**{FAKE: 1}), "unexpected"),
    "malformed_list_item": (
        "kb_propose_memory", _memory_args(evidence=[{"k": FAKE}]), "evidence",
    ),
    "mapping_key_in_list": (
        "kb_propose_memory", _memory_args(tags=[{FAKE: "v"}]), "tags",
    ),
    "open_read_tool": ("kb_search", {"query": "x", "limit": FAKE}, "limit"),
}


async def _call(client: Client, tool: str, args: dict[str, Any]) -> tuple[bool, str]:
    result = await client.call_tool(tool, args, raise_on_error=False)
    text = " ".join(getattr(block, "text", "") for block in result.content)
    return bool(result.is_error), text


def _assert_clean(text: str, logs: list[str]) -> None:
    for fragment in FRAGMENTS:
        assert fragment not in text, f"response echoes input: {text!r}"
        leaked = [m for m in logs if fragment in m]
        assert not leaked, f"log echoes input: {leaked!r}"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(CASES))
async def test_validation_error_does_not_echo_input(
    case: str, tmp_git_kb: Path, tmp_path: Path, captured_logs: _Capture,
) -> None:
    tool, args, named = CASES[case]
    app = _app(tmp_git_kb, tmp_path)
    async with Client(app) as client:
        is_error, text = await _call(client, tool, args)
    assert is_error, text
    _assert_clean(text, captured_logs.messages)
    assert named in text, f"error no longer names the problem: {text!r}"


@pytest.mark.asyncio
async def test_validation_error_through_call_tool_proxy_does_not_echo_input(
    tmp_git_kb: Path, tmp_path: Path, captured_logs: _Capture,
) -> None:
    """Production defaults to search-mode tool discovery, where agents reach a
    tool through the ``call_tool`` proxy."""
    app = _app(tmp_git_kb, tmp_path)
    async with Client(app) as client:
        is_error, text = await _call(client, "call_tool", {
            "name": "kb_propose_memory", "arguments": _memory_args(confidence=FAKE),
        })
    assert is_error, text
    _assert_clean(text, captured_logs.messages)


@pytest.mark.asyncio
async def test_unknown_tool_name_is_not_echoed(
    tmp_git_kb: Path, tmp_path: Path, captured_logs: _Capture,
) -> None:
    """A tool name is caller input too when it does not resolve, including in
    the SDK's DEBUG tool-cache trace and FastMCP's handler trace."""
    app = _app(tmp_git_kb, tmp_path)
    async with Client(app) as client:
        direct_error, direct = await _call(client, FAKE, {})
        proxy_error, proxied = await _call(client, "call_tool", {"name": FAKE, "arguments": {}})
    assert direct_error and proxy_error
    _assert_clean(direct + proxied, captured_logs.messages)


@pytest.mark.asyncio
async def test_debug_call_log_does_not_record_arguments(
    tmp_git_kb: Path, tmp_path: Path, captured_logs: _Capture,
) -> None:
    """FastMCP logs every tool call's arguments at DEBUG, valid calls included."""
    app = _app(tmp_git_kb, tmp_path)
    async with Client(app) as client:
        is_error, _text = await _call(client, "kb_search", {"query": FAKE})
    assert not is_error
    assert not [m for m in captured_logs.messages if FAKE in m], captured_logs.messages
    assert any("Handler called: call_tool" in m for m in captured_logs.messages)


@pytest.mark.asyncio
async def test_tool_body_error_traceback_does_not_log_input(
    tmp_git_kb: Path, tmp_path: Path, captured_logs: _Capture,
) -> None:
    """A tool's own error can quote its input; FastMCP logs it with a traceback.

    The response keeps the tool's guidance, which goes only to the caller that
    sent the value. The log keeps frames and exception types, not messages.
    """
    app = _app(tmp_git_kb, tmp_path)
    async with Client(app) as client:
        is_error, text = await _call(
            client, "kb_search", {"query": "x", "validity_state": f"expiring_within:{FAKE}"},
        )
    assert is_error
    assert "validity_state" in text
    for fragment in FRAGMENTS:
        leaked = [m for m in captured_logs.messages if fragment in m]
        assert not leaked, f"log echoes input: {leaked!r}"
    tracebacks = [m for m in captured_logs.messages if "Traceback" in m]
    assert tracebacks, "the traceback itself should still be logged for diagnosis"
    assert any("ValueError" in m for m in tracebacks)


@pytest.mark.asyncio
async def test_malformed_request_envelope_is_not_logged(
    tmp_git_kb: Path, tmp_path: Path, captured_logs: _Capture,
) -> None:
    """The MCP SDK validates the request envelope before any tool runs and logs
    the failure, and the raw message, through the root logger."""
    import mcp.types as mt

    app = _app(tmp_git_kb, tmp_path)
    params = mt.CallToolRequestParams.model_construct(name="kb_search", arguments=[FAKE])
    request = mt.ClientRequest(
        mt.CallToolRequest.model_construct(method="tools/call", params=params),
    )
    async with Client(app) as client:
        with pytest.raises(Exception):  # noqa: B017, PT011 - any protocol error is fine
            await client.session.send_request(request, mt.CallToolResult)
    for fragment in FRAGMENTS:
        leaked = [m for m in captured_logs.messages if fragment in m]
        assert not leaked, f"log echoes input: {leaked!r}"
    assert any("mcp/shared/session.py" in m for m in captured_logs.messages), (
        "the diagnostic should still say where it came from"
    )


@pytest.mark.asyncio
async def test_unknown_resource_uri_is_not_logged(
    tmp_git_kb: Path, tmp_path: Path, captured_logs: _Capture,
) -> None:
    app = _app(tmp_git_kb, tmp_path)
    async with Client(app) as client:
        with pytest.raises(Exception):  # noqa: B017, PT011
            await client.read_resource(f"data://{FAKE}")
    for fragment in FRAGMENTS:
        leaked = [m for m in captured_logs.messages if fragment in m]
        assert not leaked, f"log echoes input: {leaked!r}"


@pytest.mark.asyncio
async def test_malformed_notification_is_not_logged(
    tmp_git_kb: Path, tmp_path: Path, captured_logs: _Capture,
) -> None:
    import mcp.types as mt

    app = _app(tmp_git_kb, tmp_path)
    params = mt.ProgressNotificationParams.model_construct(progressToken="t", progress=FAKE)
    note = mt.ClientNotification(
        mt.ProgressNotification.model_construct(method="notifications/progress", params=params),
    )
    async with Client(app) as client:
        await client.session.send_notification(note)
        await client.ping()
    for fragment in FRAGMENTS:
        leaked = [m for m in captured_logs.messages if fragment in m]
        assert not leaked, f"log echoes input: {leaked!r}"


@pytest.mark.asyncio
async def test_unknown_prompt_name_is_not_logged(
    tmp_git_kb: Path, tmp_path: Path, captured_logs: _Capture,
) -> None:
    app = _app(tmp_git_kb, tmp_path)
    async with Client(app) as client:
        with pytest.raises(Exception):  # noqa: B017, PT011
            await client.get_prompt(FAKE, {})
    for fragment in FRAGMENTS:
        leaked = [m for m in captured_logs.messages if fragment in m]
        assert not leaked, f"log echoes input: {leaked!r}"


@pytest.mark.asyncio
async def test_sanitized_warning_is_still_logged(
    tmp_git_kb: Path, tmp_path: Path, captured_logs: _Capture,
) -> None:
    """The operator keeps a diagnosable WARNING: tool, parameter and rule."""
    app = _app(tmp_git_kb, tmp_path)
    async with Client(app) as client:
        await _call(client, "kb_propose_memory", _memory_args(confidence=FAKE))
    relevant = [m for m in captured_logs.messages if "Invalid arguments for tool" in m]
    assert relevant, captured_logs.messages
    assert any("kb_propose_memory" in m and "float_parsing" in m for m in relevant)


@pytest.mark.asyncio
async def test_every_tool_rejects_unexpected_argument_without_echo(
    tmp_git_kb: Path, tmp_path: Path, captured_logs: _Capture,
) -> None:
    app = _app(tmp_git_kb, tmp_path)
    tool_names = sorted(t.name for t in await app.list_tools(run_middleware=False))
    assert tool_names
    async with Client(app) as client:
        for name in tool_names:
            if name in {"call_tool", "tool_search"}:
                continue
            _is_error, text = await _call(client, name, {FAKE: FAKE})
            _assert_clean(text, captured_logs.messages)


@pytest.mark.asyncio
async def test_valid_call_is_unaffected(tmp_git_kb: Path, tmp_path: Path) -> None:
    app = _app(tmp_git_kb, tmp_path)
    async with Client(app) as client:
        is_error, text = await _call(client, "kb_search", {"query": "anything"})
    assert not is_error, text
