"""Tests for WorktreeRegistry: per-session worktree lifecycle + GC."""
from __future__ import annotations

import os
import subprocess
import time

from data_olympus.git_ops import GitOps
from data_olympus.worktrees import WorktreeRegistry


def _env() -> dict[str, str]:
    return {**os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}


def _seeded_repo(tmp_path):
    repo = tmp_path / "main"
    repo.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=repo, check=True, env=_env())
    (repo / "seed.md").write_text("seed")
    subprocess.run(["git", "add", "seed.md"], cwd=repo, check=True, env=_env())
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, env=_env())
    return repo


def test_get_or_create_creates_worktree_on_first_call(tmp_path) -> None:
    repo = _seeded_repo(tmp_path)
    reg = WorktreeRegistry(
        git=GitOps(repo),
        worktree_root=str(tmp_path / "wts"),
    )
    wt = reg.get_or_create(source_session="session-abc", agent_identity="claude")
    assert os.path.isdir(wt.path)
    meta = wt.read_meta()
    assert meta["source_session"] == "session-abc"
    assert meta["agent_identity"] == "claude"


def test_get_or_create_reuses_existing_for_same_session(tmp_path) -> None:
    repo = _seeded_repo(tmp_path)
    reg = WorktreeRegistry(
        git=GitOps(repo),
        worktree_root=str(tmp_path / "wts"),
    )
    a = reg.get_or_create(source_session="session-abc", agent_identity="claude")
    b = reg.get_or_create(source_session="session-abc", agent_identity="claude")
    assert a.path == b.path


def test_gc_removes_idle_worktree_with_no_unpushed_commits(tmp_path) -> None:
    repo = _seeded_repo(tmp_path)
    reg = WorktreeRegistry(
        git=GitOps(repo),
        worktree_root=str(tmp_path / "wts"),
    )
    wt = reg.get_or_create(source_session="session-abc", agent_identity="claude")
    # Force last_activity to 2 hours ago.
    wt.touch(timestamp=time.time() - 7200)
    removed = reg.gc(idle_sec=3600)
    assert wt.path in removed


def test_gc_does_not_remove_active_worktree(tmp_path) -> None:
    repo = _seeded_repo(tmp_path)
    reg = WorktreeRegistry(
        git=GitOps(repo),
        worktree_root=str(tmp_path / "wts"),
    )
    wt = reg.get_or_create(source_session="session-abc", agent_identity="claude")
    wt.touch()  # now
    removed = reg.gc(idle_sec=3600)
    assert wt.path not in removed
