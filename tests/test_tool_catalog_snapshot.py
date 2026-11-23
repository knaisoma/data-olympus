"""The tool catalog clients see is pinned: names and input schemas in both
discovery modes must stay as they were before the FastMCP 4 and MCP SDK 2
upgrade. Regenerate tests/fixtures/tool_catalog.json only for a deliberate,
reviewed change to a tool's public contract."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client

from data_olympus.server import build_app

FIXTURE = Path(__file__).parent / "fixtures" / "tool_catalog.json"


async def _catalog(tmp_kb, tmp_index_path, mode: str) -> dict[str, object]:
    app = build_app(
        kb_main_path=tmp_kb,
        kb_index_path=tmp_index_path,
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        bootstrap_now=True,
        tool_discovery_mode=mode,
        auth_token="",
    )
    async with Client(app) as client:
        tools = await client.list_tools()
    return {tool.name: tool.input_schema for tool in sorted(tools, key=lambda t: t.name)}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["all", "search"])
async def test_advertised_tool_names_and_input_schemas_are_unchanged(
    tmp_kb, tmp_index_path, mode: str,
) -> None:
    current = await _catalog(tmp_kb, tmp_index_path, mode)
    expected = json.loads(FIXTURE.read_text())[mode]
    assert sorted(current) == sorted(expected)
    for name, schema in expected.items():
        assert current[name] == schema, name
