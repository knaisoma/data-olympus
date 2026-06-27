"""REST tests for /api/v1/audit."""
from __future__ import annotations

import os
import subprocess

import httpx
import pytest

from data_olympus.server import build_app


@pytest.fixture
def http_app(tmp_kb, tmp_index_path, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@e.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@e.com")
    env = {**os.environ}
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=tmp_kb, check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_kb), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_kb), "commit", "-m", "init"], check=True, env=env)
    app = build_app(
        kb_main_path=tmp_kb, kb_index_path=tmp_index_path,
        sync_interval_sec=60, staleness_degraded_sec=600, bootstrap_now=True,
        kb_remote_url="dummy",
        worktree_root=str(tmp_path / "wts"),
        pending_root=str(tmp_path / "pending"),
        push_queue_root=str(tmp_path / "pq"),
        audit_log_path=str(tmp_path / "audit.log"),
        write_block_tiers=[],
        write_block_paths=[],
    )
    return app.http_app()


@pytest.mark.asyncio
async def test_rest_audit_returns_events(http_app) -> None:
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/api/v1/propose/memory",
            json={"text": "x", "tags": [], "source_session": "s",
                  "agent_identity": "claude", "confidence": 0.9})
        resp = await client.get("/api/v1/audit")
    assert resp.status_code == 200
    body = resp.json()
    assert body["returned"] >= 1


@pytest.fixture
def authed_app(tmp_kb, tmp_index_path, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@e.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@e.com")
    env = {**os.environ}
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=tmp_kb, check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_kb), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_kb), "commit", "-m", "init"], check=True, env=env)
    app = build_app(
        kb_main_path=tmp_kb, kb_index_path=tmp_index_path,
        sync_interval_sec=60, staleness_degraded_sec=600, bootstrap_now=True,
        kb_remote_url="dummy", worktree_root=str(tmp_path / "wts"),
        pending_root=str(tmp_path / "pending"), push_queue_root=str(tmp_path / "pq"),
        audit_log_path=str(tmp_path / "audit.log"), auth_token="tok",
    )
    return app.http_app()


@pytest.mark.asyncio
async def test_audit_and_pending_gated_when_auth_configured(authed_app) -> None:
    """Observability routes require a token when KB_AUTH_TOKEN is set."""
    transport = httpx.ASGITransport(app=authed_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/v1/audit")).status_code == 401
        assert (await client.get("/api/v1/pending")).status_code == 401
        assert (await client.get("/api/v1/audit/verify")).status_code == 401
        h = {"Authorization": "Bearer tok"}
        assert (await client.get("/api/v1/audit", headers=h)).status_code == 200
        assert (await client.get("/api/v1/pending", headers=h)).status_code == 200
        assert (await client.get("/api/v1/audit/verify", headers=h)).status_code == 200


@pytest.mark.asyncio
async def test_rest_audit_verify_ok(http_app) -> None:
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(3):
            await client.post("/api/v1/propose/memory",
                json={"text": "x", "tags": [], "source_session": "s",
                      "agent_identity": "claude", "confidence": 0.9})
        resp = await client.get("/api/v1/audit/verify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["first_broken_index"] == -1
