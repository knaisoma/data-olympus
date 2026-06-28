"""Tests for write payload size caps (memory text, edit/bootstrap postimage).

Oversized payloads must be rejected before any worktree/disk side effect, so an
abusive client cannot fill the PVC or balloon memory with a huge proposal.
"""
from __future__ import annotations

import os
import subprocess

from data_olympus.auth import PathBlocklist
from data_olympus.git_ops import GitOps
from data_olympus.pending import PendingQueue
from data_olympus.push_queue import PushQueue
from data_olympus.rate_limit import SlidingWindowLimiter
from data_olympus.tools_write import kb_propose_edit_fn, kb_propose_memory_fn
from data_olympus.worktrees import WorktreeRegistry


def _env() -> dict[str, str]:
    return {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}


def _state(tmp_path):
    repo = tmp_path / "main"
    repo.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=repo, check=True, env=_env())
    (repo / "seed.md").write_text("seed")
    subprocess.run(["git", "add", "seed.md"], cwd=repo, check=True, env=_env())
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, env=_env())
    git = GitOps(repo)
    reg = WorktreeRegistry(git=git, worktree_root=str(tmp_path / "wts"))
    pq = PushQueue(queue_root=str(tmp_path / "push-q"))
    pen = PendingQueue(pending_root=str(tmp_path / "pending"))
    rl = SlidingWindowLimiter(max_per_hour=100)
    bl = PathBlocklist(tier_blocks=[], path_blocks=[])
    return git, reg, pq, pen, rl, bl


def _git_env(monkeypatch) -> None:
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@e.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@e.com")


def test_memory_text_over_cap_rejected(tmp_path) -> None:
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    resp = kb_propose_memory_fn(
        text="x" * 200, tags=[], source_session="s", agent_identity="claude",
        confidence=0.9, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
        max_text_bytes=100,
    )
    assert resp.status == "rejected_payload_too_large"
    assert pq.size() == 0
    assert pen.size() == 0


def test_memory_text_under_cap_ok(tmp_path, monkeypatch) -> None:
    _git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    resp = kb_propose_memory_fn(
        text="small", tags=[], source_session="s", agent_identity="claude",
        confidence=0.9, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
        max_text_bytes=100,
    )
    assert resp.status == "committed"


def test_memory_cap_zero_means_unlimited(tmp_path, monkeypatch) -> None:
    _git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    resp = kb_propose_memory_fn(
        text="x" * 10000, tags=[], source_session="s", agent_identity="claude",
        confidence=0.9, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
        max_text_bytes=0,
    )
    assert resp.status == "committed"


def test_edit_postimage_over_cap_rejected(tmp_path) -> None:
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    resp = kb_propose_edit_fn(
        target_path="universal/foundation/STD-U-001.md",
        postimage="y" * 5000, base_commit="HEAD", base_blob_sha=None,
        target_file_hash=None, reason="big", source_session="s",
        agent_identity="claude", confidence=0.9, confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen, rate_limiter=rl, blocklist=bl,
        remote_addr="1.2.3.4", max_postimage_bytes=1000,
    )
    assert resp.status == "rejected_payload_too_large"
    assert pq.size() == 0
    assert pen.size() == 0


def test_rest_oversized_body_returns_413(tmp_kb, tmp_index_path, tmp_path, monkeypatch) -> None:
    """A request whose Content-Length exceeds KB_MAX_BODY_BYTES is rejected with
    413 before the body is parsed."""
    import httpx

    from data_olympus.server import build_app
    _git_env(monkeypatch)
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=tmp_kb, check=True, env=_env())
    subprocess.run(["git", "-C", str(tmp_kb), "add", "-A"], check=True, env=_env())
    subprocess.run(["git", "-C", str(tmp_kb), "commit", "-m", "init"], check=True, env=_env())
    app = build_app(
        kb_main_path=tmp_kb, kb_index_path=tmp_index_path,
        sync_interval_sec=60, staleness_degraded_sec=600, bootstrap_now=True,
        kb_remote_url="dummy", worktree_root=str(tmp_path / "wts"),
        pending_root=str(tmp_path / "pending"), push_queue_root=str(tmp_path / "pq"),
        max_body_bytes=200,
    ).http_app()

    async def _run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/api/v1/propose/memory",
                json={"text": "x" * 5000, "tags": [], "source_session": "s",
                      "agent_identity": "claude", "confidence": 0.9},
            )

    import asyncio
    resp = asyncio.run(_run())
    assert resp.status_code == 413
    assert resp.json()["error"] == "payload_too_large"
