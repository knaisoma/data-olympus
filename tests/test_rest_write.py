"""REST tests for write endpoints."""
from __future__ import annotations

import os
import subprocess

import httpx
import pytest

from data_olympus.server import build_app


@pytest.fixture
def http_app(tmp_kb, tmp_index_path, tmp_path):
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
        kb_remote_url="dummy",  # enables write-side wiring
        worktree_root=str(tmp_path / "wts"),
        pending_root=str(tmp_path / "pending"),
        push_queue_root=str(tmp_path / "pq"),
        write_block_tiers=[],
        write_block_paths=[],
    )
    return app.http_app()


@pytest.fixture(autouse=True)
def _git_env(monkeypatch):
    """Ensure git commits inside write fns have author info."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@e.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@e.com")


@pytest.mark.asyncio
async def test_rest_propose_memory_high_conf_returns_201(http_app) -> None:
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/propose/memory",
            json={"text": "test", "tags": [], "source_session": "s",
                  "agent_identity": "claude", "confidence": 0.9},
        )
    assert resp.status_code in (200, 201)
    body = resp.json()
    assert body["status"] == "committed"


@pytest.mark.asyncio
async def test_rest_propose_edit_rejects_traversal(http_app) -> None:
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/propose/edit",
            json={
                "target_path": "projects/foo/../operator/x.md",
                "postimage": "x", "base_commit": "HEAD",
                "base_blob_sha": None, "target_file_hash": None,
                "reason": "test", "source_session": "s",
                "agent_identity": "claude", "confidence": 0.9,
            },
        )
    body = resp.json()
    assert body["status"] == "rejected_path_not_indexable"


@pytest.mark.asyncio
async def test_rest_propose_edit_missing_base_commit_returns_400(http_app) -> None:
    """A missing required field must yield a clean 400, never a 500/KeyError.

    Regression: the handler accessed body["base_commit"] directly, so a body
    without it raised KeyError -> HTTP 500 with a plain-text "Internal Server
    Error" that the kb CLI then fed to jq, aborting with a parse error.
    """
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/propose/edit",
            json={
                "target_path": "universal/foundation/STD-U-001.md",
                "postimage": "x",
                # base_commit intentionally omitted
                "reason": "test", "source_session": "s",
                "agent_identity": "claude", "confidence": 0.9,
            },
        )
    assert resp.status_code == 400
    body = resp.json()
    assert "base_commit" in f"{body.get('error', '')} {body.get('message', '')}"


@pytest.mark.asyncio
async def test_rest_propose_edit_null_base_commit_returns_400(http_app) -> None:
    """An explicit JSON null for a required field is treated as missing -> 400,
    not passed through as None (which would crash deeper in the write path)."""
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/propose/edit",
            json={"target_path": "universal/foundation/STD-U-001.md",
                  "postimage": "x", "base_commit": None,
                  "reason": "t", "source_session": "s",
                  "agent_identity": "claude", "confidence": 0.9},
        )
    assert resp.status_code == 400
    body = resp.json()
    assert "base_commit" in f"{body.get('error', '')} {body.get('message', '')}"


@pytest.mark.asyncio
async def test_rest_propose_edit_non_numeric_confidence_returns_400(http_app) -> None:
    """A non-numeric confidence yields a clean 400, not a 500 from float()."""
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/propose/edit",
            json={"target_path": "universal/foundation/STD-U-001.md",
                  "postimage": "x", "base_commit": "HEAD",
                  "reason": "t", "source_session": "s",
                  "agent_identity": "claude", "confidence": "high"},
        )
    assert resp.status_code == 400
    body = resp.json()
    assert "confidence" in f"{body.get('error', '')} {body.get('message', '')}"


@pytest.mark.asyncio
async def test_rest_propose_memory_missing_text_returns_400(http_app) -> None:
    """propose/memory must also 400 (not 500) on a missing required field."""
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/propose/memory",
            json={"tags": [], "source_session": "s",
                  "agent_identity": "claude", "confidence": 0.9},
        )
    assert resp.status_code == 400
    body = resp.json()
    assert "text" in f"{body.get('error', '')} {body.get('message', '')}"


@pytest.mark.asyncio
async def test_rest_list_pending_returns_entries(http_app) -> None:
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Create one pending.
        await client.post(
            "/api/v1/propose/memory",
            json={"text": "x", "tags": [], "source_session": "s",
                  "agent_identity": "claude", "confidence": 0.3},
        )
        resp = await client.get("/api/v1/pending")
    body = resp.json()
    assert len(body["pending"]) == 1
