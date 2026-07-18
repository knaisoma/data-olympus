from __future__ import annotations

import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from data_olympus.config import load_config
from data_olympus.server import build_app

CORE_TOOLS = {
    "kb_consult",
    "kb_search",
    "kb_get",
    "kb_health",
    "kb_gate_check",
    "kb_record_event",
    "kb_session_recap",
}
SEARCH_TOOLS = CORE_TOOLS | {"tool_search", "call_tool"}


def test_config_defaults_tool_discovery_to_search(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KB_TOOL_DISCOVERY_MODE", raising=False)
    assert load_config().tool_discovery_mode == "search"


def test_config_accepts_all_tool_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_TOOL_DISCOVERY_MODE", "all")
    assert load_config().tool_discovery_mode == "all"


def test_config_rejects_unknown_tool_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_TOOL_DISCOVERY_MODE", "hidden")
    with pytest.raises(ValueError, match="KB_TOOL_DISCOVERY_MODE"):
        load_config()


def test_build_app_rejects_unknown_tool_discovery(tmp_kb, tmp_index_path) -> None:
    with pytest.raises(ValueError, match="KB_TOOL_DISCOVERY_MODE"):
        _app(tmp_kb, tmp_index_path, mode="hidden")


def _app(tmp_kb, tmp_index_path, *, mode: str = "search", auth_token: str = ""):
    return build_app(
        kb_main_path=tmp_kb,
        kb_index_path=tmp_index_path,
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        bootstrap_now=True,
        tool_discovery_mode=mode,
        auth_token=auth_token,
    )


PROPOSE_MEMORY_ARGS = {
    "text": "Authorization probe",
    "tags": ["test"],
    "source_session": "test-session",
    "agent_identity": "test-agent",
    "confidence": 0.5,
}


@pytest.mark.asyncio
async def test_search_mode_lists_only_core_and_discovery_tools(
    tmp_kb, tmp_index_path,
) -> None:
    app = _app(tmp_kb, tmp_index_path)
    assert {tool.name for tool in await app.list_tools()} == SEARCH_TOOLS


@pytest.mark.asyncio
async def test_all_mode_lists_complete_native_catalog(tmp_kb, tmp_index_path) -> None:
    app = _app(tmp_kb, tmp_index_path, mode="all")
    names = {tool.name for tool in await app.list_tools()}
    assert names > CORE_TOOLS
    assert {"kb_outline", "kb_list", "kb_propose_memory"} <= names
    assert "tool_search" not in names
    assert "call_tool" not in names


@pytest.mark.asyncio
async def test_hidden_tool_remains_callable_by_original_name(
    tmp_kb, tmp_index_path,
) -> None:
    app = _app(tmp_kb, tmp_index_path)
    async with Client(app) as client:
        result = await client.call_tool("kb_outline", {})
    assert "tiers" in result.data


@pytest.mark.asyncio
async def test_tool_search_discovers_hidden_tool(tmp_kb, tmp_index_path) -> None:
    app = _app(tmp_kb, tmp_index_path)
    async with Client(app) as client:
        result = await client.call_tool(
            "tool_search", {"query": "structural map of knowledge tiers"}
        )
    rendered = result.data if isinstance(result.data, str) else json.dumps(result.data)
    assert "kb_outline" in rendered


@pytest.mark.asyncio
async def test_call_tool_invokes_discovered_hidden_tool(tmp_kb, tmp_index_path) -> None:
    app = _app(tmp_kb, tmp_index_path)
    async with Client(app) as client:
        result = await client.call_tool(
            "call_tool", {"name": "kb_outline", "arguments": {}}
        )
    assert "tiers" in result.data


@pytest.mark.asyncio
async def test_hidden_write_tool_denies_anonymous_direct_call(
    tmp_kb, tmp_index_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_http_headers",
        lambda *_args, **_kwargs: {},
    )
    app = _app(tmp_kb, tmp_index_path, auth_token="operator-secret")
    async with Client(app) as client:
        with pytest.raises(ToolError, match="unauthorized"):
            await client.call_tool("kb_propose_memory", PROPOSE_MEMORY_ARGS)


@pytest.mark.asyncio
async def test_hidden_write_tool_denies_anonymous_proxy_call(
    tmp_kb, tmp_index_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_http_headers",
        lambda *_args, **_kwargs: {},
    )
    app = _app(tmp_kb, tmp_index_path, auth_token="operator-secret")
    async with Client(app) as client:
        with pytest.raises(ToolError, match="unauthorized"):
            await client.call_tool(
                "call_tool",
                {"name": "kb_propose_memory", "arguments": PROPOSE_MEMORY_ARGS},
            )
