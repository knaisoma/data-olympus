"""Tests for the 4 write MCP tool functions."""
from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

from data_olympus.auth import PathBlocklist
from data_olympus.git_ops import GitOps
from data_olympus.pending import PendingQueue
from data_olympus.push_queue import PushQueue
from data_olympus.rate_limit import SlidingWindowLimiter
from data_olympus.tools_write import (
    kb_list_pending_fn,
    kb_propose_edit_fn,
    kb_propose_memory_fn,
    kb_resolve_pending_fn,
)
from data_olympus.worktrees import WorktreeRegistry

if TYPE_CHECKING:
    import pytest


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
    return git, reg, pq, pen, rl, bl


def _set_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@e.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@e.com")


def test_kb_propose_memory_high_confidence_auto_commits(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    resp = kb_propose_memory_fn(
        text="test memory body",
        tags=["test"],
        source_session="session-abc",
        agent_identity="claude",
        confidence=0.9,
        confidence_threshold=0.85,
        worktrees=reg,
        push_queue=pq,
        pending=pen,
        rate_limiter=rl,
        blocklist=bl,
        remote_addr="10.0.0.1",
    )
    assert resp.status == "committed"
    assert resp.commit_sha
    assert resp.push_state == "queued"
    # Queue entry exists.
    assert pq.size() == 1


def test_kb_propose_memory_low_confidence_returns_pending(tmp_path) -> None:
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    resp = kb_propose_memory_fn(
        text="lowconf",
        tags=[],
        source_session="session-abc",
        agent_identity="claude",
        confidence=0.4,
        confidence_threshold=0.85,
        worktrees=reg,
        push_queue=pq,
        pending=pen,
        rate_limiter=rl,
        blocklist=bl,
        remote_addr="10.0.0.1",
    )
    assert resp.status == "pending_confirmation"
    assert resp.pending_id
    assert resp.proposal_text == "lowconf"
    assert pen.size() == 1


def test_kb_propose_memory_rejects_rate_limited(tmp_path) -> None:
    git, reg, pq, pen, _, bl = _state(tmp_path)
    rl = SlidingWindowLimiter(max_per_hour=0)
    resp = kb_propose_memory_fn(
        text="x",
        tags=[],
        source_session="session-abc",
        agent_identity="claude",
        confidence=0.9,
        confidence_threshold=0.85,
        worktrees=reg,
        push_queue=pq,
        pending=pen,
        rate_limiter=rl,
        blocklist=bl,
        remote_addr="10.0.0.1",
    )
    assert resp.status == "rejected_rate_limited"


def test_kb_propose_memory_rejects_blocked_tier(tmp_path) -> None:
    git, reg, pq, pen, rl, _ = _state(tmp_path)
    bl = PathBlocklist(tier_blocks=["memory"], path_blocks=[])
    resp = kb_propose_memory_fn(
        text="x",
        tags=[],
        source_session="session-abc",
        agent_identity="claude",
        confidence=0.9,
        confidence_threshold=0.85,
        worktrees=reg,
        push_queue=pq,
        pending=pen,
        rate_limiter=rl,
        blocklist=bl,
        remote_addr="10.0.0.1",
    )
    assert resp.status == "rejected_path_blocked"


def test_kb_propose_memory_rejects_symlink_escape(tmp_path, monkeypatch) -> None:
    """Regression for the Codex blocker: a KB commit that plants the memory inbox
    as a symlink to an outside dir must NOT cause a write outside the worktree."""
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    repo = git._repo
    evil = tmp_path / "evil"
    evil.mkdir()
    (repo / "memory").mkdir()
    os.symlink(str(evil), str(repo / "memory" / "inbox"))
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=_env())
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "plant symlink"],
                   check=True, env=_env())
    resp = kb_propose_memory_fn(
        text="escape attempt", tags=[], source_session="s", agent_identity="claude",
        confidence=0.95, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
    )
    assert resp.status == "rejected_symlink_escape"
    assert list(evil.iterdir()) == []  # nothing written outside the worktree
    assert pq.size() == 0


