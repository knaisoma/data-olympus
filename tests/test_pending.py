"""Tests for PendingQueue: enqueue with CAS metadata, same-path lock, resolve."""
from __future__ import annotations

import json
import os
import time

import pytest

from data_olympus.pending import (
    PathLockBusyError,
    PendingQueue,
    PendingQueueFullError,
    _path_lock_filename,
)


def test_root_property_exposes_pending_dir(tmp_path) -> None:
    """The public `root` property returns the on-disk pending dir (so callers do
    not read the private `_root`)."""
    root = str(tmp_path / "p")
    q = PendingQueue(pending_root=root)
    assert q.root == root


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


def _lock_file(q: PendingQueue, target_path: str) -> str:
    return os.path.join(q._locks_dir, _path_lock_filename(target_path))  # noqa: SLF001


def _backdate_lock(q: PendingQueue, target_path: str, seconds: float) -> None:
    """Rewrite a lock file's acquired_at to ``seconds`` ago (simulate a crash that
    left an old lock behind)."""
    p = _lock_file(q, target_path)
    with open(p) as f:
        info = json.load(f)
    info["acquired_at"] = time.time() - seconds
    with open(p, "w") as f:
        json.dump(info, f)


def test_reclaim_stale_auto_commit_lock_via_ttl(tmp_path) -> None:
    """A crash while an auto-commit held a path lock leaves it on disk; once older
    than the TTL, reclaim_stale_auto_commit_locks frees it so the path is usable."""
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    # Simulate the crash: acquire the auto-commit lock but never release it.
    q._acquire_lock("decisions/a.md", "auto-commit:sess", owner_kind="auto_commit")  # noqa: SLF001
    assert q.locks_held() == 1
    # Not yet stale (default TTL far larger than age): NOT reclaimed.
    assert q.reclaim_stale_auto_commit_locks(max_age_sec=600) == 0
    assert q.locks_held() == 1
    # Age it past the TTL: reclaimed.
    _backdate_lock(q, "decisions/a.md", 700)
    assert q.reclaim_stale_auto_commit_locks(max_age_sec=600) == 1
    assert q.locks_held() == 0


def test_reclaim_startup_frees_auto_commit_locks_unconditionally(tmp_path) -> None:
    """max_age_sec=0 (the startup sweep) reclaims a FRESH auto-commit lock: a fresh
    process provably holds none, so any present lock is a crash orphan."""
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    q._acquire_lock("decisions/b.md", "auto-commit:sess", owner_kind="auto_commit")  # noqa: SLF001
    assert q.locks_held() == 1
    assert q.reclaim_stale_auto_commit_locks(max_age_sec=0) == 1
    assert q.locks_held() == 0


def test_reclaim_does_not_touch_fresh_auto_commit_lock(tmp_path) -> None:
    """A currently-held (fresh) auto-commit lock is NOT reclaimed by the TTL path."""
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    with q.path_lock("decisions/c.md", owner="auto-commit:live"):
        # While the lock is genuinely held, the TTL reclaim must leave it alone.
        assert q.reclaim_stale_auto_commit_locks(max_age_sec=600) == 0
        assert q.locks_held() == 1
    # Released cleanly on exit.
    assert q.locks_held() == 0


def test_reclaim_never_touches_pending_proposal_locks(tmp_path) -> None:
    """Pending-proposal locks live until resolve/expiry: neither the TTL sweep nor
    the unconditional startup sweep may reclaim them, even when very old."""
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    q.enqueue(
        proposal_type="edit", target_path="decisions/pending.md",
        postimage="x", base_commit="c", base_blob_sha=None, target_file_hash=None,
        meta={},
    )
    assert q.locks_held() == 1
    # Even if the pending lock is ancient, it is not an auto-commit lock.
    _backdate_lock(q, "decisions/pending.md", 10_000)
    assert q.reclaim_stale_auto_commit_locks(max_age_sec=600) == 0
    assert q.reclaim_stale_auto_commit_locks(max_age_sec=0) == 0
    assert q.locks_held() == 1


