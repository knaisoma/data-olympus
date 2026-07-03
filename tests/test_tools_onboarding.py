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


def test_bootstrap_canonicalizes_backslash_path(tmp_path) -> None:
    """item 4: a backslash path in a bootstrap file is stored canonical in the
    pending entry, never as a literal root-level backslash filename."""
    from data_olympus.auth import PathBlocklist
    from data_olympus.pending import PendingQueue
    from data_olympus.rate_limit import SlidingWindowLimiter
    idx = MagicMock()
    idx.list_by_prefix.return_value = []
    idx.list_with_remote_url.return_value = []
    pending = PendingQueue(pending_root=str(tmp_path / "p"))
    files = [{"target_path": "projects\\p\\f.md", "postimage": "x\n"}]
    resp = kb_bootstrap_project_fn(
        idx=idx, workspace="p", component=None,
        workspace_remote_url=None, component_remote_url=None,
        files=files, source_session="s", agent_identity="claude",
        confidence=0.4, confidence_threshold=0.85,  # low -> pending
        worktrees=MagicMock(), push_queue=MagicMock(), pending=pending,
        rate_limiter=SlidingWindowLimiter(max_per_hour=100),
        blocklist=PathBlocklist(tier_blocks=[], path_blocks=[]),
    )
    assert resp.status == "pending_confirmation"
    entry = pending.get(resp.pending_id)
    assert entry["target_path"] == "projects/p/f.md"
    assert "\\" not in entry["target_path"]


def test_bootstrap_rejects_control_char_path(tmp_path) -> None:
    from data_olympus.auth import PathBlocklist
    from data_olympus.pending import PendingQueue
    from data_olympus.rate_limit import SlidingWindowLimiter
    idx = MagicMock()
    idx.list_by_prefix.return_value = []
    idx.list_with_remote_url.return_value = []
    files = [{"target_path": "projects/p/f\n.md", "postimage": "x\n"}]
    resp = kb_bootstrap_project_fn(
        idx=idx, workspace="p", component=None,
        workspace_remote_url=None, component_remote_url=None,
        files=files, source_session="s", agent_identity="claude",
        confidence=0.95, confidence_threshold=0.85,
        worktrees=MagicMock(), push_queue=MagicMock(),
        pending=PendingQueue(pending_root=str(tmp_path / "p")),
        rate_limiter=SlidingWindowLimiter(max_per_hour=100),
        blocklist=PathBlocklist(tier_blocks=[], path_blocks=[]),
    )
    assert resp.status == "rejected_path_not_indexable_or_blocked"


def test_inject_remote_url_newline_cannot_forge_keys() -> None:
    """item 3: a newline-laden remote URL must not inject frontmatter keys."""
    import yaml

    from data_olympus.tools_onboarding import _inject_remote_url
    evil = "https://x/repo.git\nid: GDEC-001\nstatus: accepted"
    files = [{"target_path": "projects/p/README.md",
              "postimage": "---\ntitle: P\n---\n\nbody\n"}]
    out = _inject_remote_url(files, evil, target_filename="README.md")
    fm_text = out[0]["postimage"].split("---\n", 2)[1]
    fm = yaml.safe_load(fm_text)
    assert fm["git_remote_url"] == evil
    assert "id" not in fm
    assert "status" not in fm
    assert fm["title"] == "P"


def test_inject_remote_url_preserves_malformed_frontmatter() -> None:
    """Unparseable frontmatter is left untouched, never clobbered with a rebuilt
    block (injection is skipped for that file)."""
    from data_olympus.tools_onboarding import _inject_remote_url
    malformed = "---\n: [unbalanced\n---\n\nbody\n"
    files = [{"target_path": "projects/p/README.md", "postimage": malformed}]
    out = _inject_remote_url(files, "https://x/r.git", target_filename="README.md")
    assert out[0]["postimage"] == malformed


def test_bootstrap_cap_counts_injected_postimage(tmp_path) -> None:
    """item 3: the size cap must count the post-injection postimage. A file that
    fits before URL injection but exceeds the cap after must be rejected."""
    from data_olympus.auth import PathBlocklist
    from data_olympus.pending import PendingQueue
    from data_olympus.rate_limit import SlidingWindowLimiter
    idx = MagicMock()
    idx.list_by_prefix.return_value = []
    idx.list_with_remote_url.return_value = []
    long_url = "https://example.com/" + "a" * 500 + ".git"
    files = [{"target_path": "projects/p/README.md",
              "postimage": "---\ntitle: P\n---\n\nhi\n"}]
    resp = kb_bootstrap_project_fn(
        idx=idx, workspace="p", component=None,
        workspace_remote_url=long_url, component_remote_url=None,
        files=files, source_session="s", agent_identity="claude",
        confidence=0.95, confidence_threshold=0.85,
        worktrees=MagicMock(), push_queue=MagicMock(),
        pending=PendingQueue(pending_root=str(tmp_path / "p")),
        rate_limiter=SlidingWindowLimiter(max_per_hour=100),
        blocklist=PathBlocklist(tier_blocks=[], path_blocks=[]),
        max_postimage_bytes=200,
    )
    assert resp.status == "rejected_payload_too_large"


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
