"""Tests for PendingQueue: enqueue with CAS metadata, same-path lock, resolve."""
from __future__ import annotations

import contextlib
import json
import os
import time

import pytest

from data_olympus import pending as pending_module
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


# ---- issue #109: provenance surfacing (source_session, reason, evidence) ----


def test_list_surfaces_provenance_fields_when_present(tmp_path) -> None:
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    pid = q.enqueue(
        proposal_type="edit", target_path="universal/foundation/x.md",
        postimage="b", base_commit="c", base_blob_sha=None, target_file_hash=None,
        meta={
            "confidence": 0.4, "agent_identity": "claude",
            "source_session": "sess-123", "reason": "fixing a typo",
            "evidence": ["saw it in the logs", "confirmed with operator"],
        },
    )
    entry = next(e for e in q.list() if e["pending_id"] == pid)
    assert entry["source_session"] == "sess-123"
    assert entry["reason"] == "fixing a typo"
    assert entry["evidence"] == ["saw it in the logs", "confirmed with operator"]


def test_list_omits_provenance_fields_cleanly_when_absent(tmp_path) -> None:
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    pid = q.enqueue(
        proposal_type="memory", target_path="memory/inbox/a.md",
        postimage="a", base_commit="c", base_blob_sha=None, target_file_hash=None,
        meta={"confidence": 0.3, "agent_identity": "claude"},
    )
    entry = next(e for e in q.list() if e["pending_id"] == pid)
    assert entry["source_session"] is None
    assert entry["reason"] is None
    assert entry["evidence"] is None


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


# --- claimed-entry visibility (issues #253, #254) ----------------------------


def _enqueued(q: PendingQueue, path: str = "operator/notes.md") -> str:
    return q.enqueue(
        proposal_type="edit", target_path=path, postimage="body",
        base_commit="HEAD", base_blob_sha=None, target_file_hash=None,
        meta={"confidence": 0.4, "agent_identity": "test"},
    )


def test_list_labels_a_pending_entry_with_its_state(tmp_path) -> None:
    """Every listed entry says which state it is in. Without the field a caller
    cannot tell a live proposal from one stranded mid-resolve."""
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    _enqueued(q)
    entries = q.list()

    assert [e["state"] for e in entries] == ["pending"]


def test_list_reports_a_claimed_entry_instead_of_hiding_it(tmp_path) -> None:
    """The defect in issue #254: a resolve that is interrupted after the claim
    leaves the entry in a state that is neither pending, nor committed, nor
    visible. `kb_list_pending` read as empty, which is indistinguishable from
    'the decision was applied', so the operator believed the write landed."""
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    pending_id = _enqueued(q)
    q.claim_for_resolve(pending_id)

    entries = q.list()

    assert [e["pending_id"] for e in entries] == [pending_id]
    assert entries[0]["state"] == "claimed"
    assert entries[0]["target_path"] == "operator/notes.md"


def test_size_still_counts_only_resolvable_entries(tmp_path) -> None:
    """A claimed entry is visible but is NOT awaiting an operator decision, so
    it must not inflate pending_count or consume queue capacity."""
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    pending_id = _enqueued(q)
    assert q.size() == 1
    q.claim_for_resolve(pending_id)
    assert q.size() == 0


def test_held_locks_report_their_path_and_age(tmp_path) -> None:
    """Issue #253 asked for this by name: `path_locks_held` was a bare count, so
    finding WHICH path was wedged meant exec-ing into the pod and reading files
    off the state volume."""
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    before = time.time()
    _enqueued(q, "operator/agent-overrides/claude.md")

    held = q.held_locks()

    assert len(held) == 1
    assert held[0]["target_path"] == "operator/agent-overrides/claude.md"
    assert held[0]["owner_kind"] == "pending"
    assert held[0]["acquired_at"] >= before
    assert held[0]["age_seconds"] >= 0


def test_held_locks_survive_the_claim_that_keeps_them(tmp_path) -> None:
    """The lock is held across claim -> commit by design, so a claimed entry's
    lock must still be reported with the claim as its owner."""
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    pending_id = _enqueued(q)
    q.claim_for_resolve(pending_id)

    held = q.held_locks()

    assert len(held) == 1
    assert held[0]["pending_id"] == pending_id
    assert held[0]["target_path"] == "operator/notes.md"


def test_held_locks_is_empty_with_no_locks(tmp_path) -> None:
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    assert q.held_locks() == []


# --- fenced completion and outcome reconciliation (issues #253, #254) --------


