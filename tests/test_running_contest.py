"""Tests for derive_running_contest on PendingQueue (issue #241, PR #261).

Covers:
1. Routine proposal with clean receipt (under_review=True, contested=False).
2. Rejection/release clears lock without lingering review state.
3. Claim/restore transition coverage while holding path lock.
4. Positive dispute detection for intent, dispute flag, and scalar/list contradicts.
5. Verification that supersedes does not trigger contest derivation.
6. Orphan lock without corresponding entry returns not under review.
7. Malformed JSON shapes and path traversal hardening.
8. Interleaving rename race recovery via bounded retry across .json and .claimed.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from typing import Any

import pytest

import data_olympus.durable as durable
import data_olympus.pending as pending_mod
from data_olympus.pending import (
    PathLockBusyError,
    PendingQueue,
    RunningContestReceipt,
    _path_lock_filename,
)


@pytest.fixture(autouse=True)
def _scoped_win_atomic_writes(monkeypatch):
    """Scoped Windows compatibility shim for test runners; restores originals on teardown."""
    if sys.platform == "win32":
        def _win_atomic_write_json(path: str, payload: dict[str, Any]) -> None:
            parent = os.path.dirname(path) or "."
            fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".tmp.", dir=parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
            except Exception:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise

        def _win_atomic_remove(path: str) -> None:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

        monkeypatch.setattr(pending_mod, "atomic_write_json", _win_atomic_write_json)
        monkeypatch.setattr(pending_mod, "atomic_remove", _win_atomic_remove)
        monkeypatch.setattr(durable, "atomic_write_json", _win_atomic_write_json)
        monkeypatch.setattr(durable, "atomic_remove", _win_atomic_remove)


def test_routine_edit_preserves_clean_receipt(tmp_path) -> None:
    """Boundary 1: Routine edit with no declared dispute.

    The proposal locks the path and surfaces under_review=True, but contested=False.
    """
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    target = "universal/foundation/STD-U-001.md"

    pid = q.enqueue(
        proposal_type="edit",
        target_path=target,
        postimage="# STD-U-001\nUpdated wording without contradiction\n",
        base_commit="abc1234",
        base_blob_sha=None,
        target_file_hash=None,
        meta={
            "agent_identity": "claude-subagent",
            "source_session": "sess-42",
            "reason": "Fixing typo in section 2",
            "confidence": 0.9,
        },
    )

    with pytest.raises(PathLockBusyError):
        q.enqueue(
            proposal_type="edit",
            target_path=target,
            postimage="# Conflicting edit\n",
            base_commit="abc1234",
            base_blob_sha=None,
            target_file_hash=None,
            meta={"agent_identity": "other-agent"},
        )

    receipt = q.derive_running_contest(target)
    assert isinstance(receipt, RunningContestReceipt)
    assert receipt.under_review is True
    assert receipt.contested is False
    assert receipt.pending_id == pid
    assert receipt.reason == "Fixing typo in section 2"
    assert receipt.agent_identity == "claude-subagent"
    assert receipt.contradicts is None


def test_routine_proposal_rejection_clears_lock(tmp_path) -> None:
    """Boundary 2: Routine proposal rejected/aborted clears lock.

    The lock file is unlinked and running contest status returns to under_review=False,
    contested=False with zero lingering dispute state.
    """
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    target = "universal/foundation/STD-U-001.md"

    pid = q.enqueue(
        proposal_type="edit",
        target_path=target,
        postimage="# Aborted routine proposal\n",
        base_commit="abc1234",
        base_blob_sha=None,
        target_file_hash=None,
        meta={
            "agent_identity": "test-runner",
            "reason": "Routine edit to be rejected",
        },
    )

    initial_receipt = q.derive_running_contest(target)
    assert initial_receipt.under_review is True
    assert initial_receipt.contested is False

    # Reject proposal (releases lock)
    q.reject(pid)

    lock_file = os.path.join(q._locks_dir, _path_lock_filename(target))
    assert not os.path.exists(lock_file)

    cleared_receipt = q.derive_running_contest(target)
    assert cleared_receipt.under_review is False
    assert cleared_receipt.contested is False
    assert cleared_receipt.pending_id is None

    # Path can immediately be re-enqueued
    next_pid = q.enqueue(
        proposal_type="edit",
        target_path=target,
        postimage="# Fresh proposal\n",
        base_commit="abc1234",
        base_blob_sha=None,
        target_file_hash=None,
        meta={"reason": "clean follow-up"},
    )
    assert next_pid != pid
    re_receipt = q.derive_running_contest(target)
    assert re_receipt.under_review is True
    assert re_receipt.contested is False


def test_claim_for_resolve_and_restore_preserves_running_contest(tmp_path) -> None:
    """Boundary 3: Gated resolve transition (.json -> .claimed -> restore/finalize).

    Verifies derive_running_contest correctly bridges the .claimed window while
    the path lock remains held.
    """
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    target = "universal/foundation/STD-U-001.md"

    pid = q.enqueue(
        proposal_type="edit",
        target_path=target,
        postimage="# Gated proposal\n",
        base_commit="abc1234",
        base_blob_sha=None,
        target_file_hash=None,
        meta={"reason": "Testing claim window"},
    )

    # 1. Active live entry
    r1 = q.derive_running_contest(target)
    assert r1.under_review is True
    assert r1.pending_id == pid

    # 2. Claim for resolve (renames <pid>.json to <pid>.claimed while lock is held)
    resolved = q.claim_for_resolve(pid)
    assert resolved.pending_id == pid
    assert not os.path.exists(os.path.join(q.root, f"{pid}.json"))
    assert os.path.exists(os.path.join(q.root, f"{pid}.claimed"))

    # During the .claimed window, derive_running_contest must still report under_review=True
    r2 = q.derive_running_contest(target)
    assert r2.under_review is True
    assert r2.pending_id == pid
    assert r2.reason == "Testing claim window"

    # 3. Gate failure triggers restore_resolve (renames <pid>.claimed back to <pid>.json)
    q.restore_resolve(pid)
    assert os.path.exists(os.path.join(q.root, f"{pid}.json"))
    r3 = q.derive_running_contest(target)
    assert r3.under_review is True
    assert r3.pending_id == pid

    # 4. Final claim + finalize_resolve consumes entry and frees lock
    q.claim_for_resolve(pid)
    q.finalize_resolve(pid, target)

    r4 = q.derive_running_contest(target)
    assert r4.under_review is False
    assert r4.contested is False
    assert r4.pending_id is None


def test_positive_contest_detection_and_contradicts_contract(tmp_path) -> None:
    """Item 4: Positive dispute detection under the metadata contract.

    Tests:
    - meta['intent'] == 'contest' -> contested=True
    - meta['dispute'] is True -> contested=True
    - meta['contradicts'] as scalar -> normalized to list[str], contested=True
    - meta['contradicts'] as list -> preserved as list[str], contested=True
    """
    # 1. Intent == 'contest'
    q1 = PendingQueue(pending_root=str(tmp_path / "p1"))
    t1 = "universal/foundation/STD-U-010.md"
    pid1 = q1.enqueue(
        proposal_type="edit",
        target_path=t1,
        postimage="# Contested rewrite\n",
        base_commit="abc1234",
        base_blob_sha=None,
        target_file_hash=None,
        meta={"intent": "contest", "reason": "Contesting decision rule"},
    )
    r1 = q1.derive_running_contest(t1)
    assert r1.under_review is True
    assert r1.contested is True
    assert r1.pending_id == pid1
    assert r1.reason == "Contesting decision rule"

    # 2. Dispute flag == True
    q2 = PendingQueue(pending_root=str(tmp_path / "p2"))
    t2 = "universal/foundation/STD-U-020.md"
    pid2 = q2.enqueue(
        proposal_type="edit",
        target_path=t2,
        postimage="# Dispute flagged\n",
        base_commit="abc1234",
        base_blob_sha=None,
        target_file_hash=None,
        meta={"dispute": True, "reason": "Flagged as dispute"},
    )
    r2 = q2.derive_running_contest(t2)
    assert r2.under_review is True
    assert r2.contested is True
    assert r2.pending_id == pid2

    # 3. Contradicts scalar string normalized to list[str]
    q3 = PendingQueue(pending_root=str(tmp_path / "p3"))
    t3 = "universal/foundation/STD-U-030.md"
    pid3 = q3.enqueue(
        proposal_type="edit",
        target_path=t3,
        postimage="# Scalar contradiction\n",
        base_commit="abc1234",
        base_blob_sha=None,
        target_file_hash=None,
        meta={"contradicts": "STD-U-001", "reason": "Refuting STD-U-001"},
    )
    r3 = q3.derive_running_contest(t3)
    assert r3.under_review is True
    assert r3.contested is True
    assert r3.contradicts == ["STD-U-001"]

    # 4. Contradicts list of strings
    q4 = PendingQueue(pending_root=str(tmp_path / "p4"))
    t4 = "universal/foundation/STD-U-040.md"
    pid4 = q4.enqueue(
        proposal_type="edit",
        target_path=t4,
        postimage="# Multi contradiction\n",
        base_commit="abc1234",
        base_blob_sha=None,
        target_file_hash=None,
        meta={"contradicts": ["STD-U-001", "STD-U-002"], "reason": "Refuting multiple"},
    )
    r4 = q4.derive_running_contest(t4)
    assert r4.under_review is True
    assert r4.contested is True
    assert r4.contradicts == ["STD-U-001", "STD-U-002"]


def test_supersedes_only_is_not_contested(tmp_path) -> None:
    """Contract: supersedes represents normal document succession and must NOT mark contested=True."""
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    target = "universal/foundation/STD-U-005.md"
    pid = q.enqueue(
        proposal_type="edit",
        target_path=target,
        postimage="# Successor doc\n",
        base_commit="abc1234",
        base_blob_sha=None,
        target_file_hash=None,
        meta={"supersedes": "STD-U-004", "reason": "Routine succession"},
    )
    receipt = q.derive_running_contest(target)
    assert receipt.under_review is True
    assert receipt.contested is False
    assert receipt.pending_id == pid
    assert receipt.contradicts is None


def test_orphan_lock_without_entry(tmp_path) -> None:
    """Review feedback: lock file exists on disk, but entry vanished or was cleaned up."""
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    target = "universal/foundation/STD-U-006.md"
    lock_file = os.path.join(q._locks_dir, _path_lock_filename(target))

    # Fabricate a valid lock pointing to a non-existent pending_id
    orphan_id = "0123456789abcdef0123456789abcdef"
    with open(lock_file, "w", encoding="utf-8") as f:
        json.dump({"pending_id": orphan_id, "holder": "test", "acquired_at": 1000.0, "target_path": target}, f)

    receipt = q.derive_running_contest(target)
    assert receipt.under_review is False
    assert receipt.contested is False
    assert receipt.pending_id is None


def test_malformed_json_shapes_and_traversal_guards(tmp_path) -> None:
    """Must-fix 1 & 2: Guard against path traversal and non-dict JSON shapes."""
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    target = "universal/foundation/STD-U-007.md"
    lock_file = os.path.join(q._locks_dir, _path_lock_filename(target))

    # 1. Lock file contains a JSON list []
    with open(lock_file, "w", encoding="utf-8") as f:
        json.dump(["unexpected", "list"], f)
    r1 = q.derive_running_contest(target)
    assert r1.under_review is False

    # 2. Lock file contains a path traversal pending_id
    with open(lock_file, "w", encoding="utf-8") as f:
        json.dump({"pending_id": "../../etc/passwd", "target_path": target}, f)
    r2 = q.derive_running_contest(target)
    assert r2.under_review is False

    # 3. Lock file contains non-string pending_id
    with open(lock_file, "w", encoding="utf-8") as f:
        json.dump({"pending_id": 12345678, "target_path": target}, f)
    r3 = q.derive_running_contest(target)
    assert r3.under_review is False

    # 4. Entry file contains 'meta': None (should not raise AttributeError)
    valid_id = "abcdef0123456789abcdef0123456789"
    with open(lock_file, "w", encoding="utf-8") as f:
        json.dump({"pending_id": valid_id, "target_path": target}, f)
    entry_file = os.path.join(q.root, f"{valid_id}.json")
    with open(entry_file, "w", encoding="utf-8") as f:
        json.dump({"pending_id": valid_id, "meta": None}, f)

    r4 = q.derive_running_contest(target)
    assert r4.under_review is True
    assert r4.contested is False
    assert r4.reason is None
    assert r4.contradicts is None


def test_claim_rename_race_interleaving(tmp_path) -> None:
    """Must-fix 3: Interleaving between .json and .claimed handled by bounded retry."""
    q = PendingQueue(pending_root=str(tmp_path / "p"))
    target = "universal/foundation/STD-U-008.md"
    pid = q.enqueue(
        proposal_type="edit",
        target_path=target,
        postimage="# Race test\n",
        base_commit="abc1234",
        base_blob_sha=None,
        target_file_hash=None,
        meta={"reason": "Testing rename race"},
    )

    # Simulate race: rename to .claimed right under the path lock
    json_path = os.path.join(q.root, f"{pid}.json")
    claimed_path = os.path.join(q.root, f"{pid}.claimed")
    os.rename(json_path, claimed_path)

    receipt = q.derive_running_contest(target)
    assert receipt.under_review is True
    assert receipt.pending_id == pid
    assert receipt.reason == "Testing rename race"
