"""End-to-end smoke test via FastMCP's in-memory test client.

If FastMCP's API changed between the pinned version and the time of writing,
adjust the import + client invocation per the FastMCP docs. The goal: prove
the server starts, tools register, and at least one read call round-trips.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastmcp import Client

from data_olympus.server import build_app

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_kb_health_round_trip(tmp_kb: Path, tmp_path: Path) -> None:
    app = build_app(
        kb_main_path=tmp_kb,
        kb_index_path=tmp_path / "idx.db",
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        bootstrap_now=True,
    )
    async with Client(app) as client:
        result = await client.call_tool("kb_health", {})
        # FastMCP returns a CallToolResult; assert the content shape contains the expected keys.
        text = str(result)
        assert "kb_commit" in text
        assert "total_rules" in text


@pytest.mark.asyncio
async def test_kb_outline_round_trip(tmp_kb: Path, tmp_path: Path) -> None:
    app = build_app(
        kb_main_path=tmp_kb,
        kb_index_path=tmp_path / "idx.db",
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        bootstrap_now=True,
    )
    async with Client(app) as client:
        result = await client.call_tool("kb_outline", {})
        text = str(result)
        assert "T1" in text


@pytest.mark.asyncio
async def test_kb_search_round_trip(tmp_kb: Path, tmp_path: Path) -> None:
    app = build_app(
        kb_main_path=tmp_kb,
        kb_index_path=tmp_path / "idx.db",
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        bootstrap_now=True,
    )
    async with Client(app) as client:
        result = await client.call_tool("kb_search", {"query": "worktree", "limit": 5})
        text = str(result)
        assert "STD-U-001" in text


@pytest.mark.asyncio
async def test_kb_get_round_trip(tmp_kb: Path, tmp_path: Path) -> None:
    app = build_app(
        kb_main_path=tmp_kb,
        kb_index_path=tmp_path / "idx.db",
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        bootstrap_now=True,
    )
    async with Client(app) as client:
        result = await client.call_tool("kb_get", {"id": "STD-U-001"})
        text = str(result)
        assert "STD-U-001" in text
        assert "worktree" in text


@pytest.mark.asyncio
async def test_kb_list_round_trip(tmp_kb: Path, tmp_path: Path) -> None:
    app = build_app(
        kb_main_path=tmp_kb,
        kb_index_path=tmp_path / "idx.db",
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        bootstrap_now=True,
    )
    async with Client(app) as client:
        result = await client.call_tool("kb_list", {"tier": "T1", "category": "foundation"})
        text = str(result)
        # Expect at least STD-U-001 in the listing
        assert "STD-U-001" in text


@pytest.mark.asyncio
async def test_kb_cleanup_plan_tool_is_registered(tmp_kb: Path, tmp_path: Path) -> None:
    app = build_app(
        kb_main_path=tmp_kb,
        kb_index_path=tmp_path / "idx.db",
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        bootstrap_now=True,
    )
    tools = await app.list_tools()
    assert any(t.name == "kb_cleanup_plan" for t in tools)


@pytest.mark.asyncio
async def test_kb_cleanup_plan_tool_rejects_invalid_input(tmp_kb: Path, tmp_path: Path) -> None:
    """The MCP tool calls kb_cleanup_plan_fn directly (no REST layer in front
    of it), so validation must live in the shared fn. An out-of-range
    jaccard_threshold must surface as a rejected_invalid_input response, not an
    unhandled exception. (A null/non-str 'content' entry is instead rejected
    earlier, by FastMCP's own pydantic schema for the declared
    list[dict[str, str]] parameter type, before kb_cleanup_plan_fn runs; that
    path is covered at the fn level in test_tools_cleanup_plan.py.)"""
    app = build_app(
        kb_main_path=tmp_kb,
        kb_index_path=tmp_path / "idx.db",
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        bootstrap_now=True,
    )
    async with Client(app) as client:
        result = await client.call_tool(
            "kb_cleanup_plan",
            {
                "workspace": "foo",
                "local_files": [{"path": "README.md", "content": "x"}],
                "jaccard_threshold": 2.0,
            },
        )
        text = str(result)
        assert "rejected_invalid_input" in text