def _claimed(q: PendingQueue, path: str = "operator/notes.md"):
    pending_id = _enqueued(q, path)
    resolved = q.claim_for_resolve(pending_id)
    return pending_id, resolved


def test_claim_stamps_a_token_and_a_time(tmp_path) -> None:
    """The claim is an explicit record, not an inference from a rename. A rename
    carries no reliable timestamp and no ownership token, and both are needed to
    tell an abandoned claim from a live resolver."""
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    pending_id, resolved = _claimed(q)

    assert resolved.claim_token
    record = q.claim_record(pending_id)
    assert record["claim_token"] == resolved.claim_token
    assert record["claimed_at"] > 0


def test_finalize_is_fenced_on_the_claim_token(tmp_path) -> None:
    """A resolver reclaimed while it was queued must not release a lock or
    delete a sidecar that now belong to a successor."""
    from data_olympus.pending import ClaimFencedError

    q = PendingQueue(pending_root=str(tmp_path / "p"))
    pending_id, resolved = _claimed(q)

    with pytest.raises(ClaimFencedError):
        q.finalize_resolve(
            pending_id, "operator/notes.md", claim_token="stale-token",
        )

    # Nothing moved: the successor's state is intact.
    assert q.claim_record(pending_id)["claim_token"] == resolved.claim_token
    assert q.locks_held() == 1


def test_restore_is_fenced_on_the_claim_token(tmp_path) -> None:
    from data_olympus.pending import ClaimFencedError

    q = PendingQueue(pending_root=str(tmp_path / "p"))
    pending_id, _resolved = _claimed(q)

    with pytest.raises(ClaimFencedError):
        q.restore_resolve(pending_id, claim_token="stale-token")

    assert [e["state"] for e in q.list()] == ["claimed"]


def test_a_recorded_commit_is_finalized_never_re_presented(tmp_path) -> None:
    """Positive evidence that the write committed. Re-resolving a committed
    entry would duplicate it, so it is consumed, not restored."""
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    pending_id, resolved = _claimed(q)
    q.record_outcome(
        pending_id, claim_token=resolved.claim_token, commit_sha="a" * 40,
    )

    results = q.reconcile_claims(min_age_sec=0)

    assert [r["outcome"] for r in results] == ["committed"]
    assert q.list() == []
    assert q.locks_held() == 0


def test_a_recorded_failure_is_restored_to_pending(tmp_path) -> None:
    """Positive evidence that the write did NOT commit is the only thing that
    authorises a restore."""
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    pending_id, resolved = _claimed(q)
    q.record_outcome(
        pending_id, claim_token=resolved.claim_token, failure="commit rejected",
    )

    results = q.reconcile_claims(min_age_sec=0)

    assert [r["outcome"] for r in results] == ["restored"]
    entries = q.list()
    assert [e["state"] for e in entries] == ["pending"]
    assert entries[0]["pending_id"] == pending_id
    # The path lock is preserved across the restore: the entry is live again.
    assert q.locks_held() == 1


def test_an_unrecorded_outcome_searches_for_the_commit(tmp_path) -> None:
    """The one window the search covers: git may have committed but no durable
    outcome was recorded. Finding the claim-linked commit proves it did."""
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    pending_id, _resolved = _claimed(q)
    seen = []

    def find_commit(record):  # noqa: ANN001, ANN202
        seen.append(record["pending_id"])
        return True

    results = q.reconcile_claims(min_age_sec=0, find_commit=find_commit)

    assert seen == [pending_id]
    assert [r["outcome"] for r in results] == ["committed"]
    assert q.locks_held() == 0


def test_a_negative_search_is_uncertain_and_never_a_restore(tmp_path) -> None:
    """The refuted rule. A negative search cannot prove non-commitment: a rebase
    or a squash can drop the trailer-bearing commit while leaving a readable
    branch, so restoring on absence would re-present an already-committed
    decision. Absence yields uncertain, and the lock is kept."""
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    pending_id, _resolved = _claimed(q)

    results = q.reconcile_claims(min_age_sec=0, find_commit=lambda _r: False)

    assert [r["outcome"] for r in results] == ["uncertain"]
    entries = q.list()
    assert [e["state"] for e in entries] == ["uncertain"]
    assert entries[0]["pending_id"] == pending_id
    assert q.locks_held() == 1
    assert results[0]["reason"]


def test_age_alone_never_resolves_a_claim(tmp_path) -> None:
    """The TTL selects an entry for reconciliation. It never decides the
    outcome: with no recorded outcome and no way to search, the answer is
    uncertain, not a restore and not a finalize."""
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    pending_id, _resolved = _claimed(q)

    results = q.reconcile_claims(min_age_sec=0)

    assert [r["outcome"] for r in results] == ["uncertain"]
    assert q.locks_held() == 1
    assert [e["pending_id"] for e in q.list()] == [pending_id]


