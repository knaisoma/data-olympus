"""REST mirror tests via httpx ASGI client against the FastMCP HTTP app."""
from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from data_olympus.server import build_app

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def http_app(tmp_kb: Path, tmp_path: Path):
    """A FastMCP app with index pre-built. Returns the underlying Starlette HTTP app."""
    app = build_app(
        kb_main_path=tmp_kb,
        kb_index_path=tmp_path / "idx.db",
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        bootstrap_now=True,
    )
    # FastMCP 3.x exposes the underlying Starlette HTTP app via http_app()
    return app.http_app()


@pytest.mark.asyncio
async def test_rest_health_returns_200(http_app) -> None:
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert "kb_commit" in body
    assert "total_rules" in body
    assert body["total_rules"] >= 1


@pytest.mark.asyncio
async def test_rest_outline_returns_200(http_app) -> None:
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/outline")
    assert resp.status_code == 200
    assert "tiers" in resp.json()


@pytest.mark.asyncio
async def test_rest_search_returns_hits(http_app) -> None:
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/search", params={"q": "worktree", "limit": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_returned"] >= 1
    # DEVIATION: plan asserts hits[0]==STD-U-001 but the tmp_kb fixture also has
    # tooling/worktrees.md whose id+title contain "worktree" and outrank STD-U-001
    # on FTS BM25. Use the same "is in hits" convention as test_index.py.
    assert any(h["id"] == "STD-U-001" for h in body["hits"])


@pytest.mark.asyncio
async def test_rest_get_by_id_returns_full_doc(http_app) -> None:
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/get/STD-U-001")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "STD-U-001"
    assert "worktree" in body["content_markdown"]


@pytest.mark.asyncio
async def test_rest_get_missing_returns_404(http_app) -> None:
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/get/STD-DOES-NOT-EXIST")
    assert resp.status_code == 404
    body = resp.json()
    assert body.get("error") == "not_found"


@pytest.mark.asyncio
async def test_rest_list_filters(http_app) -> None:
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/list", params={"tier": "T1", "category": "foundation"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["tier"] == "T1"
    assert body["category"] == "foundation"
    assert body["entries"]


@pytest.mark.asyncio
async def test_rest_list_t2_stack_backend_nestjs(http_app) -> None:
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/list",
            params={"tier": "T2", "category": "stack:backend-nestjs"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tier"] == "T2"
    assert body["category"] == "stack:backend-nestjs"
    assert any(e["id"] == "STD-BN-001" for e in body["entries"])


@pytest.mark.asyncio
async def test_rest_list_missing_tier_returns_400(http_app) -> None:
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/list")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_rest_health_returns_503_when_index_build_failed(
    tmp_kb: Path, tmp_path: Path
) -> None:
    """When ServerState.last_index_build_status == 'failed', /api/v1/health must
    return HTTP 503 with degraded:true body."""
    from data_olympus.server import build_app
    app = build_app(
        kb_main_path=tmp_kb,
        kb_index_path=tmp_path / "idx.db",
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        bootstrap_now=True,
    )
    state = app._dolympus_state  # type: ignore[attr-defined]
    state.last_index_build_status = "failed"
    state.last_index_error = "duplicate id STD-U-007"
    state.last_index_conflicts = [{"id": "STD-U-007", "paths": ["a", "b"]}]

    http_app = app.http_app()
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/health")
    assert resp.status_code == 503, f"expected 503, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["degraded"] is True
    assert body["last_index_build_status"] == "failed"


@pytest.mark.asyncio
async def test_rest_health_returns_503_when_no_pull_recorded(
    tmp_kb: Path, tmp_path: Path
) -> None:
    """When last_git_pull_at is None (no successful pull yet), health is degraded -> 503."""
    from data_olympus.server import build_app
    app = build_app(
        kb_main_path=tmp_kb,
        kb_index_path=tmp_path / "idx.db",
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        bootstrap_now=True,
    )
    state = app._dolympus_state  # type: ignore[attr-defined]
    state.last_git_pull_at = None  # simulate "never pulled"

    http_app = app.http_app()
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/health")
    assert resp.status_code == 503
    assert resp.json()["degraded"] is True


# -------------------------------------------------------------------------
# Degraded -> 503 on ALL read endpoints.
# -------------------------------------------------------------------------


def _make_degraded_app(tmp_kb: Path, tmp_path: Path):
    """Helper: build_app() then force ServerState into a degraded state."""
    from data_olympus.server import build_app
    app = build_app(
        kb_main_path=tmp_kb,
        kb_index_path=tmp_path / "idx.db",
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        bootstrap_now=True,
    )
    state = app._dolympus_state  # type: ignore[attr-defined]
    state.last_index_build_status = "failed"
    state.last_index_error = "duplicate id STD-U-007"
    return app.http_app()


@pytest.mark.asyncio
async def test_rest_outline_returns_503_when_degraded(tmp_kb: Path, tmp_path: Path) -> None:
    http_app = _make_degraded_app(tmp_kb, tmp_path)
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/outline")
    assert resp.status_code == 503
    assert resp.json()["degraded"] is True


@pytest.mark.asyncio
async def test_rest_search_returns_503_when_degraded(tmp_kb: Path, tmp_path: Path) -> None:
    http_app = _make_degraded_app(tmp_kb, tmp_path)
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/search", params={"q": "worktree"})
    assert resp.status_code == 503
    assert resp.json()["degraded"] is True


@pytest.mark.asyncio
async def test_rest_get_returns_503_when_degraded(tmp_kb: Path, tmp_path: Path) -> None:
    http_app = _make_degraded_app(tmp_kb, tmp_path)
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/get/STD-U-001")
    assert resp.status_code == 503
    assert resp.json()["degraded"] is True


@pytest.mark.asyncio
async def test_rest_list_returns_503_when_degraded(tmp_kb: Path, tmp_path: Path) -> None:
    http_app = _make_degraded_app(tmp_kb, tmp_path)
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/list", params={"tier": "T1"})
    assert resp.status_code == 503
    assert resp.json()["degraded"] is True
