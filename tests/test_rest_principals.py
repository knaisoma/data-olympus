"""REST end-to-end tests for per-agent principals: capability 403s and the
confidence clamp (a propose-only principal cannot auto-commit)."""
from __future__ import annotations

import os
import subprocess

import httpx
import pytest

from data_olympus.server import build_app

OPERATOR = "operator-token"
PRINCIPALS = [
    {"name": "proposer", "token": "ptok", "capabilities": ["read", "propose"]},
    {"name": "reader", "token": "rtok", "capabilities": ["read"]},
]


@pytest.fixture(autouse=True)
def _git_env(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@e.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@e.com")


@pytest.fixture
def app(tmp_kb, tmp_index_path, tmp_path):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=tmp_kb, check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_kb), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_kb), "commit", "-m", "init"], check=True, env=env)
    application = build_app(
        kb_main_path=tmp_kb, kb_index_path=tmp_index_path,
        sync_interval_sec=60, staleness_degraded_sec=600, bootstrap_now=True,
        kb_remote_url="dummy", worktree_root=str(tmp_path / "wts"),
        pending_root=str(tmp_path / "pending"), push_queue_root=str(tmp_path / "pq"),
        auth_token=OPERATOR, auth_principals=PRINCIPALS,
    )
    return application.http_app()


async def _post(app, route, token, payload):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            route, headers={"Authorization": f"Bearer {token}"}, json=payload,
        )


_MEMORY = {"text": "x", "tags": [], "source_session": "s",
           "agent_identity": "claude", "confidence": 0.95}


@pytest.mark.asyncio
async def test_operator_high_confidence_auto_commits(app) -> None:
    resp = await _post(app, "/api/v1/propose/memory", OPERATOR, _MEMORY)
    assert resp.status_code == 201
    assert resp.json()["status"] == "committed"


@pytest.mark.asyncio
async def test_proposer_high_confidence_is_clamped_to_pending(app) -> None:
    """A principal with propose but not auto_commit cannot self-assert its way to
    an auto-commit: the high-confidence proposal is parked as pending."""
    resp = await _post(app, "/api/v1/propose/memory", "ptok", _MEMORY)
    assert resp.status_code == 202
    assert resp.json()["status"] == "pending_confirmation"


@pytest.mark.asyncio
async def test_reader_cannot_propose_gets_403(app) -> None:
    resp = await _post(app, "/api/v1/propose/memory", "rtok", _MEMORY)
    assert resp.status_code == 403
    assert resp.json()["error"] == "forbidden"


@pytest.mark.asyncio
async def test_proposer_cannot_resolve_gets_403(app) -> None:
    resp = await _post(app, "/api/v1/resolve/some-id", "ptok", {"decision": "approve"})
    assert resp.status_code == 403
