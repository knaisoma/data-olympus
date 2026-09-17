"""Health must not hand pending ids to unauthenticated callers (issue #270).

`GET /api/v1/health`, the degraded 503 body every REST read route can return,
and MCP `kb_health` all carry `path_locks`, one record per held path lock with
`target_path`, `owner_kind`, `pending_id`, `acquired_at` and `age_seconds`.
Neither surface requires authentication even when auth is configured, so an
unauthenticated caller could enumerate pending ids that every other
pending-queue surface (`kb_list_pending`, `kb_get_pending`, `GET
/api/v1/pending`) already requires a token for.

Coverage:
- With auth configured, a caller with no/invalid token gets every path-lock
  field except `pending_id`, over REST /health and MCP kb_health.
- With auth configured, a caller with a valid token gets `pending_id` too.
- With auth not configured, `pending_id` is present regardless (unchanged
  behaviour, matches the pre-auth product).
- The missing/invalid token case does not change the health status code.
- With auth configured, health responses carry Cache-Control: private and
  Vary: Authorization.
"""
from __future__ import annotations

import os
import subprocess

import httpx
import pytest

from data_olympus.server import build_app

TOKEN = "super-secret-health-token-xyz789"


def _init_git(tmp_kb):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=tmp_kb, check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_kb), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_kb), "commit", "-m", "init"], check=True, env=env)


@pytest.fixture
def authed_app(tmp_kb, tmp_index_path, tmp_path):
    """App built with auth_token set, and one held path lock so path_locks is
    non-empty in every response this file checks."""
    _init_git(tmp_kb)
    app = build_app(
        kb_main_path=tmp_kb,
        kb_index_path=tmp_index_path,
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        bootstrap_now=True,
        kb_remote_url="dummy",
        worktree_root=str(tmp_path / "wts"),
        pending_root=str(tmp_path / "pending"),
        push_queue_root=str(tmp_path / "pq"),
        write_block_tiers=[],
        write_block_paths=[],
        auth_token=TOKEN,
    )
    state = app._dolympus_state  # type: ignore[attr-defined]
    state.pending._acquire_lock("projects/foo/bar.md", "pending-abc123")  # noqa: SLF001
    return app


@pytest.fixture
def open_app(tmp_kb, tmp_index_path, tmp_path):
    """App built with auth_token empty (default), one held path lock."""
    _init_git(tmp_kb)
    app = build_app(
        kb_main_path=tmp_kb,
        kb_index_path=tmp_index_path,
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        bootstrap_now=True,
        kb_remote_url="dummy",
        worktree_root=str(tmp_path / "wts"),
        pending_root=str(tmp_path / "pending"),
        push_queue_root=str(tmp_path / "pq"),
        write_block_tiers=[],
        write_block_paths=[],
        # auth_token omitted -> defaults to ""
    )
    state = app._dolympus_state  # type: ignore[attr-defined]
    state.pending._acquire_lock("projects/foo/bar.md", "pending-abc123")  # noqa: SLF001
    return app


