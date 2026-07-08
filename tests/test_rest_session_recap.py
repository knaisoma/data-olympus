"""REST tests for /api/v1/session-recap (issue #112)."""
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
async def test_rest_session_recap_counts_writes(http_app) -> None:
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/api/v1/propose/memory",
            json={"text": "x", "tags": [], "source_session": "recap-session",
                  "agent_identity": "claude", "confidence": 0.9})
        await client.post("/api/v1/propose/memory",
            json={"text": "y", "tags": [], "source_session": "recap-session",
                  "agent_identity": "claude", "confidence": 0.1})
        resp = await client.get(
            "/api/v1/session-recap", params={"source_session": "recap-session"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["source_session"] == "recap-session"
    assert body["committed"] == 1
    assert body["demoted_to_pending"] == 1
    assert body["rejected"] == 0


@pytest.mark.asyncio
async def test_rest_session_recap_requires_source_session(http_app) -> None:
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/session-recap")
    assert resp.status_code == 400


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
async def test_rest_session_recap_gated_when_auth_configured(authed_app) -> None:
    transport = httpx.ASGITransport(app=authed_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/session-recap", params={"source_session": "s"},
        )
        assert resp.status_code == 401
        h = {"Authorization": "Bearer tok"}
        resp = await client.get(
            "/api/v1/session-recap", params={"source_session": "s"}, headers=h,
        )
        assert resp.status_code == 200
