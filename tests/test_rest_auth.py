"""Tests for optional bearer-token auth on write routes (KB_AUTH_TOKEN).

Coverage:
- When auth_token is set, write POSTs with no Authorization header return 401.
- When auth_token is set, write POSTs with the wrong token return 401.
- When auth_token is set, write POSTs with the correct Bearer token succeed
  (parameterized over all four write routes).
- When auth_token is empty (default), write POSTs with no header still work
  (backward-compat; parameterized over all four write routes).
- Read routes (GET) are never gated regardless of auth_token.
"""
from __future__ import annotations

import os
import subprocess

import httpx
import pytest

from data_olympus.server import build_app

TOKEN = "super-secret-test-token-abc123"


@pytest.fixture
def authed_app(tmp_kb, tmp_index_path, tmp_path):
    """App built with auth_token set."""
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=tmp_kb, check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_kb), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_kb), "commit", "-m", "init"], check=True, env=env)

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
    return app.http_app()


@pytest.fixture
def open_app(tmp_kb, tmp_index_path, tmp_path):
    """App built with auth_token empty (default, backward-compat)."""
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=tmp_kb, check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_kb), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_kb), "commit", "-m", "init"], check=True, env=env)

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
        # auth_token omitted — defaults to ""
    )
    return app.http_app()


@pytest.fixture(autouse=True)
def _git_env(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@e.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@e.com")


# ---------------------------------------------------------------------------
# propose/memory — no header
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_propose_memory_no_auth_header_returns_401(authed_app) -> None:
    transport = httpx.ASGITransport(app=authed_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/propose/memory",
            json={"text": "x", "tags": [], "source_session": "s",
                  "agent_identity": "claude", "confidence": 0.9},
        )
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


# ---------------------------------------------------------------------------
# propose/memory — wrong token
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_propose_memory_wrong_token_returns_401(authed_app) -> None:
    transport = httpx.ASGITransport(app=authed_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/propose/memory",
            headers={"Authorization": "Bearer wrong-token"},
            json={"text": "x", "tags": [], "source_session": "s",
                  "agent_identity": "claude", "confidence": 0.9},
        )
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


# ---------------------------------------------------------------------------
# propose/memory — correct token
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_propose_memory_correct_token_succeeds(authed_app) -> None:
    transport = httpx.ASGITransport(app=authed_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/propose/memory",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"text": "x", "tags": [], "source_session": "s",
                  "agent_identity": "claude", "confidence": 0.9},
        )
    assert resp.status_code in (200, 201)
    assert resp.json()["status"] in ("committed", "pending_confirmation")


# ---------------------------------------------------------------------------
# propose/edit — no header + wrong token
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_propose_edit_no_auth_header_returns_401(authed_app) -> None:
    transport = httpx.ASGITransport(app=authed_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/propose/edit",
            json={
                "target_path": "projects/foo/bar.md",
                "postimage": "# x", "base_commit": "HEAD",
                "base_blob_sha": None, "target_file_hash": None,
                "reason": "test", "source_session": "s",
                "agent_identity": "claude", "confidence": 0.9,
            },
        )
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


@pytest.mark.asyncio
async def test_propose_edit_wrong_token_returns_401(authed_app) -> None:
    transport = httpx.ASGITransport(app=authed_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/propose/edit",
            headers={"Authorization": "Bearer bad"},
            json={
                "target_path": "projects/foo/bar.md",
                "postimage": "# x", "base_commit": "HEAD",
                "base_blob_sha": None, "target_file_hash": None,
                "reason": "test", "source_session": "s",
                "agent_identity": "claude", "confidence": 0.9,
            },
        )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# resolve — no header + wrong token
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_no_auth_header_returns_401(authed_app) -> None:
    transport = httpx.ASGITransport(app=authed_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/resolve/nonexistent-id",
            json={"decision": "approve"},
        )
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


@pytest.mark.asyncio
async def test_resolve_wrong_token_returns_401(authed_app) -> None:
    transport = httpx.ASGITransport(app=authed_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/resolve/nonexistent-id",
            headers={"Authorization": "Bearer wrong"},
            json={"decision": "approve"},
        )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# onboarding/bootstrap — no header + wrong token
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_onboarding_bootstrap_no_auth_header_returns_401(authed_app) -> None:
    transport = httpx.ASGITransport(app=authed_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/onboarding/bootstrap",
            json={
                "workspace": "myws",
                "files": [],
                "source_session": "s",
                "agent_identity": "claude",
                "confidence": 0.9,
            },
        )
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


