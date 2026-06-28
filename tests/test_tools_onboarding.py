"""Tests for kb_onboarding_status_fn + kb_bootstrap_project_fn."""
from __future__ import annotations

from unittest.mock import MagicMock

from data_olympus.tools_onboarding import (
    kb_bootstrap_project_fn,  # noqa: F401  used in later tasks; ensure import works
    kb_onboarding_status_fn,
)


def test_kb_onboarding_status_returns_absent_for_new_workspace() -> None:
    idx = MagicMock()
    idx.list_by_prefix.return_value = []
    idx.list_with_remote_url.return_value = []
    resp = kb_onboarding_status_fn(
        idx=idx, workspace="newproj", component=None,
        workspace_remote_url=None, component_remote_url=None,
    )
    assert resp.state == "absent"


def test_bootstrap_rejects_too_many_files() -> None:
    """An aggregate file-count cap stops one request enqueuing/writing an
    unbounded number of (individually capped) files."""
    idx = MagicMock()
    idx.list_by_prefix.return_value = []  # workspace absent
    idx.list_with_remote_url.return_value = []
    files = [{"target_path": f"projects/p/f{i}.md", "postimage": "x"} for i in range(3)]
    resp = kb_bootstrap_project_fn(
        idx=idx, workspace="p", component=None,
        workspace_remote_url=None, component_remote_url=None,
        files=files, source_session="s", agent_identity="claude",
        confidence=0.95, confidence_threshold=0.85,
        worktrees=MagicMock(), push_queue=MagicMock(), pending=MagicMock(),
        rate_limiter=MagicMock(), blocklist=MagicMock(),
        max_files=2,
    )
    assert resp.status == "rejected_too_many_files"


def test_low_conf_bootstrap_is_atomic_when_queue_would_overflow(tmp_path) -> None:
    """A low-confidence bootstrap that would overflow the pending queue is rejected
    up front, leaving no partial pending entries."""
    from data_olympus.auth import PathBlocklist
    from data_olympus.pending import PendingQueue
    from data_olympus.rate_limit import SlidingWindowLimiter
    idx = MagicMock()
    idx.list_by_prefix.return_value = []  # workspace absent
    idx.list_with_remote_url.return_value = []
    pending = PendingQueue(pending_root=str(tmp_path / "p"), cap=1)
    files = [{"target_path": f"projects/p/f{i}.md", "postimage": "x"} for i in range(3)]
    resp = kb_bootstrap_project_fn(
        idx=idx, workspace="p", component=None,
        workspace_remote_url=None, component_remote_url=None,
        files=files, source_session="s", agent_identity="claude",
        confidence=0.4, confidence_threshold=0.85,  # low -> pending path
        worktrees=MagicMock(), push_queue=MagicMock(), pending=pending,
        rate_limiter=SlidingWindowLimiter(max_per_hour=100),
        blocklist=PathBlocklist(tier_blocks=[], path_blocks=[]),
    )
    assert resp.status == "rejected_pending_queue_full"
    assert pending.size() == 0  # atomic: nothing was enqueued


def test_kb_onboarding_status_returns_onboarded() -> None:
    idx = MagicMock()
    idx.list_by_prefix.return_value = [
        {"path": "projects/example-project/README.md", "git_remote_url": "url1", "tier": "T3"},
        {"path": "projects/example-project/AGENTS.md", "git_remote_url": "url1", "tier": "T3"},
    ]
    idx.list_with_remote_url.return_value = []
    resp = kb_onboarding_status_fn(
        idx=idx, workspace="example-project", component=None,
        workspace_remote_url=None, component_remote_url=None,
    )
    assert resp.state == "onboarded"
