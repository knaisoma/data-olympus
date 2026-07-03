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


def _cloned_repo_with_origin(tmp_path):
    """A working checkout whose origin/main exists, so _has_unpushed_commits
    can actually distinguish pushed vs unpushed commits."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)],
                   check=True, env=_env())
    repo = tmp_path / "main"
    subprocess.run(["git", "clone", str(remote), str(repo)], check=True, env=_env())
    (repo / "seed.md").write_text("seed")
    subprocess.run(["git", "-C", str(repo), "add", "seed.md"], check=True, env=_env())
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, env=_env())
    subprocess.run(["git", "-C", str(repo), "push", "origin", "main"], check=True, env=_env())
    return repo


def test_gc_deletes_session_branch_so_returning_session_can_write(tmp_path) -> None:
    """CRITICAL coupled bug: GC must delete the kb-session branch, otherwise a
    returning session's `worktree add -b <branch>` fails because the branch
    already exists. This proves the session can create a worktree again after
    being GC'd."""
    repo = _cloned_repo_with_origin(tmp_path)
    git = GitOps(repo)
    reg = WorktreeRegistry(git=git, worktree_root=str(tmp_path / "wts"))

    wt = reg.get_or_create(source_session="session-abc", agent_identity="claude")
    safe = os.path.basename(wt.path)
    assert git._branch_exists(f"kb-session/{safe}")

    # Idle it out; GC removes worktree AND deletes the branch.
    wt.touch(timestamp=time.time() - 7200)
    removed = reg.gc(idle_sec=3600)
    assert wt.path in removed
    assert not git._branch_exists(f"kb-session/{safe}"), \
        "GC left the kb-session branch behind; returning session would fail"

    # Returning session: must succeed (this was the fatal error before the fix).
    wt2 = reg.get_or_create(source_session="session-abc", agent_identity="claude")
    assert os.path.isdir(wt2.path)
    assert os.path.basename(wt2.path) == safe


def test_gc_defers_worktree_with_unpushed_commits(tmp_path) -> None:
    """GC must NOT remove a worktree that has commits not yet on origin/main
    (the push queue still owes those); the _has_unpushed_commits guard holds."""
    repo = _cloned_repo_with_origin(tmp_path)
    git = GitOps(repo)
    reg = WorktreeRegistry(git=git, worktree_root=str(tmp_path / "wts"))

    wt = reg.get_or_create(source_session="session-xyz", agent_identity="claude")
    # Make an unpushed commit inside the session worktree.
    (os.path.join(wt.path, "draft.md"))
    with open(os.path.join(wt.path, "draft.md"), "w") as f:
        f.write("unpushed work")
    subprocess.run(["git", "-C", wt.path, "add", "draft.md"], check=True, env=_env())
    subprocess.run(["git", "-C", wt.path, "commit", "-m", "wip"], check=True, env=_env())
    # Fetch so origin/main is a known ref in this worktree.
    subprocess.run(["git", "-C", wt.path, "fetch", "origin"], check=True, env=_env())

    wt.touch(timestamp=time.time() - 7200)  # idle
    removed = reg.gc(idle_sec=3600)
    assert wt.path not in removed, "GC removed a worktree with unpushed commits"
    assert os.path.isdir(wt.path)


def test_gc_defers_when_reachability_cannot_be_proven(tmp_path) -> None:
    """Fail-closed: if `git rev-list HEAD --not origin/main` fails (origin
    exists but origin/main is unresolvable) GC must NOT remove the worktree.
    Otherwise a rev-list error would masquerade as 'nothing unpushed' and a
    worktree with unpushed commits could be deleted."""
    # Repo with an origin remote configured, but origin/main is NOT fetched, so
    # `origin/main` does not resolve and rev-list exits nonzero.
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)],
                   check=True, env=_env())
    repo = tmp_path / "main"
    subprocess.run(["git", "init", "--initial-branch=main", str(repo)], check=True, env=_env())
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)],
                   check=True, env=_env())
    (repo / "seed.md").write_text("seed")
    subprocess.run(["git", "-C", str(repo), "add", "seed.md"], check=True, env=_env())
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, env=_env())
    # Note: no push/fetch, so `origin/main` is an unknown ref in this repo.

    git = GitOps(repo)
    reg = WorktreeRegistry(git=git, worktree_root=str(tmp_path / "wts"))
    wt = reg.get_or_create(source_session="session-unresolved", agent_identity="claude")
    # Sanity: origin/main does not resolve here, so reachability is unprovable.
    rev = subprocess.run(
        ["git", "-C", wt.path, "rev-list", "HEAD", "--not", "origin/main"],
        check=False, capture_output=True, text=True, env=_env())
    assert rev.returncode != 0

    wt.touch(timestamp=time.time() - 7200)  # idle
    removed = reg.gc(idle_sec=3600)
    assert wt.path not in removed, "GC removed a worktree whose push state is unknown"
    assert os.path.isdir(wt.path)