@pytest.mark.asyncio
async def test_onboarding_bootstrap_wrong_token_returns_401(authed_app) -> None:
    transport = httpx.ASGITransport(app=authed_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/onboarding/bootstrap",
            headers={"Authorization": "Bearer bad"},
            json={
                "workspace": "myws",
                "files": [],
                "source_session": "s",
                "agent_identity": "claude",
                "confidence": 0.9,
            },
        )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Backward-compat: no auth_token set → write routes open
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_auth_token_write_route_works_without_header(open_app) -> None:
    """When auth_token is empty, write routes must remain open (backward-compat)."""
    transport = httpx.ASGITransport(app=open_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/propose/memory",
            json={"text": "x", "tags": [], "source_session": "s",
                  "agent_identity": "claude", "confidence": 0.9},
        )
    assert resp.status_code in (200, 201)
    assert resp.json()["status"] in ("committed", "pending_confirmation")


# ---------------------------------------------------------------------------
# Read routes are never gated
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_read_open_when_auth_token_set(authed_app) -> None:
    """GET /api/v1/health must be accessible without any token."""
    transport = httpx.ASGITransport(app=authed_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/health")
    assert resp.status_code in (200, 503)
    assert "error" not in resp.json() or resp.json().get("error") != "unauthorized"


@pytest.mark.asyncio
async def test_search_read_open_when_auth_token_set(authed_app) -> None:
    """GET /api/v1/search must be accessible without any token."""
    transport = httpx.ASGITransport(app=authed_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/search?q=test")
    assert resp.status_code != 401


# ---------------------------------------------------------------------------
# Helpers for parameterized write-route coverage
# ---------------------------------------------------------------------------

# Each entry: (route, payload).
# For resolve we use a nonexistent id — server returns non-401 (e.g. 404).
# For onboarding/bootstrap an empty files list is valid enough to pass auth.
_WRITE_ROUTES = [
    (
        "/api/v1/propose/memory",
        {"text": "param-test", "tags": [], "source_session": "s",
         "agent_identity": "claude", "confidence": 0.9},
    ),
    (
        "/api/v1/propose/edit",
        {
            "target_path": "projects/foo/bar.md",
            "postimage": "# x",
            "base_commit": "HEAD",
            "base_blob_sha": None,
            "target_file_hash": None,
            "reason": "test",
            "source_session": "s",
            "agent_identity": "claude",
            "confidence": 0.9,
        },
    ),
    (
        "/api/v1/resolve/nonexistent-param-id",
        {"decision": "approve"},
    ),
    (
        "/api/v1/onboarding/bootstrap",
        {
            "workspace": "param-ws",
            "files": [],
            "source_session": "s",
            "agent_identity": "claude",
            "confidence": 0.9,
        },
    ),
]


# ---------------------------------------------------------------------------
# Parameterized: correct token → not 401 (all four write routes)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("route,payload", _WRITE_ROUTES)
async def test_correct_token_not_401_on_write_routes(authed_app, route, payload) -> None:
    """A correct Bearer token must not be rejected (not 401) on any write route.

    Uses raise_app_exceptions=False so that business-logic errors (e.g.
    resolve on a nonexistent pending id) surface as 5xx responses rather than
    propagating as Python exceptions — the test only cares that the auth gate
    did not fire (not 401).
    """
    transport = httpx.ASGITransport(app=authed_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            route,
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=payload,
        )
    assert resp.status_code != 401, (
        f"Expected non-401 with correct token on {route}, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# Parameterized: no auth_token set → write routes open (all four)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("route,payload", _WRITE_ROUTES)
async def test_open_app_write_routes_no_header_not_401(open_app, route, payload) -> None:
    """When auth_token is empty, write routes must not return 401 (backward-compat).

    Uses raise_app_exceptions=False for the same reason as the correct-token
    parameterized test: business-logic errors for resolve/bootstrap surface as
    5xx, not 401, which is the desired outcome.
    """
    transport = httpx.ASGITransport(app=open_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(route, json=payload)
    assert resp.status_code != 401, (
        f"Expected open (non-401) on {route} without auth_token, got {resp.status_code}"
    )
