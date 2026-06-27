"""Tests for the periodic git_pull_loop refresh logic."""
from __future__ import annotations

import asyncio
import contextlib
import subprocess
import time
from typing import TYPE_CHECKING

import pytest

from data_olympus.git_ops import GitOps
from data_olympus.index import Index
from data_olympus.refresh import refresh_once

if TYPE_CHECKING:
    from pathlib import Path


def test_refresh_once_no_op_when_unchanged(tmp_git_kb: Path, tmp_path: Path) -> None:
    """refresh_once returns 'no_change' when origin/main hasn't moved."""
    idx = Index(tmp_path / "idx.db")
    idx.build(tmp_git_kb, source_commit="initial")
    git = GitOps(tmp_git_kb)
    result = refresh_once(git=git, idx=idx, kb_main_path=tmp_git_kb)
    assert result["outcome"] == "no_change"
    assert result["error"] is None


def test_refresh_once_rebuilds_on_remote_change(tmp_git_kb: Path, tmp_path: Path) -> None:
    """When origin/main moves, refresh_once ff-merges and rebuilds the index."""
    git = GitOps(tmp_git_kb)
    idx = Index(tmp_path / "idx.db")
    idx.build(tmp_git_kb, source_commit=git.head_sha())
    # Set up a sibling bare remote so ff_merge can advance
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x.com"}
    remote = tmp_path / "remote.git"
    # --initial-branch=main so HEAD is main regardless of runner default.
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)],
                   check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_git_kb), "remote", "add", "origin", str(remote)],
                   check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_git_kb), "push", "-u", "origin", "main"],
                   check=True, env=env)
    # Clone, add a new doc, push so origin/main advances
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(remote), str(clone)], check=True, env=env)
    (clone / "decisions").mkdir(parents=True, exist_ok=True)
    (clone / "decisions" / "DEC-NEW.md").write_text(
        "---\nid: DEC-NEW\n---\n# New decision\n"
    )
    subprocess.run(["git", "-C", str(clone), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(clone), "commit", "-m", "add DEC-NEW"],
                   check=True, env=env)
    subprocess.run(["git", "-C", str(clone), "push", "origin", "main"], check=True, env=env)

    before_sha = git.head_sha()
    result = refresh_once(git=git, idx=idx, kb_main_path=tmp_git_kb)
    assert result["outcome"] == "rebuilt"
    assert result["error"] is None
    after_sha = git.head_sha()
    assert before_sha != after_sha
    # The new doc must now be queryable
    new_doc = idx.get("DEC-NEW")
    assert new_doc is not None


@pytest.mark.asyncio
async def test_git_pull_loop_invokes_refresh_once_with_kwargs(
    tmp_git_kb: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop must use functools.partial (or equivalent) so refresh_once gets keyword args.
    Without this fix, refresh_once would be called positionally and fail silently."""
    from data_olympus.refresh import git_pull_loop

    calls: list[dict[str, object]] = []

    def fake_refresh_once(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append({"args": args, "kwargs": kwargs})
        return {"outcome": "no_change", "error": None, "conflicts": [], "sha": "fake"}

    monkeypatch.setattr("data_olympus.refresh.refresh_once", fake_refresh_once)

    # Build a minimal state
    from data_olympus.config import Config
    from data_olympus.git_ops import GitOps
    from data_olympus.index import Index
    from data_olympus.server import ServerState
    cfg = Config(
        kb_main_path=tmp_git_kb, kb_index_path=tmp_path / "idx.db",
        kb_remote_url="", sync_interval_sec=60, staleness_degraded_sec=600,
        confidence_threshold=0.85, http_port=8080,
    )
    idx = Index(tmp_path / "idx.db")
    idx.build(tmp_git_kb, source_commit="x")
    state = ServerState(idx=idx, git=GitOps(tmp_git_kb), config=cfg)

    task = asyncio.create_task(git_pull_loop(state, interval_sec=0))
    # Let the loop run a couple of iterations
    await asyncio.sleep(0.2)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert calls, "git_pull_loop must invoke refresh_once at least once"
    # The CRITICAL invariant: refresh_once received NO positional args (other than the
    # ones we control), only keyword args. This proves functools.partial was applied.
    first = calls[0]
    assert first["args"] == (), (
        f"refresh_once must be called with kwargs only; got positional args: {first['args']}"
    )
    assert "git" in first["kwargs"]
    assert "idx" in first["kwargs"]
    assert "kb_main_path" in first["kwargs"]


def test_refresh_once_records_duplicate_id_failure(tmp_git_kb: Path, tmp_path: Path) -> None:
    """When a build fails (e.g., duplicate id), refresh_once returns 'failed' and preserves
    the previous index."""
    git = GitOps(tmp_git_kb)
    idx = Index(tmp_path / "idx.db")
    idx.build(tmp_git_kb, source_commit=git.head_sha())
    # Inject a duplicate id directly (no remote needed for this test; we just call refresh
    # after corrupting)
    (tmp_git_kb / "universal" / "foundation" / "STD-U-001-dup.md").write_text(
        "---\nid: STD-U-001\n---\n# duplicate\n"
    )
    # Stage a commit so head moves
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x.com"}
    subprocess.run(["git", "-C", str(tmp_git_kb), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_git_kb), "commit", "-m", "add dup"],
                   check=True, env=env)
    # No remote configured: ff_merge_origin_main is a no-op; build_index manually
    # to simulate the refresh path encountering a duplicate.
    from data_olympus.refresh import rebuild_index_safely
    result = rebuild_index_safely(idx=idx, kb_main_path=tmp_git_kb, source_commit="bad")
    assert result["outcome"] == "failed"
    assert "STD-U-001" in (result["error"] or "")
    # Previous index still serves
    doc = idx.get("STD-U-001")
    assert doc is not None




def test_push_retry_loop_invokes_drain_periodically() -> None:
    from data_olympus.refresh import push_retry_loop

    calls = []

    class FakePushQueue:
        def drain(self, *, push_fn, max_attempts):  # noqa: ARG002
            calls.append(time.time())

    class FakeGitOps:
        def push(self, worktree_path):  # noqa: ARG002
            pass

    pq = FakePushQueue()
    git = FakeGitOps()

    async def runner():
        task = asyncio.create_task(
            push_retry_loop(push_queue=pq, git=git, interval_sec=0.01, max_attempts=3)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(runner())
    assert len(calls) >= 1
