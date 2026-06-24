"""Verify that env-driven Config fields actually reach the live app state.

Before the fix, main() called build_app() with only four fields; everything
else was silently dropped and hardcoded defaults were used inside build_app.
This test exercises build_app_from_config() and confirms that non-default
values propagate all the way into _dolympus_state and the pipeline objects.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import data_olympus.server as server
from data_olympus.config import load_config

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_non_default_config_is_threaded_into_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb = tmp_path / "kb"
    kb.mkdir()

    monkeypatch.setenv("KB_MAIN_PATH", str(kb))
    monkeypatch.setenv("KB_INDEX_PATH", str(tmp_path / "kb.db"))
    monkeypatch.setenv("KB_CONFIDENCE_THRESHOLD", "0.5")
    monkeypatch.setenv("KB_WRITE_BLOCK_TIERS", "T1,T2")
    monkeypatch.setenv("KB_WRITE_BLOCK_PATHS", "decisions/GDEC-008-*.md")
    monkeypatch.setenv("KB_RATE_LIMIT_PER_HOUR", "42")
    monkeypatch.setenv("KB_PENDING_TIMEOUT_SEC", "7200")
    monkeypatch.setenv("KB_PENDING_QUEUE_CAP", "25")
    monkeypatch.setenv("KB_WORKTREE_IDLE_SEC", "999")
    monkeypatch.setenv("KB_GIT_KEY_PATH", "/tmp/test-key")
    # No KB_REMOTE_URL: write-pipeline objects are None when no remote is set.
    # We still get the config values threaded into state.config.

    cfg = load_config()

    # Verify load_config() picked up all non-default values correctly.
    assert cfg.confidence_threshold == 0.5
    assert cfg.write_block_tiers == ["T1", "T2"]
    assert cfg.write_block_paths == ["decisions/GDEC-008-*.md"]
    assert cfg.rate_limit_per_hour == 42
    assert cfg.pending_timeout_sec == 7200
    assert cfg.pending_queue_cap == 25
    assert cfg.worktree_idle_sec == 999
    assert cfg.git_key_path == "/tmp/test-key"

    app = server.build_app_from_config(cfg, bootstrap_now=False)
    state = app._dolympus_state  # type: ignore[attr-defined]

    # confidence_threshold reaches the config stored in state.
    assert state.config.confidence_threshold == 0.5

    # All other non-default values reach state.config too.
    assert state.config.rate_limit_per_hour == 42
    assert state.config.pending_timeout_sec == 7200
    assert state.config.pending_queue_cap == 25
    assert state.config.worktree_idle_sec == 999
    assert state.config.git_key_path == "/tmp/test-key"
    assert state.config.write_block_tiers == ["T1", "T2"]
    assert state.config.write_block_paths == ["decisions/GDEC-008-*.md"]


def test_write_block_tiers_reach_blocklist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_git_kb: Path
) -> None:
    """When KB_REMOTE_URL is set the write pipeline is enabled; confirm the
    PathBlocklist inside the app state reflects the configured tier blocks."""
    # tmp_git_kb is a real git repo — needed for GitOps to initialise.
    kb = tmp_git_kb

    monkeypatch.setenv("KB_MAIN_PATH", str(kb))
    monkeypatch.setenv("KB_INDEX_PATH", str(tmp_path / "kb.db"))
    monkeypatch.setenv("KB_CONFIDENCE_THRESHOLD", "0.7")
    monkeypatch.setenv("KB_WRITE_BLOCK_TIERS", "T1")
    monkeypatch.setenv("KB_REMOTE_URL", "git@github.com:example/repo.git")
    # Keep pipeline roots under tmp_path so tests don't hit the read-only filesystem.
    monkeypatch.setenv("KB_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    monkeypatch.setenv("KB_PENDING_ROOT", str(tmp_path / "pending"))
    monkeypatch.setenv("KB_PUSH_QUEUE_ROOT", str(tmp_path / "push-queue"))

    cfg = load_config()
    app = server.build_app_from_config(cfg, bootstrap_now=False)
    state = app._dolympus_state  # type: ignore[attr-defined]

    # With a remote URL the blocklist object is created.
    assert state.blocklist is not None

    # The PathBlocklist should block tier T1.
    assert state.blocklist.blocks("universal/foundation/STD-U-001.md", "T1")
    # T2 should pass through (not blocked).
    assert not state.blocklist.blocks("tech-stacks/nestjs/STD-BN-001.md", "T2")

    # confidence_threshold is threaded too.
    assert state.config.confidence_threshold == 0.7
