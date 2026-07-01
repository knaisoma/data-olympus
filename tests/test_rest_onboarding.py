"""REST tests for onboarding endpoints."""
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
        write_block_tiers=[], write_block_paths=[],
    )
    return app.http_app()


@pytest.mark.asyncio
async def test_rest_onboarding_status_returns_absent_for_new(http_app) -> None:
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/onboarding/status?workspace=newproj")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "absent"


@pytest.mark.asyncio
async def test_rest_onboarding_status_returns_onboarded_for_example_project(http_app) -> None:
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/onboarding/status?workspace=example-project")
    assert resp.status_code == 200
    body = resp.json()
    # Fixture has projects/example-project/README.md but no AGENTS.md, so partial.
    assert body["state"] in ("onboarded", "partial")


@pytest.mark.asyncio
async def test_rest_cleanup_plan_returns_200_and_classifies(http_app) -> None:
    body = {
        "workspace": "foo",
        "component": None,
        "local_files": [{"path": "README.md", "content": "# X\n\nunique local text\n"}],
    }
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/v1/onboarding/cleanup-plan", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data and "summary" in data
    assert len(data["items"]) == 1


@pytest.mark.asyncio
async def test_rest_cleanup_plan_local_files_not_a_list_returns_400(http_app) -> None:
    body = {
        "workspace": "foo",
        "local_files": "not-a-list",
    }
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/v1/onboarding/cleanup-plan", json=body)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_rest_cleanup_plan_entry_missing_path_returns_400(http_app) -> None:
    body = {
        "workspace": "foo",
        "local_files": [{"content": "no path key here"}],
    }
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/v1/onboarding/cleanup-plan", json=body)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_rest_cleanup_plan_bad_jaccard_threshold_returns_400(http_app) -> None:
    body = {
        "workspace": "foo",
        "local_files": [{"path": "README.md", "content": "hello"}],
        "jaccard_threshold": "not-a-number",
    }
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/v1/onboarding/cleanup-plan", json=body)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_rest_cleanup_plan_null_content_returns_400(http_app) -> None:
    """{"content": null} must be rejected as a 400, not raise a 500 (entry.get
    returns None for an explicit JSON null, not the "" default)."""
    body = {
        "workspace": "foo",
        "local_files": [{"path": "README.md", "content": None}],
    }
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/v1/onboarding/cleanup-plan", json=body)
    assert resp.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_threshold", ["nan", "2.0", "-1"])
async def test_rest_cleanup_plan_out_of_range_jaccard_threshold_returns_400(
    http_app, bad_threshold: str,
) -> None:
    body = {
        "workspace": "foo",
        "local_files": [{"path": "README.md", "content": "hello"}],
        "jaccard_threshold": bad_threshold,
    }
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/v1/onboarding/cleanup-plan", json=body)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_rest_cleanup_plan_too_many_files_returns_400(http_app) -> None:
    # Config default (KB_MAX_BOOTSTRAP_FILES) is 50; comfortably exceed it.
    body = {
        "workspace": "foo",
        "local_files": [{"path": f"f{i}.md", "content": "x"} for i in range(10000)],
    }
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/v1/onboarding/cleanup-plan", json=body)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_rest_playbook_returns_text(http_app) -> None:
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/onboarding/playbook?kind=project&workspace=foo")
    assert resp.status_code == 200
    data = resp.json()
    assert data["kind"] == "project"
    assert "foo" in data["text"]


@pytest.mark.asyncio
async def test_rest_playbook_invalid_kind_returns_400(http_app) -> None:
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/onboarding/playbook?kind=bogus")
    assert resp.status_code == 400
