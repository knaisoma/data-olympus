"""A malformed lock file must not break lock readers, health or reads (#269).

``held_locks()`` caught decode errors but called ``.get`` on whatever parsed,
so a lock holding valid JSON that is not an object (``[]``, ``null``, a string,
a number) raised ``AttributeError``. Every REST read route, ``/readyz`` and MCP
``kb_health`` build health through it, so one such file took them all down.
The same unchecked ``.get`` sat in the ownership and cleanup helpers.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

import httpx
import pytest
from fastmcp import Client

from data_olympus.pending import PendingQueue, _path_lock_filename
from data_olympus.server import build_app

if TYPE_CHECKING:
    from pathlib import Path

MALFORMED = {
    "list": "[]",
    "null": "null",
    "string": '"x"',
    "number": "1",
    "empty": "",
    "truncated": '{"pending_id": "',
}


def _write_lock(queue: PendingQueue, target_path: str, content: str) -> str:
    lock_path = os.path.join(queue._locks_dir, _path_lock_filename(target_path))
    with open(lock_path, "w", encoding="utf-8") as f:
        f.write(content)
    return lock_path


def _queue(tmp_path: Path) -> PendingQueue:
    return PendingQueue(pending_root=str(tmp_path / "pending"))


@pytest.mark.parametrize("kind", sorted(MALFORMED))
def test_held_locks_reports_malformed_lock_as_unreadable(tmp_path: Path, kind: str) -> None:
    q = _queue(tmp_path)
    q.enqueue(proposal_type="edit", target_path="decisions/a.md", postimage="x",
              base_commit="abc", base_blob_sha=None, target_file_hash=None, meta={})
    _write_lock(q, "decisions/b.md", MALFORMED[kind])

    records = q.held_locks()

    assert {r["owner_kind"] for r in records} == {"pending", "unreadable"}
    unreadable = next(r for r in records if r["owner_kind"] == "unreadable")
    assert unreadable["pending_id"] is None
    assert unreadable["target_path"] is None


@pytest.mark.parametrize("kind", sorted(MALFORMED))
def test_cleanup_helpers_leave_malformed_lock_alone(tmp_path: Path, kind: str) -> None:
    q = _queue(tmp_path)
    lock_path = _write_lock(q, "decisions/b.md", MALFORMED[kind])

    assert q.gc_orphan_locks() == 0
    assert q.reclaim_stale_auto_commit_locks(max_age_sec=0) == 0
    assert q.reclaim_stale_auto_commit_locks(max_age_sec=60) == 0
    assert q._release_lock("decisions/b.md", expected_acquired_at=1.0) is False
    q._release_lock_owned_by("decisions/b.md", "0" * 32)
    assert q._lock_holder_is(lock_path, "0" * 32) is False

    assert os.path.exists(lock_path), "a lock whose owner cannot be established was removed"


def _app(tmp_git_kb: Path, tmp_path: Path):
    return build_app(
        kb_main_path=tmp_git_kb,
        kb_index_path=tmp_path / "idx.db",
        sync_interval_sec=60, staleness_degraded_sec=600, bootstrap_now=True,
        kb_remote_url=str(tmp_git_kb),
        worktree_root=str(tmp_path / "wts"),
        pending_root=str(tmp_path / "pending"),
        push_queue_root=str(tmp_path / "pushq"),
    )


@pytest.mark.asyncio
async def test_rest_reads_and_readiness_survive_non_object_lock(
    tmp_git_kb: Path, tmp_path: Path,
) -> None:
    app = _app(tmp_git_kb, tmp_path)
    _write_lock(_queue(tmp_path), "decisions/b.md", "[]")
    transport = httpx.ASGITransport(app=app.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for route in ("/api/v1/health", "/readyz", "/api/v1/search?q=anything",
                      "/api/v1/list?tier=T1", "/api/v1/outline"):
            resp = await client.get(route)
            assert resp.status_code == 200, (route, resp.status_code, resp.text)
        health = (await client.get("/api/v1/health?verbose=true")).json()
    assert any(r["owner_kind"] == "unreadable" for r in health["path_locks"])


@pytest.mark.asyncio
async def test_mcp_health_survives_non_object_lock(tmp_git_kb: Path, tmp_path: Path) -> None:
    app = _app(tmp_git_kb, tmp_path)
    _write_lock(_queue(tmp_path), "decisions/b.md", "null")
    async with Client(app) as client:
        result = await client.call_tool("kb_health", {}, raise_on_error=False)
    assert not result.is_error
