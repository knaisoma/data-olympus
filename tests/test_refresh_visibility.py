"""Tests for git sync-failure visibility (Codex refresh-adoption finding).

A fetch/ff failure must be classified and surfaced, and must NOT advance the
freshness marker, so health degrades instead of reporting a fresh no-change.
"""
from __future__ import annotations

import asyncio
import contextlib
import subprocess
from typing import TYPE_CHECKING

import pytest

from data_olympus.config import Config
from data_olympus.git_ops import GitOps
from data_olympus.index import Index
from data_olympus.refresh import git_pull_loop, refresh_once
from data_olympus.server import ServerState

if TYPE_CHECKING:
    from pathlib import Path

_ENV = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x.com"}


def test_ff_merge_no_remote_status(tmp_git_kb: Path) -> None:
    git = GitOps(tmp_git_kb)
    result = git.ff_merge_origin_main()
    assert result.status == "no_remote"
    assert result.changed is False


def test_ff_merge_fetch_failed_status(tmp_git_kb: Path, tmp_path: Path) -> None:
    """A configured-but-unreachable origin classifies as fetch_failed."""
    subprocess.run(
        ["git", "-C", str(tmp_git_kb), "remote", "add", "origin",
         str(tmp_path / "does-not-exist.git")],
        check=True, env=_ENV,
    )
    git = GitOps(tmp_git_kb)
    result = git.ff_merge_origin_main(timeout_sec=10)
    assert result.status == "fetch_failed"
    assert result.changed is False
    assert "fetch_failed" in result.note


def test_refresh_once_surfaces_sync_status(tmp_git_kb: Path, tmp_path: Path) -> None:
    idx = Index(tmp_path / "idx.db")
    idx.build(tmp_git_kb, source_commit="initial")
    git = GitOps(tmp_git_kb)
    out = refresh_once(git=git, idx=idx, kb_main_path=tmp_git_kb)
    assert out["sync_status"] == "no_remote"
    assert "remote_head_sha" in out


@pytest.mark.asyncio
async def test_loop_freezes_freshness_on_fetch_failure(
    tmp_git_kb: Path, tmp_path: Path,
) -> None:
    """On fetch_failed the loop records the error and does NOT advance
    last_git_pull_at (so staleness climbs and health degrades)."""
    subprocess.run(
        ["git", "-C", str(tmp_git_kb), "remote", "add", "origin",
         str(tmp_path / "missing.git")],
        check=True, env=_ENV,
    )
    cfg = Config(
        kb_main_path=tmp_git_kb, kb_index_path=tmp_path / "idx.db",
        kb_remote_url="dummy", sync_interval_sec=60, staleness_degraded_sec=600,
        confidence_threshold=0.85, http_port=8080,
    )
    idx = Index(tmp_path / "idx.db")
    idx.build(tmp_git_kb, source_commit="x")
    state = ServerState(idx=idx, git=GitOps(tmp_git_kb), config=cfg)

    task = asyncio.create_task(git_pull_loop(state, interval_sec=0))
    await asyncio.sleep(0.2)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert state.last_git_fetch_status == "fetch_failed"
    assert state.last_git_fetch_error
    assert state.last_git_fetch_at is not None
    # Freshness marker stays None -> staleness path will degrade health.
    assert state.last_git_pull_at is None
    assert state.last_successful_refresh_at is None
