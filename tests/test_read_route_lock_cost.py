"""REST read routes must not enumerate or read every path lock on a request
that turns out healthy (issue #271).

Every REST read route ran `_build_health(state)` first to decide whether to
return 503. `_build_health` called both `PendingQueue.locks_held()` (lists the
lock directory) and `PendingQueue.held_locks()` (opens and parses every
`*.lock` file) unconditionally, so a plain `GET /api/v1/get/{id}` or `/search`
paid one file read per open proposal, on a request that never wanted the lock
list at all -- the precheck only needs `degraded`.

Coverage:
- A healthy get/search/outline/list/metrics/readyz call does zero lock-directory
  listings and zero lock-file reads, with zero locks held.
- The same holds with 200 unrelated open proposals: cost stays zero, not
  proportional to queue size.
- A degraded response still returns the full path_locks list in its 503 body
  (the rare path is allowed to pay the enumeration cost).
- /api/v1/health is unaffected: it still returns path_locks.
"""
from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

import httpx
import pytest

from data_olympus.server import build_app

if TYPE_CHECKING:
    from data_olympus.pending import PendingQueue


def _init_git(tmp_kb):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=tmp_kb, check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_kb), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_kb), "commit", "-m", "init"], check=True, env=env)


@pytest.fixture
def app(tmp_kb, tmp_index_path, tmp_path):
    _init_git(tmp_kb)
    return build_app(
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
    )


@pytest.fixture(autouse=True)
def _git_env(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@e.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@e.com")


class _Counter:
    """Wraps PendingQueue.locks_held/held_locks with call counters, via
    monkeypatch on the bound methods, so the assertion is against the real
    dispatch path (ASGI request -> route -> _build_health -> PendingQueue),
    not a mock standing in for it."""

    def __init__(self, monkeypatch, pending: PendingQueue) -> None:
        self.locks_held_calls = 0
        self.held_locks_calls = 0
        real_locks_held = pending.locks_held
        real_held_locks = pending.held_locks

        def counted_locks_held():
            self.locks_held_calls += 1
            return real_locks_held()

        def counted_held_locks():
            self.held_locks_calls += 1
            return real_held_locks()

        monkeypatch.setattr(pending, "locks_held", counted_locks_held)
        monkeypatch.setattr(pending, "held_locks", counted_held_locks)


def _counter(app, monkeypatch) -> _Counter:
    state = app._dolympus_state  # type: ignore[attr-defined]
    assert state.pending is not None
    return _Counter(monkeypatch, state.pending)


# ---------------------------------------------------------------------------
# Healthy: zero enumeration, zero file reads
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/api/v1/search?q=anything",
    "/api/v1/outline",
    "/api/v1/list?tier=universal",
    "/readyz",
    "/metrics",
])
@pytest.mark.asyncio
async def test_healthy_route_does_no_lock_enumeration(app, monkeypatch, path) -> None:
    counter = _counter(app, monkeypatch)
    transport = httpx.ASGITransport(app=app.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(path)
    assert resp.status_code in (200, 501)  # /metrics is 501 without prometheus-client
    assert counter.locks_held_calls == 0
    assert counter.held_locks_calls == 0


@pytest.mark.asyncio
async def test_healthy_get_does_no_lock_enumeration(app, monkeypatch) -> None:
    counter = _counter(app, monkeypatch)
    transport = httpx.ASGITransport(app=app.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/get/nonexistent-doc")
    assert resp.status_code == 404  # not degraded, so the precheck ran open
    assert counter.locks_held_calls == 0
    assert counter.held_locks_calls == 0


@pytest.mark.asyncio
async def test_healthy_route_cost_does_not_scale_with_queue_size(app, monkeypatch) -> None:
    """200 unrelated open proposals: the healthy precheck's cost stays zero,
    not proportional to the queue."""
    state = app._dolympus_state  # type: ignore[attr-defined]
    for i in range(200):
        state.pending._acquire_lock(f"projects/p{i}/doc.md", f"pending-{i}")  # noqa: SLF001
    counter = _counter(app, monkeypatch)
    transport = httpx.ASGITransport(app=app.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/search?q=anything")
    assert resp.status_code == 200
    assert counter.locks_held_calls == 0
    assert counter.held_locks_calls == 0


# ---------------------------------------------------------------------------
# Degraded: the rare path pays the enumeration cost, and the 503 body carries
# the full lock list
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_degraded_response_still_carries_path_locks(app) -> None:
    state = app._dolympus_state  # type: ignore[attr-defined]
    state.pending._acquire_lock("projects/foo/bar.md", "pending-xyz")  # noqa: SLF001
    state.last_git_pull_at = None  # forces degraded, matches test_health.py convention
    transport = httpx.ASGITransport(app=app.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/search?q=anything")
    assert resp.status_code == 503
    body = resp.json()
    assert body["degraded"] is True
    assert len(body["path_locks"]) == 1
    assert body["path_locks"][0]["pending_id"] == "pending-xyz"  # no auth configured


# ---------------------------------------------------------------------------
# /api/v1/health is unaffected: it always returns path_locks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_route_still_returns_path_locks(app) -> None:
    state = app._dolympus_state  # type: ignore[attr-defined]
    state.pending._acquire_lock("projects/foo/bar.md", "pending-xyz")  # noqa: SLF001
    transport = httpx.ASGITransport(app=app.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/health?verbose=true")
    assert resp.status_code == 200
    assert len(resp.json()["path_locks"]) == 1
