"""Tests for PendingQueue: enqueue with CAS metadata, same-path lock, resolve."""
from __future__ import annotations

import json

import pytest

from data_olympus.pending import (
    PathLockBusyError,
    PendingQueue,
    PendingQueueFullError,
)


def test_enqueue_respects_capacity(tmp_path) -> None:
    q = PendingQueue(pending_root=str(tmp_path / "p"), cap=2)
    for i in range(2):
        q.enqueue(
            proposal_type="memory", target_path=f"memory/inbox/x{i}.md",
            postimage="b", base_commit="HEAD", base_blob_sha=None,
            target_file_hash=None, meta={},
        )
    with pytest.raises(PendingQueueFullError):
        q.enqueue(
            proposal_type="memory", target_path="memory/inbox/x2.md",
            postimage="b", base_commit="HEAD", base_blob_sha=None,
            target_file_hash=None, meta={},
        )


def test_enqueue_cap_zero_is_unlimited(tmp_path) -> None:
    q = PendingQueue(pending_root=str(tmp_path / "p"))  # cap defaults to 0
    for i in range(5):
        q.enqueue(
            proposal_type="memory", target_path=f"memory/inbox/y{i}.md",
            postimage="b", base_commit="HEAD", base_blob_sha=None,
            target_file_hash=None, meta={},
        )
    assert q.size() == 5


def test_enqueue_memory_writes_postimage(tmp_path) -> None:
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    pid = q.enqueue(
        proposal_type="memory",
        target_path="memory/inbox/2026-06-01-test.md",
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
    assert body["target_path"] == "memory/inbox/2026-06-01-test.md"
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
        proposal_type="memory", target_path="memory/inbox/a.md",
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
        proposal_type="memory", target_path="memory/inbox/a.md",
        postimage="# accept\n", base_commit="c", base_blob_sha=None, target_file_hash=None,
        meta={"confidence": 0.3, "agent_identity": "claude", "source_session": "s"},
    )
    resolved = q.approve(pid)
    assert resolved.target_path == "memory/inbox/a.md"
    assert resolved.postimage == "# accept\n"
    assert resolved.meta["confidence"] == 0.3
    # Lock + entry are cleared after approve.
    assert not (tmp_path / "p" / f"{pid}.json").exists()


def test_resolve_edit_uses_edited_text(tmp_path) -> None:
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    pid = q.enqueue(
        proposal_type="memory", target_path="memory/inbox/a.md",
        postimage="orig", base_commit="c", base_blob_sha=None, target_file_hash=None,
        meta={"confidence": 0.3, "agent_identity": "claude"},
    )
    resolved = q.approve(pid, edited_text="edited!")
    assert resolved.postimage == "edited!"


# ---- item 5: atomic claim + orphan-lock GC ----


def test_sequential_double_approve_second_is_gone(tmp_path) -> None:
    """A SEQUENTIAL second resolve of a fully-consumed id sees PendingNotFoundError
    (the entry is gone). The CONCURRENT race is covered separately below; there
    the loser sees PendingAlreadyResolvedError because the entry vanishes between
    the exists() check and the rename."""
    from data_olympus.pending import PendingNotFoundError
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    pid = q.enqueue(
        proposal_type="memory", target_path="memory/inbox/a.md",
        postimage="body", base_commit="c", base_blob_sha=None,
        target_file_hash=None, meta={},
    )
    first = q.approve(pid)
    assert first.postimage == "body"
    with pytest.raises(PendingNotFoundError):
        q.approve(pid)


def test_concurrent_approve_exactly_one_winner(tmp_path) -> None:
    """Threaded: N threads approve the same id; exactly one succeeds, the rest
    raise PendingAlreadyResolvedError. Proves the os.rename claim is a real
    test-and-set."""
    import threading

    from data_olympus.pending import (
        PendingAlreadyResolvedError,
        PendingNotFoundError,
    )
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    pid = q.enqueue(
        proposal_type="memory", target_path="memory/inbox/race.md",
        postimage="body", base_commit="c", base_blob_sha=None,
        target_file_hash=None, meta={},
    )
    wins: list[int] = []
    losses: list[int] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        try:
            q.approve(pid)
            wins.append(1)
        except (PendingAlreadyResolvedError, PendingNotFoundError):
            # Both mean "you lost the race, the entry is gone". Either is a
            # correct loser outcome; what must NOT happen is two winners.
            losses.append(1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1
    assert len(losses) == 7


def test_gc_orphan_locks_reclaims_lock_without_entry(tmp_path) -> None:
    """A crash between _acquire_lock and the entry write leaves a lock with no
    entry; gc_orphan_locks reclaims it so the path is not locked forever."""
    import json
    import uuid

    q = PendingQueue(pending_root=str(tmp_path / "p"))
    # Simulate the crash: acquire a lock for a pending_id whose entry never lands.
    orphan_id = uuid.uuid4().hex
    q._acquire_lock("decisions/orphan.md", orphan_id)  # noqa: SLF001
    assert q.locks_held() == 1
    reclaimed = q.gc_orphan_locks()
    assert reclaimed == 1
    assert q.locks_held() == 0
    # A live lock (with entry) is NOT reclaimed.
    pid = q.enqueue(
        proposal_type="edit", target_path="decisions/live.md",
        postimage="x", base_commit="c", base_blob_sha=None, target_file_hash=None,
        meta={},
    )
    assert q.gc_orphan_locks() == 0
    assert q.locks_held() == 1
    _ = json, pid


def test_path_lock_shared_context_manager(tmp_path) -> None:
    """The auto-commit path acquires the SAME lock the pending queue uses, so a
    path with a pending proposal cannot be auto-committed concurrently."""
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    q.enqueue(
        proposal_type="edit", target_path="universal/foundation/x.md",
        postimage="a", base_commit="c", base_blob_sha=None, target_file_hash=None,
        meta={},
    )
    # The auto-commit path_lock on the same target must be busy.
    with pytest.raises(PathLockBusyError), q.path_lock(
        "universal/foundation/x.md", owner="auto-commit:s",
    ):
        pass
