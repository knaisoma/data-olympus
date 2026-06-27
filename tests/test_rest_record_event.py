"""REST /api/v1/audit/event."""
from __future__ import annotations

import os
import subprocess

import httpx
import pytest

from data_olympus.server import build_app


@pytest.fixture
def http_app(tmp_kb, tmp_index_path, tmp_path, monkeypatch):
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "t")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "t@e.com")
    env = {**os.environ}
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=tmp_kb, check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_kb), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_kb), "commit", "-m", "init"], check=True, env=env)
    app = build_app(
        kb_main_path=tmp_kb, kb_index_path=tmp_index_path,
        sync_interval_sec=60, staleness_degraded_sec=600, bootstrap_now=True,
        kb_remote_url="dummy", worktree_root=str(tmp_path / "wts"),
        pending_root=str(tmp_path / "pending"), push_queue_root=str(tmp_path / "pq"),
        audit_log_path=str(tmp_path / "audit.log"))
    return app.http_app()


@pytest.mark.asyncio
async def test_record_event_then_compliance(http_app) -> None:
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/api/v1/audit/event", json={
            "event_type": "gate_bypass", "workspace": "proj",
            "agent_identity": "codex", "source_session": "s", "reason": "x"})
        assert r.status_code == 200
        c = await client.get("/api/v1/compliance")
    assert c.json()["counts"].get("gate_bypass", 0) >= 1


@pytest.mark.asyncio
async def test_record_event_rejects_bad_type(http_app) -> None:
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/api/v1/audit/event", json={
            "event_type": "consult", "workspace": "proj",
            "agent_identity": "x", "source_session": "s", "reason": ""})
    assert r.status_code == 400