@pytest.fixture(autouse=True)
def _git_env(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@e.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@e.com")


# ---------------------------------------------------------------------------
# REST /api/v1/health
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rest_health_no_token_omits_pending_id(authed_app) -> None:
    transport = httpx.ASGITransport(app=authed_app.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/health?verbose=true")
    assert resp.status_code == 200
    locks = resp.json()["path_locks"]
    assert len(locks) == 1
    assert "pending_id" not in locks[0]
    assert locks[0]["target_path"] == "projects/foo/bar.md"
    assert locks[0]["owner_kind"] == "pending"
    assert "acquired_at" in locks[0]
    assert "age_seconds" in locks[0]


@pytest.mark.asyncio
async def test_rest_health_wrong_token_omits_pending_id(authed_app) -> None:
    transport = httpx.ASGITransport(app=authed_app.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/health?verbose=true",
            headers={"Authorization": "Bearer wrong-token"},
        )
    assert resp.status_code == 200
    assert "pending_id" not in resp.json()["path_locks"][0]


@pytest.mark.asyncio
async def test_rest_health_valid_token_includes_pending_id(authed_app) -> None:
    transport = httpx.ASGITransport(app=authed_app.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/health?verbose=true",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert resp.status_code == 200
    assert resp.json()["path_locks"][0]["pending_id"] == "pending-abc123"


@pytest.mark.asyncio
async def test_rest_health_open_app_includes_pending_id_unchanged(open_app) -> None:
    """No auth configured: behaviour matches the pre-auth product exactly."""
    transport = httpx.ASGITransport(app=open_app.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/health?verbose=true")
    assert resp.status_code == 200
    assert resp.json()["path_locks"][0]["pending_id"] == "pending-abc123"


@pytest.mark.asyncio
async def test_rest_health_missing_token_does_not_change_status_code(authed_app) -> None:
    transport = httpx.ASGITransport(app=authed_app.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        no_token = await client.get("/api/v1/health")
        with_token = await client.get(
            "/api/v1/health", headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert no_token.status_code == with_token.status_code == 200


@pytest.mark.asyncio
async def test_rest_health_compact_mode_also_omits_pending_id_without_token(
    authed_app,
) -> None:
    """Compact mode (the default) must not leak through the field-omission
    logic that only drops null/empty values: pending_id is non-null here, so
    the redaction must happen before compact_dump, not rely on it."""
    transport = httpx.ASGITransport(app=authed_app.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    assert "pending_id" not in resp.json()["path_locks"][0]


@pytest.mark.asyncio
async def test_rest_health_auth_configured_sets_cache_headers(authed_app) -> None:
    transport = httpx.ASGITransport(app=authed_app.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/health")
    assert resp.headers.get("cache-control") == "private"
    assert resp.headers.get("vary") == "Authorization"


@pytest.mark.asyncio
async def test_rest_health_no_auth_configured_omits_cache_headers(open_app) -> None:
    """Auth not configured: unchanged behaviour, no new response headers."""
    transport = httpx.ASGITransport(app=open_app.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/health")
    assert "cache-control" not in resp.headers
    assert "vary" not in resp.headers


# ---------------------------------------------------------------------------
# REST degraded 503 body (shared by every read route)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rest_degraded_body_omits_pending_id_without_token(authed_app) -> None:
    state = authed_app._dolympus_state  # type: ignore[attr-defined]
    # last_git_pull_at is None -> the FIRST degraded clause in
    # ServerState/health.py's snapshot() fires unconditionally, no DB mocking
    # needed (matches how test_health.py triggers "degraded").
    state.last_git_pull_at = None
    transport = httpx.ASGITransport(app=authed_app.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/search?q=anything")
    assert resp.status_code == 503
    assert "pending_id" not in resp.json()["path_locks"][0]


# ---------------------------------------------------------------------------
# MCP kb_health
# ---------------------------------------------------------------------------

def _patch_headers(monkeypatch, headers: dict) -> None:
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_http_headers",
        lambda *_a, **_k: headers,
    )


@pytest.mark.asyncio
async def test_mcp_kb_health_no_token_omits_pending_id(authed_app, monkeypatch) -> None:
    from fastmcp import Client

    _patch_headers(monkeypatch, {})
    async with Client(authed_app) as client:
        result = await client.call_tool("kb_health", {"verbose": True})
    locks = result.data["path_locks"]
    assert len(locks) == 1
    assert "pending_id" not in locks[0]


@pytest.mark.asyncio
async def test_mcp_kb_health_wrong_token_omits_pending_id(authed_app, monkeypatch) -> None:
    from fastmcp import Client

    _patch_headers(monkeypatch, {"authorization": "Bearer wrong-token"})
    async with Client(authed_app) as client:
        result = await client.call_tool("kb_health", {"verbose": True})
    assert "pending_id" not in result.data["path_locks"][0]


@pytest.mark.asyncio
async def test_mcp_kb_health_valid_token_includes_pending_id(authed_app, monkeypatch) -> None:
    from fastmcp import Client

    _patch_headers(monkeypatch, {"authorization": f"Bearer {TOKEN}"})
    async with Client(authed_app) as client:
        result = await client.call_tool("kb_health", {"verbose": True})
    assert result.data["path_locks"][0]["pending_id"] == "pending-abc123"


@pytest.mark.asyncio
async def test_mcp_kb_health_open_app_includes_pending_id_unchanged(
    open_app, monkeypatch,
) -> None:
    """No auth configured: behaviour matches the pre-auth product exactly."""
    from fastmcp import Client

    _patch_headers(monkeypatch, {})
    async with Client(open_app) as client:
        result = await client.call_tool("kb_health", {"verbose": True})
    assert result.data["path_locks"][0]["pending_id"] == "pending-abc123"