def test_reclaimed_lock_release_does_not_delete_successor(tmp_path) -> None:
    """The reclaim race: a stale auto-commit holder is reclaimed, a successor grabs
    the path, then the stale holder finally releases. The ownership-checked release
    must NOT delete the successor's lock (acquired_at differs)."""
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    target = "decisions/race.md"
    stale_acquired = q._acquire_lock(  # noqa: SLF001
        target, "auto-commit:stale", owner_kind="auto_commit",
    )
    # Reclaim it (as if TTL-expired).
    _backdate_lock(q, target, 700)
    assert q.reclaim_stale_auto_commit_locks(max_age_sec=600) == 1
    # Successor grabs the freed path.
    q._acquire_lock(target, "auto-commit:successor", owner_kind="auto_commit")  # noqa: SLF001
    assert q.locks_held() == 1
    # The stale holder resumes and releases with ITS acquired_at: must be a no-op.
    q._release_lock(target, expected_acquired_at=stale_acquired)  # noqa: SLF001
    assert q.locks_held() == 1
    with open(_lock_file(q, target)) as f:
        assert json.load(f)["pending_id"] == "auto-commit:successor"


def test_reclaimer_check_to_delete_race_spares_successor(tmp_path) -> None:
    """The reclaimer's OWN check-to-delete window: reclaim reads a stale lock and
    passes the age check, then a successor replaces the file before the unlink. The
    ownership-checked delete must remove ONLY the exact lock inspected, so the
    successor's lock survives. Simulated by swapping the file in during the
    reclaim's _release_lock call."""
    target = "decisions/reclaim-race.md"

    class RacyQueue(PendingQueue):
        def _release_lock(self, tp, *, expected_acquired_at=None):  # noqa: ANN001, ANN204
            # Simulate the successor grabbing the freed path in the race window,
            # AFTER reclaim decided this lock is stale but BEFORE it deletes.
            if expected_acquired_at is not None and not getattr(
                self, "_raced", False,
            ):
                self._raced = True
                lock_path = _lock_file(self, tp)
                with open(lock_path, "w") as f:
                    json.dump(
                        {"pending_id": "auto-commit:successor",
                         "target_path": tp, "owner_kind": "auto_commit",
                         "acquired_at": time.time()}, f,
                    )
            super()._release_lock(tp, expected_acquired_at=expected_acquired_at)

    q = RacyQueue(pending_root=str(tmp_path / "p"))
    q._acquire_lock(target, "auto-commit:stale", owner_kind="auto_commit")  # noqa: SLF001
    _backdate_lock(q, target, 700)
    reclaimed = q.reclaim_stale_auto_commit_locks(max_age_sec=600)
    # The stale lock was NOT actually removed (its acquired_at no longer matches the
    # successor's), so the ownership-checked release is a no-op and the successor
    # lock survives.
    assert reclaimed == 0
    assert q.locks_held() == 1
    with open(_lock_file(q, target)) as f:
        assert json.load(f)["pending_id"] == "auto-commit:successor"


def test_release_lock_spares_successor_with_different_timestamp(tmp_path) -> None:
    """A real successor acquires with its own time.time() acquired_at (distinct to
    sub-microsecond), so the ownership check's content compare rejects it: the
    release is a no-op and reports no removal. This is the physically reachable
    case (a colliding acquired_at cannot occur: each acquire stamps a fresh now)."""
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    target = "decisions/inode.md"
    stale_acquired = q._acquire_lock(  # noqa: SLF001
        target, "auto-commit:stale", owner_kind="auto_commit",
    )
    lock_path = _lock_file(q, target)
    # Successor replaces the file (fresh inode, fresh timestamp), as a real acquire
    # would after the stale lock was freed.
    os.unlink(lock_path)
    successor_acquired = q._acquire_lock(  # noqa: SLF001
        target, "auto-commit:successor", owner_kind="auto_commit",
    )
    assert successor_acquired != stale_acquired
    # A release keyed to the STALE token must not touch the successor.
    assert q._release_lock(  # noqa: SLF001
        target, expected_acquired_at=stale_acquired,
    ) is False
    assert q.locks_held() == 1
    with open(lock_path) as f:
        assert json.load(f)["pending_id"] == "auto-commit:successor"


def test_release_lock_returns_true_on_own_lock(tmp_path) -> None:
    """The ownership-checked release removes and reports True for the exact lock the
    caller acquired."""
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    acquired = q._acquire_lock("decisions/mine.md", "auto-commit:me",  # noqa: SLF001
                               owner_kind="auto_commit")
    assert q._release_lock("decisions/mine.md",  # noqa: SLF001
                           expected_acquired_at=acquired) is True
    assert q.locks_held() == 0


def test_config_clamps_nonpositive_auto_commit_lock_ttl(monkeypatch) -> None:
    """A non-positive KB_AUTO_COMMIT_LOCK_TTL_SEC is clamped to the 600s default so
    the periodic loop never receives the max_age_sec=0 startup sentinel."""
    from data_olympus.config import load_config

    for bad in ("0", "-5"):
        monkeypatch.setenv("KB_AUTO_COMMIT_LOCK_TTL_SEC", bad)
        monkeypatch.setenv("KB_HTTP_PORT", "8080")
        cfg = load_config()
        assert cfg.auto_commit_lock_ttl_sec == 600


