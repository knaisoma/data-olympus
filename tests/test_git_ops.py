"""Tests for git_ops module."""
from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

import pytest

from data_olympus.git_ops import GitOps

if TYPE_CHECKING:
    from pathlib import Path


def test_head_sha_on_fresh_repo(tmp_git_kb: Path) -> None:
    git = GitOps(tmp_git_kb)
    sha = git.head_sha()
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


def test_ff_merge_no_op_when_no_remote_change(tmp_git_kb: Path) -> None:
    git = GitOps(tmp_git_kb)
    before = git.head_sha()
    # No remote configured; ff_merge should be a no-op (or report no fetch source) without raising.
    result = git.ff_merge_origin_main(timeout_sec=10)
    assert result.previous_sha == before
    assert result.current_sha == before
    assert result.changed is False


def test_ff_merge_advances_after_local_remote_commit(tmp_git_kb: Path, tmp_path: Path) -> None:
    """Simulate a remote by setting origin to a sibling clone, adding a commit to remote, ff."""
    git = GitOps(tmp_git_kb)
    remote = tmp_path / "remote.git"
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
           "GIT_AUTHOR_NAME": "tester", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "tester", "GIT_COMMITTER_EMAIL": "t@example.com"}
    # Bare remote. --initial-branch=main so HEAD is main regardless of the
    # runner's init.defaultBranch (CI defaults to master without this).
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)],
                   check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_git_kb), "remote", "add", "origin", str(remote)],
                   check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_git_kb), "push", "-u", "origin", "main"],
                   check=True, env=env)

    # Clone, add commit, push
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(remote), str(clone)], check=True, env=env)
    (clone / "newfile.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(clone), "add", "newfile.md"], check=True, env=env)
    subprocess.run(["git", "-C", str(clone), "commit", "-m", "add new"], check=True, env=env)
    subprocess.run(["git", "-C", str(clone), "push", "origin", "main"], check=True, env=env)

    before = git.head_sha()
    result = git.ff_merge_origin_main(timeout_sec=10)
    assert result.previous_sha == before
    assert result.current_sha != before
    assert result.changed is True


def test_head_sha_missing_repo_raises(tmp_path: Path) -> None:
    git = GitOps(tmp_path / "no_such_dir")
    with pytest.raises(FileNotFoundError):
        git.head_sha()


def _git_env() -> dict[str, str]:
    return {**os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}


def test_worktree_add_and_remove_round_trip(tmp_path) -> None:
    # Set up a bare-ish repo with one commit.
    repo = tmp_path / "main"
    repo.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=repo, check=True, env=_git_env())
    (repo / "a.md").write_text("x")
    subprocess.run(["git", "add", "a.md"], cwd=repo, check=True, env=_git_env())
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, env=_git_env())

    git = GitOps(repo)
    wt = tmp_path / "wt" / "session-abc"
    git.worktree_add(str(wt), branch="kb-session/abc")
    assert wt.is_dir()
    assert (wt / "a.md").exists()

    git.worktree_remove(str(wt), force=True)
    assert not wt.exists()


def test_worktree_add_idempotent_existing_wt_returns_existing(tmp_path) -> None:
    repo = tmp_path / "main"
    repo.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=repo, check=True, env=_git_env())
    (repo / "a.md").write_text("x")
    subprocess.run(["git", "add", "a.md"], cwd=repo, check=True, env=_git_env())
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, env=_git_env())

    git = GitOps(repo)
    wt = tmp_path / "wt" / "session-abc"
    git.worktree_add(str(wt), branch="kb-session/abc")
    # Second call: must not raise; the worktree already exists.
    git.worktree_add(str(wt), branch="kb-session/abc")


def test_normalize_remote_url_collapses_ssh_https():
    from data_olympus.git_ops import normalize_remote_url
    a = normalize_remote_url("git@github.com:org/repo.git")
    b = normalize_remote_url("https://github.com/org/repo")
    assert a == b


def test_normalize_remote_url_strips_trailing_slash_and_dotgit():
    from data_olympus.git_ops import normalize_remote_url
    a = normalize_remote_url("https://github.com/org/repo/")
    b = normalize_remote_url("https://github.com/org/repo.git")
    assert a == b


def test_get_remote_url_returns_none_if_no_remote(tmp_path):
    import subprocess

    from data_olympus.git_ops import get_remote_url
    repo = tmp_path / "norepo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    assert get_remote_url(str(repo)) is None
