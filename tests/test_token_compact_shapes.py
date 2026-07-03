"""Compact-vs-verbose shape tests for the read tools (issue #65).

Two axes are covered:
  - Model level: SearchResponse/GetResponse/ListResponse/OutlineResponse/
    HealthResponse ``compact_dump`` drops exactly the intended fields, keeps the
    payload, and surfaces deviating status; ``verbose`` (model_dump) reproduces
    today's exact shape.
  - Wire level: the MCP tool wrappers (via an in-memory FastMCP Client) and the
    REST handlers (via httpx ASGI) honour ``verbose`` consistently. Compact is
    the default; ``verbose=True`` restores the full shape on both surfaces.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from fastmcp import Client

from data_olympus.index import Index
from data_olympus.server import build_app
from data_olympus.tools_read import (
    kb_get_fn,
    kb_health_fn,
    kb_list_fn,
    kb_outline_fn,
    kb_search_fn,
    shape_response,
)

if TYPE_CHECKING:
    from pathlib import Path


# --------------------------------------------------------------------------
# Model-level compact/verbose shape
# --------------------------------------------------------------------------


def test_search_compact_drops_query_path_score(status_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(status_kb, source_commit="x")
    resp = kb_search_fn(idx=idx, query="caching")
    compact = shape_response(resp, verbose=False)

    assert "query" not in compact, "compact drops the query echo"
    assert compact["source_commit"] == "x"
    assert compact["total_returned"] == len(resp.hits)
    for hit in compact["hits"]:
        assert set(hit) <= {"id", "title", "snippet", "status", "type"}
        assert "path" not in hit, "compact drops per-hit path (fetch via kb_get)"
        assert "score" not in hit, "compact drops the raw bm25 score"


def test_search_compact_surfaces_only_deviating_status(
    status_kb: Path, tmp_index_path: Path
) -> None:
    """In-force hits omit status; a superseded hit KEEPS it (the signal an agent
    must act on)."""
    idx = Index(tmp_index_path)
    idx.build(status_kb, source_commit="x")
    resp = kb_search_fn(idx=idx, query="caching", limit=100)
    compact = shape_response(resp, verbose=False)
    by_id = {h["id"]: h for h in compact["hits"]}

    assert by_id["STD-NEW"].get("status") is None, "active status omitted"
    assert by_id["DEC-1"].get("status") is None, "accepted (in-force) status omitted"
    assert by_id["STD-OLD"]["status"] == "superseded", "deviating status surfaced"


def test_search_verbose_reproduces_full_shape(status_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(status_kb, source_commit="x")
    resp = kb_search_fn(idx=idx, query="caching")
    verbose = shape_response(resp, verbose=True)

    assert verbose == resp.model_dump(), "verbose is byte-for-byte the legacy shape"
    assert verbose["query"] == "caching"
    for hit in verbose["hits"]:
        assert {"id", "path", "title", "snippet", "score", "status", "type"} <= set(hit)


def test_search_compact_caps_snippet() -> None:
    from data_olympus.models import COMPACT_SNIPPET_CHARS, SearchHitModel

    long = "x" * (COMPACT_SNIPPET_CHARS + 500)
    hit = SearchHitModel(id="A", path="a.md", title="A", snippet=long, score=-1.0)
    compact = hit.compact_dump()
    assert len(str(compact["snippet"])) == COMPACT_SNIPPET_CHARS + 1  # +1 for the ellipsis
    assert str(compact["snippet"]).endswith("…")


def test_get_compact_keeps_full_body_trims_envelope(
    tmp_kb: Path, tmp_index_path: Path
) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="abc")
    resp = kb_get_fn(idx=idx, id="STD-U-001")
    compact = shape_response(resp, verbose=False)

    assert compact["content_markdown"] == resp.content_markdown, "full body preserved"
    for dropped in ("path", "git_remote_url", "last_modified_source", "source_commit"):
        assert dropped not in compact
    assert compact["id"] == "STD-U-001"
    assert compact["tier"] == resp.tier


def test_get_verbose_reproduces_full_shape(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="abc")
    resp = kb_get_fn(idx=idx, id="STD-U-001")
    assert shape_response(resp, verbose=True) == resp.model_dump()


def test_list_compact_drops_path(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    resp = kb_list_fn(idx=idx, tier="T1")
    compact = shape_response(resp, verbose=False)

    assert "category" not in compact, "null category omitted"
    for entry in compact["entries"]:
        assert set(entry) == {"id", "title"}
    assert compact["total"] == len(resp.entries)


def test_list_verbose_reproduces_full_shape(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    resp = kb_list_fn(idx=idx, tier="T1", category="foundation")
    verbose = shape_response(resp, verbose=True)
    assert verbose == resp.model_dump()
    assert verbose["category"] == "foundation"
    for entry in verbose["entries"]:
        assert "path" in entry


def test_outline_compact_equals_verbose(tmp_kb: Path, tmp_index_path: Path) -> None:
    """kb_outline is already lean: compact and verbose are the same shape."""
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    resp = kb_outline_fn(idx=idx)
    assert shape_response(resp, verbose=False) == shape_response(resp, verbose=True)
    assert shape_response(resp, verbose=True) == resp.model_dump()


def test_health_compact_omits_nulls_keeps_core(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    resp = kb_health_fn(idx=idx, last_git_pull_at=None, staleness_degraded_sec=600)
    compact = shape_response(resp, verbose=False)

    # Core fields a consumer always branches on are present even when falsy.
    for core in ("kb_commit", "total_rules", "degraded", "db_size_bytes"):
        assert core in compact
    # Null diagnostics are omitted.
    assert "last_index_error" not in compact
    assert "remote_head_sha" not in compact
    assert "last_git_pull_at" not in compact  # None -> omitted


def test_health_verbose_reproduces_full_shape(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    resp = kb_health_fn(idx=idx, last_git_pull_at=None, staleness_degraded_sec=600)
    verbose = shape_response(resp, verbose=True)
    assert verbose == resp.model_dump()
    assert "last_index_error" in verbose  # present as null in verbose


def test_health_compact_keeps_populated_diagnostic() -> None:
    """A populated diagnostic (a real error) survives the null filter."""
    from data_olympus.models import HealthResponse

    h = HealthResponse(
        kb_commit="c", index_built_at=1.0, total_rules=3, last_git_pull_at=None,
        staleness_seconds=None, degraded=True, db_size_bytes=10,
        last_index_build_status="failed", last_index_error="dup id",
    )
    compact = h.compact_dump()
    assert compact["last_index_build_status"] == "failed"
    assert compact["last_index_error"] == "dup id"


# --------------------------------------------------------------------------
# Wire level: MCP tool wrappers
# --------------------------------------------------------------------------


def _app(tmp_kb: Path, tmp_path: Path):
    return build_app(
        kb_main_path=tmp_kb,
        kb_index_path=tmp_path / "idx.db",
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        bootstrap_now=True,
    )


@pytest.mark.asyncio
async def test_mcp_search_compact_default_and_verbose(tmp_kb: Path, tmp_path: Path) -> None:
    app = _app(tmp_kb, tmp_path)
    async with Client(app) as client:
        compact = (await client.call_tool("kb_search", {"query": "worktree"})).data
        verbose = (
            await client.call_tool("kb_search", {"query": "worktree", "verbose": True})
        ).data

    assert "query" not in compact
    assert all("path" not in h and "score" not in h for h in compact["hits"])
    assert verbose["query"] == "worktree"
    assert all("path" in h and "score" in h for h in verbose["hits"])


@pytest.mark.asyncio
async def test_mcp_get_compact_default_and_verbose(tmp_kb: Path, tmp_path: Path) -> None:
    app = _app(tmp_kb, tmp_path)
    async with Client(app) as client:
        compact = (await client.call_tool("kb_get", {"id": "STD-U-001"})).data
        verbose = (
            await client.call_tool("kb_get", {"id": "STD-U-001", "verbose": True})
        ).data

    assert "path" not in compact
    assert compact["content_markdown"]  # body kept
    assert "path" in verbose and "source_commit" in verbose


@pytest.mark.asyncio
async def test_mcp_list_and_health_compact_default(tmp_kb: Path, tmp_path: Path) -> None:
    app = _app(tmp_kb, tmp_path)
    async with Client(app) as client:
        lst = (await client.call_tool("kb_list", {"tier": "T1"})).data
        health = (await client.call_tool("kb_health", {})).data
        health_v = (await client.call_tool("kb_health", {"verbose": True})).data

    assert all("path" not in e for e in lst["entries"])
    assert "last_index_error" not in health
    assert "last_index_error" in health_v


# --------------------------------------------------------------------------
# Wire level: REST handlers
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_read_tools_honour_verbose(tmp_kb: Path, tmp_path: Path) -> None:
    app = _app(tmp_kb, tmp_path)
    transport = httpx.ASGITransport(app=app.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        s_compact = (await client.get("/api/v1/search", params={"q": "worktree"})).json()
        s_verbose = (
            await client.get("/api/v1/search", params={"q": "worktree", "verbose": "true"})
        ).json()
        g_compact = (await client.get("/api/v1/get/STD-U-001")).json()
        g_verbose = (
            await client.get("/api/v1/get/STD-U-001", params={"verbose": "true"})
        ).json()
        l_compact = (await client.get("/api/v1/list", params={"tier": "T1"})).json()
        l_verbose = (
            await client.get("/api/v1/list", params={"tier": "T1", "verbose": "true"})
        ).json()
        h_compact = (await client.get("/api/v1/health")).json()
        h_verbose = (await client.get("/api/v1/health", params={"verbose": "true"})).json()

    assert "query" not in s_compact and s_verbose["query"] == "worktree"
    assert all("path" not in h for h in s_compact["hits"])
    assert all("path" in h for h in s_verbose["hits"])

    assert "path" not in g_compact and "path" in g_verbose
    assert g_compact["content_markdown"]

    assert all("path" not in e for e in l_compact["entries"])
    assert all("path" in e for e in l_verbose["entries"])

    assert "last_index_error" not in h_compact
    assert "last_index_error" in h_verbose
