"""End-to-end acceptance test for the hardened enforcement + write flow.

Exercises, against an in-process app, the chain the security review asked to be
guarded by an acceptance test: consult -> gate block -> consult -> gate allow,
the high/low-confidence write paths, and an auth-gated write.
"""
from __future__ import annotations

import os
import subprocess

import httpx
import pytest

from data_olympus.server import build_app


def _git(tmp_kb):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=tmp_kb, check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_kb), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_kb), "commit", "-m", "init"], check=True, env=env)


@pytest.fixture(autouse=True)
def _git_env(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@e.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@e.com")


@pytest.fixture
def app(tmp_kb, tmp_index_path, tmp_path):
    _git(tmp_kb)
    application = build_app(
        kb_main_path=tmp_kb, kb_index_path=tmp_index_path,
        sync_interval_sec=60, staleness_degraded_sec=600, bootstrap_now=True,
        kb_remote_url="dummy", worktree_root=str(tmp_path / "wts"),
        pending_root=str(tmp_path / "pending"), push_queue_root=str(tmp_path / "pq"),
        audit_log_path=str(tmp_path / "audit.log"),
    )
    return application.http_app()


@pytest.mark.asyncio
async def test_gate_blocks_then_allows_after_consult(app) -> None:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        gate_body = {"workspace": "ws1", "session_id": "sess1",
                     "tool_name": "Edit", "action_path": "pyproject.toml"}
        # 1. Governed action with no consult on record -> blocked.
        r1 = await client.post("/api/v1/gate/check", json=gate_body)
        assert r1.status_code == 200
        assert r1.json()["verdict"] == "consult_required"
        # 2. Record a consultation for the same (session, workspace).
        r2 = await client.post("/api/v1/consult", json={
            "workspace": "ws1", "source_session": "sess1",
            "intent": "change a dependency in pyproject.toml", "agent_identity": "claude"})
        assert r2.status_code == 200
        # 3. Same governed action now allowed.
        r3 = await client.post("/api/v1/gate/check", json=gate_body)
        assert r3.json()["verdict"] == "allow"


@pytest.mark.asyncio
async def test_high_and_low_confidence_write_paths(app) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        high = await client.post("/api/v1/propose/memory", json={
            "text": "high conf", "tags": [], "source_session": "s",
            "agent_identity": "claude", "confidence": 0.95})
        assert high.status_code == 201
        assert high.json()["status"] == "committed"
        low = await client.post("/api/v1/propose/memory", json={
            "text": "low conf", "tags": [], "source_session": "s",
            "agent_identity": "claude", "confidence": 0.4})
        assert low.status_code == 202
        assert low.json()["status"] == "pending_confirmation"


@pytest.mark.asyncio
async def test_auth_gated_write_blocks_anonymous(tmp_kb, tmp_index_path, tmp_path) -> None:
    _git(tmp_kb)
    app = build_app(
        kb_main_path=tmp_kb, kb_index_path=tmp_index_path,
        sync_interval_sec=60, staleness_degraded_sec=600, bootstrap_now=True,
        kb_remote_url="dummy", worktree_root=str(tmp_path / "wts"),
        pending_root=str(tmp_path / "pending"), push_queue_root=str(tmp_path / "pq"),
        auth_token="acc-token",
    ).http_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        anon = await client.post("/api/v1/propose/memory", json={
            "text": "x", "tags": [], "source_session": "s",
            "agent_identity": "claude", "confidence": 0.95})
        assert anon.status_code == 401
        authed = await client.post("/api/v1/propose/memory",
            headers={"Authorization": "Bearer acc-token"},
            json={"text": "x", "tags": [], "source_session": "s",
                  "agent_identity": "claude", "confidence": 0.95})
        assert authed.status_code == 201
