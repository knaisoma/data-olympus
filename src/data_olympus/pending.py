"""Pending queue: full postimage + CAS metadata + same-path lock.

Low-confidence proposals enter the pending queue rather
than committing immediately. The operator approves or rejects (or edits the
text and approves). On approve, the resolved record carries enough metadata
for the caller to commit it cleanly through the audit-trailer pipeline.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from data_olympus.durable import atomic_remove, atomic_write_json


class PathLockBusyError(Exception):
    """Raised when another pending entry already locks the same target_path."""


class PendingQueueFullError(Exception):
    """Raised when the pending queue is at capacity (KB_PENDING_QUEUE_CAP)."""


@dataclass(frozen=True, slots=True)
class ResolvedPending:
    pending_id: str
    proposal_type: Literal["memory", "edit"]
    target_path: str
    postimage: str
    base_commit: str
    base_blob_sha: str | None
    target_file_hash: str | None
    meta: dict[str, Any]


def _path_lock_filename(target_path: str) -> str:
    import hashlib
    return hashlib.sha256(target_path.encode("utf-8")).hexdigest() + ".lock"


class PendingQueue:
    def __init__(self, *, pending_root: str, cap: int = 0) -> None:
        self._root = pending_root
        self._cap = cap  # 0 = unlimited
        self._locks_dir = os.path.join(self._root, "locks")
        os.makedirs(self._root, exist_ok=True)
        os.makedirs(self._locks_dir, exist_ok=True)

    def _acquire_lock(self, target_path: str, pending_id: str) -> None:
        lock_path = os.path.join(self._locks_dir, _path_lock_filename(target_path))
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise PathLockBusyError(target_path) from None
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"pending_id": pending_id, "target_path": target_path,
                           "acquired_at": time.time()}, f)
        except Exception:
            os.unlink(lock_path)
            raise

    def _release_lock(self, target_path: str) -> None:
        lock_path = os.path.join(self._locks_dir, _path_lock_filename(target_path))
        with contextlib.suppress(FileNotFoundError):
            os.unlink(lock_path)

    def enqueue(
        self,
        *,
        proposal_type: Literal["memory", "edit"],
        target_path: str,
        postimage: str,
        base_commit: str,
        base_blob_sha: str | None,
        target_file_hash: str | None,
        meta: dict[str, Any],
    ) -> str:
        if self._cap > 0 and self.size() >= self._cap:
            raise PendingQueueFullError(
                f"pending queue at capacity ({self._cap})"
            )
        pending_id = uuid.uuid4().hex
        self._acquire_lock(target_path, pending_id)
        try:
            entry = {
                "pending_id": pending_id,
                "proposal_type": proposal_type,
                "target_path": target_path,
                "postimage": postimage,
                "base_commit": base_commit,
                "base_blob_sha": base_blob_sha,
                "target_file_hash": target_file_hash,
                "meta": meta,
                "enqueued_at": time.time(),
            }
            atomic_write_json(os.path.join(self._root, f"{pending_id}.json"), entry)
        except Exception:
            self._release_lock(target_path)
            raise
        return pending_id

    def list(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for name in sorted(os.listdir(self._root)):
            if not name.endswith(".json"):
                continue
            with open(os.path.join(self._root, name)) as f:
                entry = json.load(f)
            out.append({
                "pending_id": entry["pending_id"],
                "proposal_type": entry["proposal_type"],
                "target_path": entry["target_path"],
                "confidence": entry["meta"].get("confidence"),
                "agent_identity": entry["meta"].get("agent_identity"),
                "created_at": entry["enqueued_at"],
            })
        return out

    def get(self, pending_id: str) -> dict[str, Any]:
        with open(os.path.join(self._root, f"{pending_id}.json")) as f:
            data: dict[str, Any] = json.load(f)
            return data

    def approve(self, pending_id: str, *, edited_text: str | None = None) -> ResolvedPending:
        entry = self.get(pending_id)
        postimage = edited_text if edited_text is not None else entry["postimage"]
        resolved = ResolvedPending(
            pending_id=pending_id,
            proposal_type=entry["proposal_type"],
            target_path=entry["target_path"],
            postimage=postimage,
            base_commit=entry["base_commit"],
            base_blob_sha=entry["base_blob_sha"],
            target_file_hash=entry["target_file_hash"],
            meta=entry["meta"],
        )
        # Release the lock + remove the entry. The CALLER applies the postimage
        # in the worktree and produces the commit.
        self._release_lock(entry["target_path"])
        atomic_remove(os.path.join(self._root, f"{pending_id}.json"))
        return resolved

    def reject(self, pending_id: str) -> None:
        entry = self.get(pending_id)
        self._release_lock(entry["target_path"])
        atomic_remove(os.path.join(self._root, f"{pending_id}.json"))

    def size(self) -> int:
        return sum(
            1 for f in os.listdir(self._root)
            if f.endswith(".json") and not f.startswith("locks")
        )

    def locks_held(self) -> int:
        if not os.path.isdir(self._locks_dir):
            return 0
        return sum(1 for f in os.listdir(self._locks_dir) if f.endswith(".lock"))