def test_a_fresh_claim_is_left_alone(tmp_path) -> None:
    """A live resolver's claim must not be reclaimed out from under it."""
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    _claimed(q)

    assert q.reconcile_claims(min_age_sec=3600) == []
    assert [e["state"] for e in q.list()] == ["claimed"]


def test_an_uncertain_claim_is_retried_and_can_still_resolve(tmp_path) -> None:
    """A transient unreadable repository must heal itself. Reconciliation runs
    on every sweep rather than deciding once and giving up."""
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    _claimed(q)

    assert [r["outcome"] for r in q.reconcile_claims(
        min_age_sec=0, find_commit=lambda _r: None)] == ["uncertain"]
    assert [r["outcome"] for r in q.reconcile_claims(
        min_age_sec=0, find_commit=lambda _r: True)] == ["committed"]
    assert q.locks_held() == 0


def test_reconcile_records_the_write_context_for_the_search(tmp_path) -> None:
    """The searcher needs the ref the commit would be on and an immutable
    pre-write sha, both captured BEFORE the write."""
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    pending_id, resolved = _claimed(q)
    q.record_write_context(
        pending_id, claim_token=resolved.claim_token,
        session_ref="kb-session/abc", pre_write_ref_sha="b" * 40,
    )
    captured = {}

    q.reconcile_claims(min_age_sec=0,
                       find_commit=lambda r: captured.update(r) or True)

    assert captured["session_ref"] == "kb-session/abc"
    assert captured["pre_write_ref_sha"] == "b" * 40
    assert captured["target_path"] == "operator/notes.md"


def test_claims_at_risk_only_counts_writes_that_may_have_committed(tmp_path) -> None:
    """A rebase or a branch deletion can destroy the evidence recovery needs, so
    those operations defer while a claim on that ref might have a commit.

    "Might have a commit" starts at the write, not at the claim: a claim that has
    not written anything has no commit to lose, and treating it as at risk would
    deadlock the resolve's own refresh_base against its own claim.
    """
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    pending_id, resolved = _claimed(q)

    assert q.claims_at_risk("kb-session/abc") == []

    q.record_write_context(
        pending_id, claim_token=resolved.claim_token,
        session_ref="kb-session/abc", pre_write_ref_sha="b" * 40,
    )
    at_risk = q.claims_at_risk("kb-session/abc")
    assert [c["pending_id"] for c in at_risk] == [pending_id]
    assert q.claims_at_risk("kb-session/other") == []


def test_a_finalized_claim_is_no_longer_at_risk(tmp_path) -> None:
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    pending_id, resolved = _claimed(q)
    q.record_write_context(
        pending_id, claim_token=resolved.claim_token,
        session_ref="kb-session/abc", pre_write_ref_sha="b" * 40,
    )
    q.record_outcome(
        pending_id, claim_token=resolved.claim_token, commit_sha="a" * 40,
    )
    q.reconcile_claims(min_age_sec=0)

    assert q.claims_at_risk("kb-session/abc") == []


# --- the interleavings the round-1 review reproduced --------------------------


def test_a_successor_cannot_claim_midway_through_a_restore(tmp_path) -> None:
    """The interleaving the review reproduced: A publishes the restored entry,
    B claims it and stamps B's token, then A's delete removes B's sidecar and
    NEITHER entry file is left.

    Publishing through a rename removes a torn-write window but does not close
    this one on its own, because the restored entry is visible while the claimed
    sidecar still exists. What closes it is holding the shared write serializer
    across the WHOLE transition. So this asserts the property that matters: a
    concurrent claim cannot complete while a restore is mid-transition.
    """
    import threading

    from data_olympus.write_gate import WriteSerializer

    q = PendingQueue(pending_root=str(tmp_path / "p"), serializer=WriteSerializer())
    pending_id, resolved = _claimed(q)

    midway = threading.Event()
    claimed_during = threading.Event()
    real_remove = pending_module.atomic_remove

    def slow_remove(path: str) -> None:
        # The entry is published and the sidecar is not yet gone: exactly the
        # point at which the successor used to get in.
        midway.set()
        claimed_during.wait(timeout=0.5)
        real_remove(path)

    def successor() -> None:
        midway.wait(timeout=5)
        with contextlib.suppress(Exception):
            q.claim_for_resolve(pending_id)
        claimed_during.set()

    thread = threading.Thread(target=successor)
    pending_module.atomic_remove = slow_remove
    try:
        thread.start()
        q.restore_resolve(pending_id, claim_token=resolved.claim_token)
        completed_during_restore = claimed_during.is_set()
        thread.join(timeout=10)
    finally:
        pending_module.atomic_remove = real_remove

    assert not completed_during_restore, (
        "a claim completed while a restore was mid-transition; the serializer "
        "does not cover the whole claim lifecycle"
    )
    # And whatever ran, the entry still exists: one of the two files, never none.
    root = str(tmp_path / "p")
    final = {n for n in os.listdir(root) if n.startswith(pending_id)}
    assert final in ({f"{pending_id}.json"}, {f"{pending_id}.claimed"}), final


