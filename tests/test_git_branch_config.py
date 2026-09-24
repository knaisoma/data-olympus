"""The knowledge-base branch is configurable (issue #289).

Every git operation the server runs against the KB remote used to name ``main``
literally, so a repository whose trunk is called something else could not be
served at all: the refresh loop's fetch failed, a compare-and-swap write was
refused, and a marker-free write pushed ``HEAD:main`` onto a remote that had no
such branch.

These tests pin both halves of the fix: the setting and its validation, and the
git operations actually using it. The end-to-end cases run against a real bare
remote whose only branch is ``master``, because that is the reported case and a
mock would not catch a literal left behind in a subprocess argument list.
"""
from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from data_olympus.config import load_config
from data_olympus.git_ops import GitOps
from data_olympus.worktrees import WorktreeRegistry

if TYPE_CHECKING:
    from pathlib import Path

_ENV = {
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    "GIT_AUTHOR_NAME": "tester",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "tester",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, env=_ENV,
    )


@pytest.fixture
def master_kb(tmp_path: Path) -> tuple[Path, Path]:
    """A working repo on ``master`` with a bare origin that has no ``main``.

    Returns ``(repo, remote)``.
    """
    remote = tmp_path / "remote.git"
    repo = tmp_path / "kb"
    repo.mkdir()
    _git("init", "--bare", "--initial-branch=master", str(remote))
    _git("init", "--initial-branch=master", str(repo))
    (repo / "seed.md").write_text("seed\n", encoding="utf-8")
    _git("-C", str(repo), "add", "-A")
    _git("-C", str(repo), "commit", "-m", "seed")
    _git("-C", str(repo), "remote", "add", "origin", str(remote))
    _git("-C", str(repo), "push", "-u", "origin", "master")
    return repo, remote


def _remote_branches(remote: Path) -> set[str]:
    out = _git("-C", str(remote), "for-each-ref", "--format=%(refname:short)", "refs/heads")
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


# --- the setting -------------------------------------------------------------