def test_kb_propose_edit_rejects_symlink_escape(tmp_path, monkeypatch) -> None:
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    repo = git._repo
    evil = tmp_path / "evil-edit"
    evil.mkdir()
    # Plant universal/ as a symlink to the evil dir, committed into the tree.
    os.symlink(str(evil), str(repo / "universal"))
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=_env())
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "plant symlink dir"],
                   check=True, env=_env())
    resp = kb_propose_edit_fn(
        target_path="universal/foundation/STD-U-001.md",
        postimage="pwned\n", base_commit="HEAD", base_blob_sha=None,
        target_file_hash=None, reason="escape", source_session="s",
        agent_identity="claude", confidence=0.95, confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen,
        rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
    )
    assert resp.status == "rejected_symlink_escape"
    assert list(evil.iterdir()) == []
    assert pq.size() == 0


def _seed_t1_file(repo) -> tuple[str, str]:
    """Seed a T1 file in the repo; return (target_path, base_blob_sha)."""
    p = repo / "universal" / "foundation" / "STD-U-001.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\nid: STD-U-001\ntier: T1\n---\n# T1\nbody\n")
    import subprocess
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=_env())
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "seed t1"], check=True, env=_env())
    sha = subprocess.check_output(
        ["git", "-C", str(repo), "ls-tree", "HEAD", str(p.relative_to(repo))],
        text=True,
    ).split()[2]
    return "universal/foundation/STD-U-001.md", sha


def test_kb_propose_edit_rejects_traversal(tmp_path) -> None:
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    resp = kb_propose_edit_fn(
        target_path="projects/foo/../../memory/x.md",
        postimage="x", base_commit="HEAD", base_blob_sha=None, target_file_hash=None,
        reason="test", source_session="s", agent_identity="claude", confidence=0.9,
        confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen,
        rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
    )
    assert resp.status == "rejected_path_not_indexable"


def test_kb_propose_edit_high_conf_commits(tmp_path, monkeypatch) -> None:
    # Need git env for the commit inside the function (same pattern as Task 12).
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@e.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@e.com")
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    repo = git._repo  # note: GitOps stores path as self._repo
    target, blob = _seed_t1_file(repo)
    resp = kb_propose_edit_fn(
        target_path=target,
        postimage="new body\n",
        base_commit="HEAD",
        base_blob_sha=blob,
        target_file_hash=None,
        reason="fix",
        source_session="s",
        agent_identity="claude",
        confidence=0.9,
        confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen,
        rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
    )
    assert resp.status == "committed"
    assert pq.size() == 1


def test_kb_propose_edit_low_conf_pending(tmp_path) -> None:
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    repo = git._repo
    target, blob = _seed_t1_file(repo)
    resp = kb_propose_edit_fn(
        target_path=target,
        postimage="new body\n",
        base_commit="HEAD",
        base_blob_sha=blob,
        target_file_hash=None,
        reason="lowconf",
        source_session="s",
        agent_identity="claude",
        confidence=0.5,
        confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen,
        rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
    )
    assert resp.status == "pending_confirmation"
    assert pen.size() == 1


def test_kb_list_pending_returns_entries(tmp_path) -> None:
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    kb_propose_memory_fn(
        text="x", tags=[], source_session="s", agent_identity="claude",
        confidence=0.3, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
    )
    resp = kb_list_pending_fn(pending=pen)
    assert len(resp.pending) == 1


def test_kb_resolve_pending_approve_commits(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@e.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@e.com")
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    m = kb_propose_memory_fn(
        text="memory body", tags=[], source_session="s", agent_identity="claude",
        confidence=0.3, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
    )
    resp = kb_resolve_pending_fn(
        pending_id=m.pending_id,
        decision="approve",
        edited_text=None,
        worktrees=reg, push_queue=pq, pending=pen,
        source_session="s", agent_identity="claude",
    )
    assert resp.status == "committed"
    assert resp.commit_sha
    assert pen.size() == 0


def test_kb_resolve_pending_reject_clears(tmp_path) -> None:
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    m = kb_propose_memory_fn(
        text="x", tags=[], source_session="s", agent_identity="claude",
        confidence=0.3, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
    )
    resp = kb_resolve_pending_fn(
        pending_id=m.pending_id, decision="reject", edited_text=None,
        worktrees=reg, push_queue=pq, pending=pen,
        source_session="s", agent_identity="claude",
    )
    assert resp.status == "rejected"
    assert pen.size() == 0