def test_a_reclaimed_resolver_cannot_release_a_successors_lock(tmp_path) -> None:
    """A passes its token check, reconciliation finalizes A, a successor claims
    the path, and A resumes. A must mutate nothing."""
    from data_olympus.pending import ClaimFencedError

    q = PendingQueue(pending_root=str(tmp_path / "p"))
    pending_id, first = _claimed(q)
    q.record_outcome(
        pending_id, claim_token=first.claim_token, failure="rejected",
    )
    q.reconcile_claims(min_age_sec=0)          # restores it
    second = q.claim_for_resolve(pending_id)   # the successor

    with pytest.raises(ClaimFencedError):
        q.finalize_resolve(
            pending_id, "operator/notes.md", claim_token=first.claim_token,
        )

    assert q.locks_held() == 1
    assert q.claim_record(pending_id)["claim_token"] == second.claim_token


def test_an_unreadable_claim_record_is_reported_not_skipped(tmp_path) -> None:
    """A damaged approval record is the invisible-entry defect this batch
    fixes, so it must never be swallowed as a rename race."""
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    pending_id, _resolved = _claimed(q)
    claimed_path = os.path.join(str(tmp_path / "p"), f"{pending_id}.claimed")
    with open(claimed_path, "w") as f:
        f.write("{ this is not json")

    entries = q.list()
    assert [e["state"] for e in entries] == ["unreadable"]
    # It also blocks a rewrite, because it MIGHT be a claim with a commit.
    assert q.claims_at_risk("kb-session/anything")
    # And reconciliation reports it rather than deciding an outcome.
    assert [r["outcome"] for r in q.reconcile_claims(min_age_sec=0)] == ["uncertain"]
    assert q.locks_held() == 1


def test_a_rejection_is_not_resurrected_by_reconciliation(tmp_path) -> None:
    """The reviewer reproduced this: a rejection claims an entry, reconciliation
    reads that fresh claim, the rejection finishes and releases its lock, and
    reconciliation writes its stale snapshot back. The result was a completed
    rejection resurrected as an uncertain claimed entry with no lock.
    """
    import threading

    from data_olympus.write_gate import WriteSerializer

    q = PendingQueue(pending_root=str(tmp_path / "p"), serializer=WriteSerializer())
    pending_id = _enqueued(q)

    started = threading.Event()
    outcomes: list[object] = []

    def reconcile() -> None:
        started.wait(timeout=5)
        outcomes.append(q.reconcile_claims(min_age_sec=0))

    thread = threading.Thread(target=reconcile)
    thread.start()
    started.set()
    q.reject(pending_id)
    thread.join(timeout=10)

    assert q.list() == [], q.list()
    assert q.locks_held() == 0
    root = str(tmp_path / "p")
    assert not [n for n in os.listdir(root) if n.startswith(pending_id)]


def test_an_undecodable_claim_record_does_not_stop_the_sweep(tmp_path) -> None:
    """Invalid UTF-8 used to raise out of list(), reconciliation and risk
    inspection, so one damaged record disabled recovery for every other claim."""
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    broken_id, _ = _claimed(q, "operator/broken.md")
    good_id, good = _claimed(q, "operator/good.md")
    q.record_outcome(good_id, claim_token=good.claim_token, commit_sha="a" * 40)
    with open(os.path.join(str(tmp_path / "p"), f"{broken_id}.claimed"), "wb") as f:
        f.write(b"\xff\xfe not utf-8")

    states = {e["pending_id"]: e["state"] for e in q.list()}
    assert states[broken_id] == "unreadable"

    outcomes = {r["pending_id"]: r["outcome"]
                for r in q.reconcile_claims(min_age_sec=0)}
    assert outcomes[broken_id] == "uncertain"
    assert outcomes[good_id] == "committed", "one bad record stopped the sweep"
    assert q.claims_at_risk("kb-session/anything")
