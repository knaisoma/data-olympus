"""Tests for PendingQueue: enqueue with CAS metadata, same-path lock, resolve."""
from __future__ import annotations

import json

from data_olympus.pending import PathLockBusyError, PendingQueue


def test_enqueue_memory_writes_postimage(tmp_path) -> None:
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    pid = q.enqueue(
        proposal_type="memory",
        target_path="operator/memory/inbox/2026-06-01-test.md",
        postimage="# test\nbody\n",
        base_commit="0123abc",
        base_blob_sha=None,
        target_file_hash=None,
        meta={"agent_identity": "claude", "source_session": "s", "confidence": 0.4},
    )
    entry_path = tmp_path / "p" / f"{pid}.json"
    assert entry_path.exists()
    body = json.loads(entry_path.read_text())
    assert body["postimage"] == "# test\nbody\n"
    assert body["target_path"] == "operator/memory/inbox/2026-06-01-test.md"
    assert body["base_commit"] == "0123abc"


def test_enqueue_edit_captures_base_blob_sha_and_file_hash(tmp_path) -> None:
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    pid = q.enqueue(
        proposal_type="edit",
        target_path="universal/foundation/STD-U-001.md",
        postimage="new body\n",
        base_commit="0123abc",
        base_blob_sha="blob-sha-1",
        target_file_hash="file-hash-1",
        meta={"agent_identity": "claude", "source_session": "s", "confidence": 0.5},
    )
    body = json.loads((tmp_path / "p" / f"{pid}.json").read_text())
    assert body["base_blob_sha"] == "blob-sha-1"
    assert body["target_file_hash"] == "file-hash-1"


def test_path_lock_blocks_concurrent_enqueue_to_same_target(tmp_path) -> None:
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    q.enqueue(
        proposal_type="edit", target_path="universal/foundation/x.md",
        postimage="a", base_commit="c", base_blob_sha=None, target_file_hash=None,
        meta={},
    )
    import pytest
    with pytest.raises(PathLockBusyError):
        q.enqueue(
            proposal_type="edit", target_path="universal/foundation/x.md",
            postimage="b", base_commit="c", base_blob_sha=None, target_file_hash=None,
            meta={},
        )


def test_path_lock_releases_on_reject(tmp_path) -> None:
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    pid = q.enqueue(
        proposal_type="edit", target_path="universal/foundation/x.md",
        postimage="a", base_commit="c", base_blob_sha=None, target_file_hash=None,
        meta={},
    )
    q.reject(pid)
    # Second enqueue on same target now succeeds.
    q.enqueue(
        proposal_type="edit", target_path="universal/foundation/x.md",
        postimage="b", base_commit="c", base_blob_sha=None, target_file_hash=None,
        meta={},
    )


def test_list_pending_returns_active_entries(tmp_path) -> None:
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    p1 = q.enqueue(
        proposal_type="memory", target_path="operator/memory/inbox/a.md",
        postimage="a", base_commit="c", base_blob_sha=None, target_file_hash=None,
        meta={"confidence": 0.3, "agent_identity": "claude"},
    )
    p2 = q.enqueue(
        proposal_type="edit", target_path="universal/foundation/x.md",
        postimage="b", base_commit="c", base_blob_sha=None, target_file_hash=None,
        meta={"confidence": 0.4, "agent_identity": "claude"},
    )
    entries = q.list()
    ids = {e["pending_id"] for e in entries}
    assert {p1, p2} <= ids


def test_resolve_approve_returns_postimage_and_metadata(tmp_path) -> None:
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    pid = q.enqueue(
        proposal_type="memory", target_path="operator/memory/inbox/a.md",
        postimage="# accept\n", base_commit="c", base_blob_sha=None, target_file_hash=None,
        meta={"confidence": 0.3, "agent_identity": "claude", "source_session": "s"},
    )
    resolved = q.approve(pid)
    assert resolved.target_path == "operator/memory/inbox/a.md"
    assert resolved.postimage == "# accept\n"
    assert resolved.meta["confidence"] == 0.3
    # Lock + entry are cleared after approve.
    assert not (tmp_path / "p" / f"{pid}.json").exists()


def test_resolve_edit_uses_edited_text(tmp_path) -> None:
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    pid = q.enqueue(
        proposal_type="memory", target_path="operator/memory/inbox/a.md",
        postimage="orig", base_commit="c", base_blob_sha=None, target_file_hash=None,
        meta={"confidence": 0.3, "agent_identity": "claude"},
    )
    resolved = q.approve(pid, edited_text="edited!")
    assert resolved.postimage == "edited!"
