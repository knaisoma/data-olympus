"""End-to-end smoke test via FastMCP's in-memory test client.

If FastMCP's API changed between the pinned version and the time of writing,
adjust the import + client invocation per the FastMCP docs. The goal: prove
the server starts, tools register, and at least one read call round-trips.
"""
from __future__ import annotations

import sqlite3
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
async def test_empty_corpus_startup_builds_schema_and_read_tools_degrade(
    tmp_path: Path,
) -> None:
    kb = tmp_path / "empty-kb"
    kb.mkdir()
    index_path = tmp_path / "idx.db"
    app = build_app(
        kb_main_path=kb,
        kb_index_path=index_path,
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        bootstrap_now=True,
    )

    conn = sqlite3.connect(index_path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert {"docs", "meta"} <= tables

    async with Client(app) as client:
        health = await client.call_tool("kb_health", {})
        search = await client.call_tool("kb_search", {"query": "worktree", "limit": 5})
        get = await client.call_tool("kb_get", {"id": "STD-U-001"})
        listing = await client.call_tool("kb_list", {"tier": "T1"})
        outline = await client.call_tool("kb_outline", {})
        onboarding = await client.call_tool(
            "kb_onboarding_status",
            {"workspace": "example-project"},
        )
        cleanup = await client.call_tool(
            "kb_cleanup_plan",
            {
                "workspace": "example-project",
                "local_files": [{"path": "README.md", "content": "# Local\n"}],
            },
        )

    for result in (health, search, get, listing, outline, onboarding, cleanup):
        assert "sqlite3" not in str(result).lower()


@pytest.mark.asyncio
async def test_kb_cleanup_plan_tool_is_registered(tmp_kb: Path, tmp_path: Path) -> None:
    app = build_app(
        kb_main_path=tmp_kb,
        kb_index_path=tmp_path / "idx.db",
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        bootstrap_now=True,
        tool_discovery_mode="all",
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


def test_missing_corpus_error_names_the_setting_and_its_origin(tmp_path: Path) -> None:
    """A missing corpus must say which setting to change (issue #244).

    /kb-main is the built-in default, so an operator who never set
    KB_MAIN_PATH would otherwise see a path they have never heard of in the
    error and have nothing to search for.
    """
    from data_olympus.index import Index

    index = Index(tmp_path / "idx.db")
    missing = tmp_path / "not-there"

    with pytest.raises(NotADirectoryError) as excinfo:
        index.build(missing, source_commit="0" * 40)

    message = str(excinfo.value)
    assert "KB_MAIN_PATH" in message
    assert str(missing) in message
    assert "does not exist" in message
    assert "built-in default" in message


def test_missing_corpus_error_says_when_the_path_was_configured(
    tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    from data_olympus.index import Index

    missing = tmp_path / "not-there"
    monkeypatch.setenv("KB_MAIN_PATH", str(missing))
    index = Index(tmp_path / "idx.db")

    with pytest.raises(NotADirectoryError) as excinfo:
        index.build(missing, source_commit="0" * 40)

    assert "configured via KB_MAIN_PATH" in str(excinfo.value)


def test_a_file_instead_of_a_corpus_directory_is_reported_distinctly(
    tmp_path: Path,
) -> None:
    from data_olympus.index import Index

    not_a_dir = tmp_path / "corpus.md"
    not_a_dir.write_text("# oops\n", encoding="utf-8")
    index = Index(tmp_path / "idx.db")

    with pytest.raises(NotADirectoryError) as excinfo:
        index.build(not_a_dir, source_commit="0" * 40)

    assert "exists but is not a directory" in str(excinfo.value)
