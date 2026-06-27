# tests/test_rest_enforce.py
"""REST tests for the enforcement endpoints."""
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
    )
    return app.http_app()


@pytest.mark.asyncio
async def test_consult_then_gate_allows(http_app) -> None:
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        c = await client.post("/api/v1/consult", json={
            "workspace": "proj", "intent": "add a new caching library",
            "source_session": "s1", "agent_identity": "claude"})
        assert c.status_code == 200
        g = await client.post("/api/v1/gate/check", json={
            "workspace": "proj", "session_id": "s1", "tool_name": "Edit",
            "action_path": "/p/pyproject.toml"})
    assert g.json()["verdict"] == "allow"


@pytest.mark.asyncio
async def test_gate_without_consult_requires_consult(http_app) -> None:
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        g = await client.post("/api/v1/gate/check", json={
            "workspace": "proj", "session_id": "fresh", "tool_name": "Edit",
            "action_path": "/p/pyproject.toml"})
    assert g.json()["verdict"] == "consult_required"


@pytest.mark.asyncio
async def test_compliance_reports_events(http_app) -> None:
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/api/v1/consult", json={
            "workspace": "proj", "intent": "migration plan",
            "source_session": "s1", "agent_identity": "claude"})
        r = await client.get("/api/v1/compliance")
    assert r.json()["counts"].get("consult", 0) >= 1
