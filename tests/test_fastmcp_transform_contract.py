from __future__ import annotations

import inspect
import tomllib
from pathlib import Path

import pytest
from fastmcp.server.transforms.search import BM25SearchTransform
from fastmcp.tools import Tool


def test_project_requires_fastmcp_with_search_transform() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())
    dependencies = pyproject["project"]["dependencies"]
    assert "fastmcp>=3.4.4,<4" in dependencies


def test_bm25_search_transform_exposes_required_configuration() -> None:
    parameters = inspect.signature(BM25SearchTransform).parameters
    assert {"always_visible", "search_tool_name", "call_tool_name"} <= set(parameters)


@pytest.mark.asyncio
async def test_bm25_search_transform_delegates_hidden_direct_calls() -> None:
    transform = BM25SearchTransform(always_visible=["kb_health"])

    async def kb_outline() -> dict[str, object]:
        return {"tiers": []}

    hidden_tool = Tool.from_function(kb_outline, name="kb_outline")
    calls: list[tuple[str, object]] = []

    async def call_next(name: str, *, version: object = None) -> Tool | None:
        calls.append((name, version))
        return hidden_tool

    resolved = await transform.get_tool("kb_outline", call_next)

    assert resolved is hidden_tool
    assert calls == [("kb_outline", None)]
