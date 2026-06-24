"""Verify that tools_write fns emit audit events."""
from __future__ import annotations

import os
import subprocess

from data_olympus.audit_log import AuditLog
from data_olympus.auth import PathBlocklist
from data_olympus.git_ops import GitOps
from data_olympus.pending import PendingQueue
from data_olympus.push_queue import PushQueue
from data_olympus.rate_limit import SlidingWindowLimiter
from data_olympus.tools_write import kb_propose_memory_fn
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
    rl = SlidingWindowLimiter(max_per_hour=10)
    bl = PathBlocklist(tier_blocks=[], path_blocks=[])
    al = AuditLog(log_path=str(tmp_path / "audit.log"))
    return reg, pq, pen, rl, bl, al


def test_propose_memory_committed_emits_event(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@e.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@e.com")
    reg, pq, pen, rl, bl, al = _state(tmp_path)
    kb_propose_memory_fn(
        text="x", tags=[], source_session="s", agent_identity="claude",
        confidence=0.9, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
        audit_log=al,
    )
    events = list(al.iter_filtered())
    assert len(events) == 1
    assert events[0]["event_type"] == "propose_memory"
    assert events[0]["status"] == "committed"


def test_propose_memory_pending_emits_event(tmp_path) -> None:
    reg, pq, pen, rl, bl, al = _state(tmp_path)
    kb_propose_memory_fn(
        text="x", tags=[], source_session="s", agent_identity="claude",
        confidence=0.3, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
        audit_log=al,
    )
    events = list(al.iter_filtered())
    assert len(events) == 1
    assert events[0]["status"] == "pending_confirmation"
