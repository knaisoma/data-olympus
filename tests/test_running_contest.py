"""Tests for derive_running_contest on PendingQueue (issue #241).

Covers:
1. Routine proposal with clean receipt (under_review=True, contested=False).
2. Rejection/release clears lock without lingering review state.
3. Claim/restore transition coverage while holding path lock.
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

# Cross-platform Windows compatibility shim for test runners
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

    pending_mod.atomic_write_json = _win_atomic_write_json
    pending_mod.atomic_remove = _win_atomic_remove
    durable.atomic_write_json = _win_atomic_write_json
    durable.atomic_remove = _win_atomic_remove


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
