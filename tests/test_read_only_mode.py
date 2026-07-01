"""Read-only replica serving mode (issue #44).

A replica set to KB_READ_ONLY refreshes its index from the git remote via the
git_pull_loop but exposes ONLY the read surface: it registers the read tools
(kb_search / kb_get / kb_list / kb_outline / kb_health) and read REST routes,
and does NOT register write/enforcement-write tools or write REST routes, nor
initialise the write pipeline (worktrees / push queue / pending).

Default (KB_READ_ONLY unset) behaviour is unchanged and is covered elsewhere;
here we only assert the read-only deltas plus one default-mode control.
"""
from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import httpx
import pytest
from fastmcp import Client

import data_olympus.server as server
from data_olympus.config import load_config
from data_olympus.server import build_app

if TYPE_CHECKING:
    from pathlib import Path

READ_TOOLS = {"kb_search", "kb_get", "kb_list", "kb_outline", "kb_health"}
WRITE_OR_ENFORCE_TOOLS = {
    "kb_propose_memory",
    "kb_propose_edit",
    "kb_resolve_pending",
    "kb_list_pending",
    "kb_audit",
    "kb_bootstrap_project",
    "kb_consult",
    "kb_gate_check",
    "kb_compliance",
    "kb_record_event",
}


def _git_init(kb: Path) -> None:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@e.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@e.com",
    }
    subprocess.run(
        ["git", "init", "--initial-branch=main"], cwd=str(kb), check=True,
        capture_output=True, env=env,
    )
    subprocess.run(
        ["git", "-C", str(kb), "add", "-A"], check=True, capture_output=True, env=env
    )
    subprocess.run(
        ["git", "-C", str(kb), "commit", "-m", "init"], check=True,
        capture_output=True, env=env,
    )


def _read_only_app(tmp_kb: Path, tmp_path: Path):
    _git_init(tmp_kb)
    # A replica sets KB_REMOTE_URL (so git_pull_loop has a remote to refresh
    # from) yet must NOT bring up the write pipeline: read_only=True gates it.
    return build_app(
        kb_main_path=tmp_kb,
        kb_index_path=tmp_path / "idx.db",
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        bootstrap_now=True,
        kb_remote_url="dummy",
        worktree_root=str(tmp_path / "wts"),
        pending_root=str(tmp_path / "pending"),
        push_queue_root=str(tmp_path / "pq"),
        read_only=True,
    )


@pytest.mark.asyncio
async def test_read_only_registers_only_read_tools(tmp_kb: Path, tmp_path: Path) -> None:
    app = _read_only_app(tmp_kb, tmp_path)
    tools = {t.name for t in await app.list_tools()}
    assert tools >= READ_TOOLS
    assert not (WRITE_OR_ENFORCE_TOOLS & tools), (
        f"write/enforce tools leaked into read-only mode: "
        f"{WRITE_OR_ENFORCE_TOOLS & tools}"
    )


@pytest.mark.asyncio
async def test_read_only_read_tool_round_trips(tmp_kb: Path, tmp_path: Path) -> None:
    app = _read_only_app(tmp_kb, tmp_path)
    async with Client(app) as client:
        result = await client.call_tool("kb_search", {"query": "worktree", "limit": 5})
        assert "STD-U-001" in str(result)


@pytest.mark.asyncio
async def test_read_only_write_pipeline_not_initialised(
    tmp_kb: Path, tmp_path: Path
) -> None:
    app = _read_only_app(tmp_kb, tmp_path)
    state = app._dolympus_state  # type: ignore[attr-defined]
    assert state.config.read_only is True
    assert state.worktrees is None
    assert state.push_queue is None
    assert state.pending is None
    assert state.rate_limiter is None
    assert state.blocklist is None