def test_branch_defaults_to_main(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KB_GIT_BRANCH", raising=False)
    assert load_config().kb_git_branch == "main"


def test_branch_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_GIT_BRANCH", "master")
    assert load_config().kb_git_branch == "master"


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_branch_gets_the_default(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    """Blank means unset, as every other path-like setting documents.

    An unsubstituted compose or Helm variable arrives as an empty string; it
    must not turn into an empty ref name that git would reject at runtime.
    """
    monkeypatch.setenv("KB_GIT_BRANCH", blank)
    assert load_config().kb_git_branch == "main"


@pytest.mark.parametrize(
    "bad",
    [
        "-delete-everything",  # git would read it as an option, not a ref
        "--force",
        "has space",
        "has\ttab",
        "has\nnewline",
        "a..b",
        "trailing/",
        "/leading",
        "double//slash",
        "ends.lock",
        "ends.",
        "at@{seq}",
        "tilde~1",
        "caret^1",
        "colon:name",
        "question?",
        "star*",
        "brack[et",
        "back\\slash",
        ".leading-dot",
    ],
)
def test_unusable_branch_name_fails_startup(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    """Reject at load rather than interpolate an arbitrary string into a git
    command line. A leading dash is the case that matters most: git would parse
    it as an option."""
    monkeypatch.setenv("KB_GIT_BRANCH", bad)
    with pytest.raises(ValueError, match="KB_GIT_BRANCH"):
        load_config()


@pytest.mark.parametrize(
    "good", ["main", "master", "trunk", "release/2026-10", "v2.x", "a_b-c.d"]
)
def test_ordinary_branch_names_are_accepted(
    monkeypatch: pytest.MonkeyPatch, good: str
) -> None:
    monkeypatch.setenv("KB_GIT_BRANCH", good)
    assert load_config().kb_git_branch == good


# --- the git operations ------------------------------------------------------


def test_ff_merge_follows_the_configured_branch(master_kb: tuple[Path, Path]) -> None:
    repo, remote = master_kb
    clone = repo.parent / "clone"
    _git("clone", str(remote), str(clone))
    (clone / "new.md").write_text("new\n", encoding="utf-8")
    _git("-C", str(clone), "add", "-A")
    _git("-C", str(clone), "commit", "-m", "second")
    _git("-C", str(clone), "push", "origin", "master")

    git = GitOps(repo, branch="master")
    before = git.head_sha()
    result = git.ff_merge_upstream(timeout_sec=20)
    assert result.status == "changed", result.note
    assert result.changed is True
    assert result.previous_sha == before
    assert result.current_sha != before


def test_ff_merge_on_default_branch_still_fails_against_master(
    master_kb: tuple[Path, Path],
) -> None:
    """The regression this issue reported: without the setting, the fetch of a
    branch the remote does not have degrades health instead of syncing."""
    repo, _ = master_kb
    result = GitOps(repo).ff_merge_upstream(timeout_sec=20)
    assert result.status == "fetch_failed"
    assert result.changed is False


def test_push_targets_the_configured_branch(master_kb: tuple[Path, Path]) -> None:
    """The quiet half of the bug: a push of ``HEAD:main`` would CREATE a main
    branch on a master remote, where nothing ever reads it."""
    repo, remote = master_kb
    (repo / "written.md").write_text("written\n", encoding="utf-8")
    _git("-C", str(repo), "add", "-A")
    _git("-C", str(repo), "commit", "-m", "write")

    GitOps(repo, branch="master").push(str(repo), timeout_sec=20)

    assert _remote_branches(remote) == {"master"}
    head = _git("-C", str(repo), "rev-parse", "HEAD").stdout.strip()
    assert _git("-C", str(remote), "rev-parse", "master").stdout.strip() == head


def test_refresh_base_rebases_onto_the_configured_branch(
    master_kb: tuple[Path, Path],
) -> None:
    repo, remote = master_kb
    clone = repo.parent / "clone"
    _git("clone", str(remote), str(clone))
    (clone / "upstream.md").write_text("upstream\n", encoding="utf-8")
    _git("-C", str(clone), "add", "-A")
    _git("-C", str(clone), "commit", "-m", "upstream work")
    _git("-C", str(clone), "push", "origin", "master")
    upstream_sha = _git("-C", str(clone), "rev-parse", "HEAD").stdout.strip()

    session = repo.parent / "session"
    git = GitOps(repo, branch="master")
    git.worktree_add(str(session), branch="kb-session/test")
    (session / "session.md").write_text("session\n", encoding="utf-8")
    _git("-C", str(session), "add", "-A")
    _git("-C", str(session), "commit", "-m", "session work")

    assert git.refresh_base(str(session), timeout_sec=20) == upstream_sha
    parents = _git("-C", str(session), "rev-parse", "HEAD^").stdout.strip()
    assert parents == upstream_sha


def test_unpushed_detection_uses_the_configured_branch(
    master_kb: tuple[Path, Path],
) -> None:
    """Reachability is what decides whether a session worktree may be garbage
    collected. Measured against a branch the remote does not have, every
    worktree looks unpushed and none is ever reclaimed."""
    repo, _ = master_kb
    session = repo.parent / "session"
    git = GitOps(repo, branch="master")
    git.worktree_add(str(session), branch="kb-session/gc")

    assert git.list_unpushed_shas(str(session)) == []

    (session / "local.md").write_text("local\n", encoding="utf-8")
    _git("-C", str(session), "add", "-A")
    _git("-C", str(session), "commit", "-m", "unpushed")
    assert len(git.list_unpushed_shas(str(session))) == 1


def test_worktree_gc_reachability_uses_the_configured_branch(
    master_kb: tuple[Path, Path], tmp_path: Path
) -> None:
    repo, _ = master_kb
    git = GitOps(repo, branch="master")
    manager = WorktreeRegistry(git=git, worktree_root=str(tmp_path / "worktrees"))
    session = tmp_path / "worktrees" / "kb-session-gc"
    git.worktree_add(str(session), branch="kb-session/gc")

    assert manager._has_unpushed_commits(str(session)) is False

    (session / "local.md").write_text("local\n", encoding="utf-8")
    _git("-C", str(session), "add", "-A")
    _git("-C", str(session), "commit", "-m", "unpushed")
    assert manager._has_unpushed_commits(str(session)) is True
