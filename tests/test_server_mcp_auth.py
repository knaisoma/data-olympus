"""Unit tests for the MCP-transport auth middleware (MCPAuthMiddleware).

The middleware is the MCP counterpart to the REST principal checks: it enforces
write-tool capabilities and stashes the resolved principal so the write-tool
closures can apply the confidence clamp. We exercise it directly with a fake
call context, monkeypatching get_http_headers, because the in-memory MCP client
transport carries no HTTP headers.
"""
from __future__ import annotations

import types

import pytest

from data_olympus.principals import PrincipalRegistry
from data_olympus.server import MCPAuthMiddleware, _current_principal

TOKEN = "operator-secret"


def _ctx(tool_name: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(message=types.SimpleNamespace(name=tool_name))


def _patch_headers(monkeypatch, headers: dict) -> None:
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_http_headers",
        lambda *_a, **_k: headers,
    )


@pytest.mark.asyncio
async def test_write_tool_blocked_without_token(monkeypatch) -> None:
    from fastmcp.exceptions import ToolError
    _patch_headers(monkeypatch, {})  # no auth header -> anonymous
    mw = MCPAuthMiddleware(PrincipalRegistry(auth_token=TOKEN))

    async def call_next(_ctx):  # pragma: no cover - must not be reached
        return "ok"

    with pytest.raises(ToolError):
        await mw.on_call_tool(_ctx("kb_propose_memory"), call_next)


@pytest.mark.asyncio
async def test_write_tool_allowed_with_operator_token(monkeypatch) -> None:
    _patch_headers(monkeypatch, {"authorization": f"Bearer {TOKEN}"})
    mw = MCPAuthMiddleware(PrincipalRegistry(auth_token=TOKEN))
    seen = {}

    async def call_next(_ctx):
        seen["principal"] = _current_principal.get()
        return "ok"

    result = await mw.on_call_tool(_ctx("kb_propose_memory"), call_next)
    assert result == "ok"
    assert seen["principal"].name == "operator"
    assert seen["principal"].can_auto_commit is True


@pytest.mark.asyncio
async def test_write_tool_clamped_for_proposer_without_auto_commit(monkeypatch) -> None:
    _patch_headers(monkeypatch, {"authorization": "Bearer ptok"})
    mw = MCPAuthMiddleware(PrincipalRegistry(
        auth_token=TOKEN,
        principals=[{"name": "proposer", "token": "ptok",
                     "capabilities": ["read", "propose"]}],
    ))
    seen = {}

    async def call_next(_ctx):
        seen["principal"] = _current_principal.get()
        return "ok"

    result = await mw.on_call_tool(_ctx("kb_propose_memory"), call_next)
    assert result == "ok"
    assert seen["principal"].name == "proposer"
    assert seen["principal"].can_auto_commit is False  # clamp engaged


@pytest.mark.asyncio
async def test_write_tool_forbidden_when_capability_missing(monkeypatch) -> None:
    from fastmcp.exceptions import ToolError
    _patch_headers(monkeypatch, {"authorization": "Bearer ptok"})
    mw = MCPAuthMiddleware(PrincipalRegistry(
        auth_token=TOKEN,
        principals=[{"name": "proposer", "token": "ptok",
                     "capabilities": ["read", "propose"]}],
    ))

    async def call_next(_ctx):  # pragma: no cover - must not be reached
        return "ok"

    # proposer may propose but not resolve.
    with pytest.raises(ToolError):
        await mw.on_call_tool(_ctx("kb_resolve_pending"), call_next)


@pytest.mark.asyncio
async def test_read_tool_open_without_token(monkeypatch) -> None:
    _patch_headers(monkeypatch, {})
    mw = MCPAuthMiddleware(PrincipalRegistry(auth_token=TOKEN))
    called = {}

    async def call_next(_ctx):
        called["yes"] = True
        return "search-result"

    result = await mw.on_call_tool(_ctx("kb_search"), call_next)
    assert result == "search-result"
    assert called.get("yes") is True


@pytest.mark.asyncio
async def test_observability_tool_requires_auth_when_configured(monkeypatch) -> None:
    """kb_audit/kb_consult etc. require authentication when auth is configured,
    matching the REST gating of /audit and /consult."""
    from fastmcp.exceptions import ToolError
    _patch_headers(monkeypatch, {})  # anonymous
    mw = MCPAuthMiddleware(PrincipalRegistry(auth_token=TOKEN))

    async def call_next(_ctx):  # pragma: no cover - must not be reached
        return "ok"

    with pytest.raises(ToolError):
        await mw.on_call_tool(_ctx("kb_audit"), call_next)


@pytest.mark.asyncio
async def test_observability_tool_allowed_with_token(monkeypatch) -> None:
    _patch_headers(monkeypatch, {"authorization": f"Bearer {TOKEN}"})
    mw = MCPAuthMiddleware(PrincipalRegistry(auth_token=TOKEN))

    async def call_next(_ctx):
        return "audit-events"

    assert await mw.on_call_tool(_ctx("kb_audit"), call_next) == "audit-events"


@pytest.mark.asyncio
async def test_observability_tool_open_when_no_auth_configured(monkeypatch) -> None:
    _patch_headers(monkeypatch, {})
    mw = MCPAuthMiddleware(PrincipalRegistry())  # no auth configured

    async def call_next(_ctx):
        return "audit-events"

    assert await mw.on_call_tool(_ctx("kb_audit"), call_next) == "audit-events"