def test_reclaim_under_serializer_never_deletes_successor(tmp_path) -> None:
    """The two-deleter race is closed by running the reclaim under the SAME
    serializer path_lock uses: reclaim (delete) and a holder's release+reacquire
    (the two deleters) can no longer interleave, so the reclaimer's unlink can
    never land on a successor that a holder freed the path for mid-scan.

    Whichever of {reclaimer, holder} wins the serializer, the INVARIANT holds: the
    successor lock is never deleted. If the reclaimer runs first it removes the
    genuinely-stale lock (reclaimed==1) and the holder then reacquires; if the
    holder runs first the stale lock is already gone and the reclaimer removes
    nothing (reclaimed==0). Random jitter before each side's serializer acquire is
    used to shake out both orderings; we assert both orderings actually occur so
    the test cannot silently degrade to exercising one schedule."""
    import random
    import threading

    outcomes: set[int] = set()
    for trial in range(60):
        q = PendingQueue(pending_root=str(tmp_path / f"p{trial}"))
        target = "decisions/serialized.md"
        q._acquire_lock(  # noqa: SLF001
            target, "auto-commit:stale", owner_kind="auto_commit",
        )
        _backdate_lock(q, target, 700)
        serializer = threading.RLock()

        def holder(q=q, target=target, serializer=serializer) -> None:
            # Mimic tools_write: the whole acquire..release runs UNDER the
            # serializer. This holder resumes, releases the (stale) lock it held,
            # and a successor acquires the freed path, all atomically vs reclaim.
            time.sleep(random.uniform(0, 0.002))  # noqa: S311
            with serializer:
                q._release_lock(target)  # noqa: SLF001
                q._acquire_lock(  # noqa: SLF001
                    target, "auto-commit:successor", owner_kind="auto_commit",
                )

        reclaimed_box: list[int] = []

        def reclaimer(q=q, serializer=serializer, box=reclaimed_box) -> None:
            time.sleep(random.uniform(0, 0.002))  # noqa: S311
            box.append(q.reclaim_stale_auto_commit_locks(
                max_age_sec=600, serializer=serializer,
            ))

        th = threading.Thread(target=holder)
        rt = threading.Thread(target=reclaimer)
        th.start()
        rt.start()
        th.join()
        rt.join()

        # Invariant across every ordering: successor present, never deleted.
        assert q.locks_held() == 1
        with open(_lock_file(q, target)) as f:
            assert json.load(f)["pending_id"] == "auto-commit:successor"
        assert reclaimed_box[0] in (0, 1)
        outcomes.add(reclaimed_box[0])
    # Both orderings were actually hit (reclaimer-first removes 1; holder-first
    # removes 0). If jitter ever fails to produce both, the invariant above is
    # what really matters, but this guards against a silently one-sided schedule.
    assert outcomes == {0, 1}


def test_reclaim_recognises_legacy_pre_fix_auto_commit_lock(tmp_path) -> None:
    """A lock written by a PRE-fix build has no owner_kind and stashes the
    auto-commit marker in pending_id ("auto-commit:..."). After upgrade on a
    persistent /state volume, the reclaimer must still recognise and free it (once
    stale) so an old wedged path is not stuck forever. A legacy UUID-holder pending
    lock must NOT be mistaken for auto-commit."""
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    lock_path = _lock_file(q, "decisions/legacy.md")
    # Hand-write a pre-fix auto-commit lock: pending_id has the prefix, NO owner_kind.
    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"pending_id": "auto-commit:oldsess",
                   "target_path": "decisions/legacy.md",
                   "acquired_at": time.time() - 700}, f)
    assert q.locks_held() == 1
    # Fresh legacy lock (aged 700s) reclaimed under a 600s TTL.
    assert q.reclaim_stale_auto_commit_locks(max_age_sec=600) == 1
    assert q.locks_held() == 0

    # A legacy pending lock (UUID holder, no owner_kind) must be left alone.
    import uuid
    pending_lock = _lock_file(q, "decisions/legacy-pending.md")
    fd2 = os.open(pending_lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd2, "w") as f:
        json.dump({"pending_id": uuid.uuid4().hex,
                   "target_path": "decisions/legacy-pending.md",
                   "acquired_at": time.time() - 10_000}, f)
    assert q.reclaim_stale_auto_commit_locks(max_age_sec=600) == 0
    assert q.reclaim_stale_auto_commit_locks(max_age_sec=0) == 0
    assert q.locks_held() == 1