@pytest.mark.asyncio
async def test_read_only_read_routes_work(tmp_kb: Path, tmp_path: Path) -> None:
    app = _read_only_app(tmp_kb, tmp_path)
    transport = httpx.ASGITransport(app=app.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/api/v1/health")
        assert health.status_code == 200
        search = await client.get("/api/v1/search", params={"q": "worktree"})
        assert search.status_code == 200


@pytest.mark.asyncio
async def test_read_only_write_routes_absent(tmp_kb: Path, tmp_path: Path) -> None:
    app = _read_only_app(tmp_kb, tmp_path)
    transport = httpx.ASGITransport(app=app.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Write and enforcement-write routes must not be registered -> 404.
        for method, path, body in [
            ("POST", "/api/v1/propose/memory", {"text": "x"}),
            ("POST", "/api/v1/propose/edit", {"target_path": "x"}),
            ("POST", "/api/v1/resolve/abc", {"decision": "approve"}),
            ("POST", "/api/v1/onboarding/bootstrap", {"workspace": "w"}),
            ("POST", "/api/v1/audit/event", {"event_type": "x", "workspace": "w"}),
            ("POST", "/api/v1/consult", {"workspace": "w", "source_session": "s"}),
            ("POST", "/api/v1/gate/check", {"workspace": "w", "session_id": "s"}),
        ]:
            resp = await client.request(method, path, json=body)
            assert resp.status_code == 404, (
                f"{method} {path} should be absent in read-only mode, got "
                f"{resp.status_code}"
            )


@pytest.mark.asyncio
async def test_read_only_observability_get_routes_absent(
    tmp_kb: Path, tmp_path: Path
) -> None:
    """The observability GET routes sit behind the same read-only gate as the
    write routes (register_routes' `if not read_only:` block), so a replica must
    404 them too -- otherwise a replica would expose the writer's pending /
    audit / compliance surface without the write pipeline that backs it."""
    app = _read_only_app(tmp_kb, tmp_path)
    transport = httpx.ASGITransport(app=app.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for path in [
            "/api/v1/pending",
            "/api/v1/audit",
            "/api/v1/audit/verify",
            "/api/v1/compliance",
        ]:
            resp = await client.get(path)
            assert resp.status_code == 404, (
                f"GET {path} should be absent in read-only mode, got "
                f"{resp.status_code}"
            )


@pytest.mark.asyncio
async def test_default_mode_still_registers_write_tools(
    tmp_kb: Path, tmp_path: Path
) -> None:
    """Control: with read_only unset (default) and a remote configured, the
    write tools and pipeline are present exactly as before."""
    _git_init(tmp_kb)
    app = build_app(
        kb_main_path=tmp_kb,
        kb_index_path=tmp_path / "idx.db",
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        bootstrap_now=True,
        kb_remote_url="dummy",
        worktree_root=str(tmp_path / "wts"),
        pending_root=str(tmp_path / "pending"),
        push_queue_root=str(tmp_path / "pq"),
    )
    tools = {t.name for t in await app.list_tools()}
    assert "kb_propose_memory" in tools
    assert "kb_consult" in tools
    state = app._dolympus_state  # type: ignore[attr-defined]
    assert state.config.read_only is False
    assert state.worktrees is not None


def test_kb_read_only_env_parsing(
    tmp_kb: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KB_READ_ONLY is parsed as a truthy flag and reaches Config."""
    monkeypatch.setenv("KB_MAIN_PATH", str(tmp_kb))
    monkeypatch.setenv("KB_INDEX_PATH", str(tmp_path / "kb.db"))
    monkeypatch.setenv("KB_READ_ONLY", "true")
    assert load_config().read_only is True

    monkeypatch.setenv("KB_READ_ONLY", "0")
    assert load_config().read_only is False

    monkeypatch.delenv("KB_READ_ONLY")
    assert load_config().read_only is False


def test_read_only_config_threads_through_build_app_from_config(
    tmp_kb: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """load_config -> build_app_from_config carries read_only into app state."""
    _git_init(tmp_kb)
    monkeypatch.setenv("KB_MAIN_PATH", str(tmp_kb))
    monkeypatch.setenv("KB_INDEX_PATH", str(tmp_path / "kb.db"))
    monkeypatch.setenv("KB_READ_ONLY", "1")
    monkeypatch.setenv("KB_REMOTE_URL", "dummy")
    monkeypatch.setenv("KB_WORKTREE_ROOT", str(tmp_path / "wts"))
    monkeypatch.setenv("KB_PENDING_ROOT", str(tmp_path / "pending"))
    monkeypatch.setenv("KB_PUSH_QUEUE_ROOT", str(tmp_path / "pq"))

    cfg = load_config()
    app = server.build_app_from_config(cfg, bootstrap_now=False)
    state = app._dolympus_state  # type: ignore[attr-defined]
    assert state.config.read_only is True
    assert state.worktrees is None
