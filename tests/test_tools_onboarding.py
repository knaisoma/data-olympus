"""Tests for kb_onboarding_status_fn + kb_bootstrap_project_fn."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from data_olympus.tools_onboarding import (
    kb_bootstrap_project_fn,  # noqa: F401  used in later tasks; ensure import works
    kb_onboarding_status_fn,
)


def _partial_idx(present_paths: list[str]) -> MagicMock:
    """An idx mock whose T3 workspace already contains ``present_paths`` (so a
    canonical file that is NOT present makes compute_status report 'partial')."""
    idx = MagicMock()
    idx.list_by_prefix.return_value = [{"path": p} for p in present_paths]
    idx.list_with_remote_url.return_value = []
    return idx


@pytest.fixture
def real_worktrees(tmp_path, monkeypatch):
    """A WorktreeRegistry over a real temp git repo, for exercising the
    high-confidence commit path end to end."""
    from data_olympus.git_ops import GitOps
    from data_olympus.worktrees import WorktreeRegistry
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@e.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@e.com")
    repo = tmp_path / "kb"
    repo.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=repo, check=True)
    (repo / "seed.md").write_text("seed\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True)
    git = GitOps(Path(repo))
    return WorktreeRegistry(git=git, worktree_root=str(tmp_path / "wts"))


def test_kb_onboarding_status_returns_absent_for_new_workspace() -> None:
    idx = MagicMock()
    idx.list_by_prefix.return_value = []
    idx.list_with_remote_url.return_value = []
    resp = kb_onboarding_status_fn(
        idx=idx, workspace="newproj", component=None,
        workspace_remote_url=None, component_remote_url=None,
    )
    assert resp.state == "absent"


def test_bootstrap_rejects_too_many_files(tmp_path) -> None:
    """An aggregate file-count cap stops one request enqueuing/writing an
    unbounded number of (individually capped) files."""
    from data_olympus.onboarding_inflight import BootstrapInFlight
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
        in_flight=BootstrapInFlight(str(tmp_path / "inflight")),
    )
    assert resp.status == "rejected_too_many_files"


def test_low_conf_bootstrap_is_atomic_when_queue_would_overflow(tmp_path) -> None:
    """A low-confidence bootstrap that would overflow the pending queue is rejected
    up front, leaving no partial pending entries."""
    from data_olympus.auth import PathBlocklist
    from data_olympus.onboarding_inflight import BootstrapInFlight
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
        in_flight=BootstrapInFlight(str(tmp_path / "inflight")),
    )
    assert resp.status == "rejected_pending_queue_full"
    assert pending.size() == 0  # atomic: nothing was enqueued


def test_bootstrap_canonicalizes_backslash_path(tmp_path) -> None:
    """item 4: a backslash path in a bootstrap file is stored canonical in the
    pending entry, never as a literal root-level backslash filename."""
    from data_olympus.auth import PathBlocklist
    from data_olympus.onboarding_inflight import BootstrapInFlight
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
        in_flight=BootstrapInFlight(str(tmp_path / "inflight")),
    )
    assert resp.status == "pending_confirmation"
    entry = pending.get(resp.pending_id)
    assert entry["target_path"] == "projects/p/f.md"
    assert "\\" not in entry["target_path"]


def test_bootstrap_rejects_control_char_path(tmp_path) -> None:
    from data_olympus.auth import PathBlocklist
    from data_olympus.onboarding_inflight import BootstrapInFlight
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
        in_flight=BootstrapInFlight(str(tmp_path / "inflight")),
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
    from data_olympus.onboarding_inflight import BootstrapInFlight
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
        in_flight=BootstrapInFlight(str(tmp_path / "inflight")),
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


# --------------------------------------------------------------------------
# item 1: partial-state bootstrap fills only missing files, never overwrites.
# --------------------------------------------------------------------------

def _bootstrap_low_conf(idx, files, tmp_path, **overrides):
    """Run a low-confidence (pending-path) bootstrap with real queue + guard."""
    from data_olympus.auth import PathBlocklist
    from data_olympus.onboarding_inflight import BootstrapInFlight
    from data_olympus.pending import PendingQueue
    from data_olympus.rate_limit import SlidingWindowLimiter
    pending = overrides.pop("pending", None) or PendingQueue(
        pending_root=str(tmp_path / "p"),
    )
    kwargs = dict(
        idx=idx, workspace="p", component=None,
        workspace_remote_url=None, component_remote_url=None,
        files=files, source_session="s", agent_identity="claude",
        confidence=0.4, confidence_threshold=0.85,  # low -> pending
        worktrees=MagicMock(), push_queue=MagicMock(), pending=pending,
        rate_limiter=SlidingWindowLimiter(max_per_hour=100),
        blocklist=PathBlocklist(tier_blocks=[], path_blocks=[]),
        in_flight=BootstrapInFlight(str(tmp_path / "inflight")),
    )
    kwargs.update(overrides)
    return kb_bootstrap_project_fn(**kwargs), pending


def test_partial_bootstrap_fills_only_missing_files(tmp_path) -> None:
    """A 'partial' workspace (README present, AGENTS missing) bootstraps ONLY the
    missing AGENTS.md; a supplied README is dropped, never overwriting the
    committed copy (item 1)."""
    idx = _partial_idx(["projects/p/README.md"])  # AGENTS.md missing -> partial
    files = [
        {"target_path": "projects/p/README.md", "postimage": "new readme\n"},
        {"target_path": "projects/p/AGENTS.md", "postimage": "agents\n"},
    ]
    resp, pending = _bootstrap_low_conf(idx, files, tmp_path)
    assert resp.status == "pending_confirmation"
    enqueued = [e["target_path"] for e in pending.list()]
    assert enqueued == ["projects/p/AGENTS.md"]  # only the missing file
    assert "projects/p/README.md" not in enqueued  # existing file untouched


def test_partial_bootstrap_nothing_missing_is_rejected(tmp_path) -> None:
    """If every supplied file targets an already-present canonical path, the
    partial bootstrap rejects rather than committing an empty change (item 1)."""
    idx = _partial_idx(["projects/p/README.md"])  # AGENTS.md missing
    # Caller supplies only README (which is present); nothing fills the gap.
    files = [{"target_path": "projects/p/README.md", "postimage": "again\n"}]
    resp, pending = _bootstrap_low_conf(idx, files, tmp_path)
    assert resp.status == "rejected_already_onboarded"
    assert pending.size() == 0


def test_absent_bootstrap_unchanged_writes_all_files(tmp_path) -> None:
    """Regression: the absent flow still enqueues every supplied file (item 1 did
    not narrow the absent path)."""
    idx = MagicMock()
    idx.list_by_prefix.return_value = []  # absent
    idx.list_with_remote_url.return_value = []
    files = [
        {"target_path": "projects/p/README.md", "postimage": "r\n"},
        {"target_path": "projects/p/AGENTS.md", "postimage": "a\n"},
    ]
    resp, pending = _bootstrap_low_conf(idx, files, tmp_path)
    assert resp.status == "pending_confirmation"
    assert sorted(e["target_path"] for e in pending.list()) == [
        "projects/p/AGENTS.md", "projects/p/README.md",
    ]


def test_partial_high_conf_commit_does_not_overwrite_existing(
    tmp_path, real_worktrees,
) -> None:
    """High-confidence partial bootstrap commits only the missing file into the
    worktree; the present file is never written (item 1, commit path)."""
    from data_olympus.auth import PathBlocklist
    from data_olympus.onboarding_inflight import BootstrapInFlight
    from data_olympus.pending import PendingQueue
    from data_olympus.rate_limit import SlidingWindowLimiter
    idx = _partial_idx(["projects/p/README.md"])  # AGENTS.md missing
    files = [
        {"target_path": "projects/p/README.md", "postimage": "SHOULD NOT LAND\n"},
        {"target_path": "projects/p/AGENTS.md", "postimage": "agents body\n"},
    ]
    push_queue = MagicMock()
    resp = kb_bootstrap_project_fn(
        idx=idx, workspace="p", component=None,
        workspace_remote_url=None, component_remote_url=None,
        files=files, source_session="sess-partial", agent_identity="claude",
        confidence=0.95, confidence_threshold=0.85,  # high -> commit
        worktrees=real_worktrees, push_queue=push_queue,
        pending=PendingQueue(pending_root=str(tmp_path / "p")),
        rate_limiter=SlidingWindowLimiter(max_per_hour=100),
        blocklist=PathBlocklist(tier_blocks=[], path_blocks=[]),
        in_flight=BootstrapInFlight(str(tmp_path / "inflight")),
    )
    assert resp.status == "committed"
    wt = real_worktrees.get_or_create(
        source_session="sess-partial", agent_identity="claude",
    )
    committed = subprocess.run(
        ["git", "-C", wt.path, "show", "--name-only", "--format=", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert committed == ["projects/p/AGENTS.md"]  # ONLY the missing file
    assert not (Path(wt.path) / "projects/p/README.md").exists()


# --------------------------------------------------------------------------
# item 2: double-bootstrap within the convergence window is rejected.
# --------------------------------------------------------------------------

def test_double_bootstrap_within_window_rejected(tmp_path, real_worktrees) -> None:
    """After a committed bootstrap, the index still reports absent until it
    converges; a second bootstrap in that window is rejected as in-progress
    (item 2)."""
    from data_olympus.auth import PathBlocklist
    from data_olympus.onboarding_inflight import BootstrapInFlight
    from data_olympus.pending import PendingQueue
    from data_olympus.rate_limit import SlidingWindowLimiter
    idx = MagicMock()
    idx.list_by_prefix.return_value = []  # absent, and STAYS absent (no reindex)
    idx.list_with_remote_url.return_value = []
    guard = BootstrapInFlight(str(tmp_path / "inflight"))

    def _run():
        return kb_bootstrap_project_fn(
            idx=idx, workspace="p", component=None,
            workspace_remote_url=None, component_remote_url=None,
            files=[{"target_path": "projects/p/README.md", "postimage": "r\n"}],
            source_session="sess-dbl", agent_identity="claude",
            confidence=0.95, confidence_threshold=0.85,
            worktrees=real_worktrees, push_queue=MagicMock(),
            pending=PendingQueue(pending_root=str(tmp_path / "p")),
            rate_limiter=SlidingWindowLimiter(max_per_hour=100),
            blocklist=PathBlocklist(tier_blocks=[], path_blocks=[]),
            in_flight=guard,
        )

    first = _run()
    assert first.status == "committed"
    # Index has NOT reindexed yet (still absent); the guard must reject the retry.
    second = _run()
    assert second.status == "rejected_already_in_progress"


def test_expired_inflight_marker_allows_reclaim(tmp_path) -> None:
    """A stale in-flight claim (TTL elapsed) is reclaimed, so a crashed bootstrap
    cannot wedge a workspace forever (item 2)."""
    from data_olympus.onboarding_inflight import BootstrapInFlight
    guard = BootstrapInFlight(str(tmp_path / "inflight"), ttl_seconds=-1.0)
    assert guard.claim("p", None) is True
    # ttl -1 means the marker is already expired; a second claim reclaims it.
    assert guard.claim("p", None) is True


def test_non_committed_bootstrap_releases_claim(tmp_path) -> None:
    """A rejected (non-committed) bootstrap must release its claim so a real retry
    is not blocked for the full TTL (item 2)."""
    from data_olympus.onboarding_inflight import BootstrapInFlight
    idx = MagicMock()
    idx.list_by_prefix.return_value = []
    idx.list_with_remote_url.return_value = []
    guard = BootstrapInFlight(str(tmp_path / "inflight"))
    files = [{"target_path": f"projects/p/f{i}.md", "postimage": "x"} for i in range(3)]
    # too_many_files -> rejected, no side effect, claim released.
    resp, _ = _bootstrap_low_conf(idx, files, tmp_path, in_flight=guard, max_files=2)
    assert resp.status == "rejected_too_many_files"
    # The slot is free again immediately.
    assert guard.claim("p", None) is True


# --------------------------------------------------------------------------
# item 3: lock-busy bundle rolls back completely; bundle_id shared.
# --------------------------------------------------------------------------

def test_lock_busy_bundle_rolls_back_completely(tmp_path) -> None:
    """If one file in a bundle is already path-locked by a pre-existing pending
    entry, the whole bundle rolls back: no partial enqueue, no orphan lock, and a
    whole-bundle rejection (item 3)."""
    from data_olympus.pending import PendingQueue
    idx = MagicMock()
    idx.list_by_prefix.return_value = []
    idx.list_with_remote_url.return_value = []
    pending = PendingQueue(pending_root=str(tmp_path / "p"))
    # Pre-lock the SECOND file so the first enqueues then the second raises busy.
    pre_id = pending.enqueue(
        proposal_type="edit", target_path="projects/p/AGENTS.md",
        postimage="pre\n", base_commit="HEAD", base_blob_sha=None,
        target_file_hash=None, meta={},
    )
    files = [
        {"target_path": "projects/p/README.md", "postimage": "r\n"},
        {"target_path": "projects/p/AGENTS.md", "postimage": "a\n"},  # locked
    ]
    resp, _ = _bootstrap_low_conf(idx, files, tmp_path, pending=pending)
    assert resp.status == "rejected_path_locked"
    assert sorted(resp.rejected_paths) == ["projects/p/AGENTS.md", "projects/p/README.md"]
    # Only the pre-existing entry survives; the bundle's README enqueue rolled back.
    remaining = [e["target_path"] for e in pending.list()]
    assert remaining == ["projects/p/AGENTS.md"]
    assert pending.get(pre_id)  # untouched
    assert pending.locks_held() == 1  # only the pre-existing lock


def test_bundle_id_shared_across_pending_entries(tmp_path) -> None:
    """Every pending entry of one bootstrap carries the same bundle_id (item 3)."""
    from data_olympus.pending import PendingQueue
    idx = MagicMock()
    idx.list_by_prefix.return_value = []
    idx.list_with_remote_url.return_value = []
    pending = PendingQueue(pending_root=str(tmp_path / "p"))
    files = [
        {"target_path": "projects/p/README.md", "postimage": "r\n"},
        {"target_path": "projects/p/AGENTS.md", "postimage": "a\n"},
    ]
    resp, _ = _bootstrap_low_conf(idx, files, tmp_path, pending=pending)
    assert resp.status == "pending_confirmation"
    bundle_ids = {
        pending.get(e["pending_id"])["meta"]["bundle_id"] for e in pending.list()
    }
    assert len(bundle_ids) == 1  # one shared id
    assert next(iter(bundle_ids))  # non-empty


# --------------------------------------------------------------------------
# item 4: remote-url injection is key-aware and newline-safe.
# --------------------------------------------------------------------------

def test_inject_remote_url_body_mention_is_not_a_false_positive() -> None:
    """A URL mentioned only in the body must NOT suppress injection into the
    frontmatter: the presence check is key-aware, not a substring scan (item 4)."""
    import yaml

    from data_olympus.tools_onboarding import _inject_remote_url
    url = "https://github.com/org/repo"
    files = [{"target_path": "projects/p/README.md",
              "postimage": f"---\ntitle: P\n---\n\nsee {url} for details\n"}]
    out = _inject_remote_url(files, url, target_filename="README.md")
    fm = yaml.safe_load(out[0]["postimage"].split("---\n", 2)[1])
    assert fm["git_remote_url"] == url  # injected despite the body mention
    assert fm["title"] == "P"


def test_inject_remote_url_already_present_key_is_noop() -> None:
    """If the frontmatter already carries the exact git_remote_url, the file is
    left byte-identical (key-aware presence check, item 4)."""
    from data_olympus.tools_onboarding import _inject_remote_url
    url = "https://github.com/org/repo"
    postimage = f"---\ngit_remote_url: {url}\ntitle: P\n---\n\nbody\n"
    files = [{"target_path": "projects/p/README.md", "postimage": postimage}]
    out = _inject_remote_url(files, url, target_filename="README.md")
    assert out[0]["postimage"] == postimage
