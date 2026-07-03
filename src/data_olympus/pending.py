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
import re
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Iterator

from data_olympus.durable import atomic_remove, atomic_write_json

# The exact shape of ``uuid.uuid4().hex`` (32 lowercase hex chars). Used to reject
# path-traversal in a client-supplied pending_id before it hits the disk join.
_PENDING_ID_RE = re.compile(r"[0-9a-f]{32}")


class PathLockBusyError(Exception):
    """Raised when another pending entry already locks the same target_path."""


class PendingQueueFullError(Exception):
    """Raised when the pending queue is at capacity (KB_PENDING_QUEUE_CAP)."""


class PendingNotFoundError(Exception):
    """Raised when a pending_id does not resolve to an entry on disk.

    Previously ``get`` let the bare ``FileNotFoundError`` from ``open`` propagate,
    which the REST resolve route surfaced as an opaque HTTP 500. This typed error
    lets the route map an unknown/expired id to a 404 (item 9)."""


class PendingAlreadyResolvedError(Exception):
    """Raised when a pending entry is claimed but has already been claimed by a
    concurrent resolve (item 5). ``approve``/``reject`` are get-then-remove, so two
    concurrent resolves of the same id both saw the entry and both committed. The
    atomic ``claim`` renames the entry to a ``.claimed`` sidecar in one
    ``os.rename`` step; the loser of the race sees ``FileNotFoundError`` and this
    error is raised so exactly one resolve proceeds."""


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

    @contextlib.contextmanager
    def path_lock(self, target_path: str, *, owner: str) -> Iterator[None]:
        """Advisory per-path lock SHARED between the auto-commit path and the
        pending queue (scope item 1).

        Both surfaces write into the same on-disk ``locks/`` directory keyed by the
        canonical target path, so an auto-commit cannot land on a path that has a
        pending proposal in flight (the later approval would clobber it) and two
        auto-commits to the same path cannot interleave. ``owner`` is a marker
        (e.g. ``auto-commit:<sha_or_session>``) recorded in the lock file for
        operator diagnosis; it is not a pending_id. Raises :class:`PathLockBusyError`
        when the path is already locked. Releases on exit."""
        self._acquire_lock(target_path, owner)
        try:
            yield
        finally:
            self._release_lock(target_path)

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
        # pending_id reaches here straight from a URL path param; reject anything
        # that is not the hex-uuid shape we mint so a value like
        # ``../../etc/passwd`` can never be interpolated into the on-disk join.
        if not _PENDING_ID_RE.fullmatch(pending_id):
            raise PendingNotFoundError(pending_id)
        path = os.path.join(self._root, f"{pending_id}.json")
        if not os.path.exists(path):
            raise PendingNotFoundError(pending_id)
        with open(path) as f:
            data: dict[str, Any] = json.load(f)
            return data

    def _claim(self, pending_id: str) -> dict[str, Any]:
        """Atomically claim a pending entry so exactly one resolver proceeds
        (item 5). ``approve``/``reject`` were get-then-remove: two concurrent
        resolves of the same id both read the entry and both committed (two
        commits, two audit events, one decision). ``os.rename`` is atomic on a
        POSIX filesystem, so renaming ``<pid>.json`` to ``<pid>.claimed`` is a
        test-and-set: the winner gets the file, the loser hits ``FileNotFoundError``
        and we surface :class:`PendingAlreadyResolvedError`. The claimed sidecar is
        removed by the caller once the entry is fully processed (or on failure it
        lingers as a ``.claimed`` file that ``list``/``size`` ignore and the GC
        cleans up its lock)."""
        if not _PENDING_ID_RE.fullmatch(pending_id):
            raise PendingNotFoundError(pending_id)
        src = os.path.join(self._root, f"{pending_id}.json")
        if not os.path.exists(src):
            raise PendingNotFoundError(pending_id)
        claimed = os.path.join(self._root, f"{pending_id}.claimed")
        try:
            os.rename(src, claimed)
        except FileNotFoundError:
            # Lost the race: another resolver renamed it first between our
            # exists() check and here.
            raise PendingAlreadyResolvedError(pending_id) from None
        with open(claimed) as f:
            data: dict[str, Any] = json.load(f)
            return data

    def _finish_claim(self, pending_id: str, target_path: str) -> None:
        """Release the path lock and remove the ``.claimed`` sidecar. Called after
        a claimed entry has been fully processed (committed or rejected)."""
        self._release_lock(target_path)
        atomic_remove(os.path.join(self._root, f"{pending_id}.claimed"))

    def approve(self, pending_id: str, *, edited_text: str | None = None) -> ResolvedPending:
        entry = self._claim(pending_id)
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
        # Release the lock + remove the (claimed) entry. The CALLER applies the
        # postimage in the worktree and produces the commit; the CAS/validation
        # gate may still reject after this, but the entry is already consumed so a
        # rejected approval does not leave a re-approvable duplicate.
        self._finish_claim(pending_id, entry["target_path"])
        return resolved

    def reject(self, pending_id: str) -> None:
        entry = self._claim(pending_id)
        self._finish_claim(pending_id, entry["target_path"])

    def gc_orphan_locks(self) -> int:
        """Remove lock files whose ``pending_id`` has no live entry (item 5).

        A crash between ``_acquire_lock`` and the entry write in ``enqueue`` leaves
        the path locked forever with no entry to release it, so every future
        proposal to that path is rejected ``rejected_path_lock_busy``. Each lock
        file records the ``pending_id`` that holds it; if neither ``<pid>.json``
        nor ``<pid>.claimed`` exists, the lock is orphaned and is removed. Locks
        held by the shared auto-commit path (``owner`` is not a hex uuid) are
        left alone: those are held only for the duration of an in-process commit
        and are never orphaned across a restart (a restart clears the process,
        and a stale one is released by the ``finally`` of ``path_lock``; but if a
        hard crash left one, its non-uuid owner means we cannot prove it orphaned,
        so we conservatively skip it). Returns the number of locks removed."""
        if not os.path.isdir(self._locks_dir):
            return 0
        removed = 0
        for name in os.listdir(self._locks_dir):
            if not name.endswith(".lock"):
                continue
            lock_path = os.path.join(self._locks_dir, name)
            try:
                with open(lock_path) as f:
                    info = json.load(f)
            except (FileNotFoundError, ValueError):
                continue
            holder = info.get("pending_id", "")
            # Only reclaim locks held by a pending entry (uuid holder). A
            # non-uuid holder is the transient auto-commit owner; skip it.
            if not _PENDING_ID_RE.fullmatch(str(holder)):
                continue
            live = os.path.join(self._root, f"{holder}.json")
            claimed = os.path.join(self._root, f"{holder}.claimed")
            if os.path.exists(live) or os.path.exists(claimed):
                continue
            with contextlib.suppress(FileNotFoundError):
                os.unlink(lock_path)
                removed += 1
        return removed

    def would_exceed(self, n: int) -> bool:
        """True if enqueuing ``n`` more entries would exceed the cap (0 = unlimited).

        Lets a multi-file caller (onboarding bootstrap) check capacity up front so
        it can reject atomically instead of enqueuing a partial bundle."""
        return self._cap > 0 and self.size() + n > self._cap

    def size(self) -> int:
        return sum(
            1 for f in os.listdir(self._root)
            if f.endswith(".json") and not f.startswith("locks")
        )

    def locks_held(self) -> int:
        if not os.path.isdir(self._locks_dir):
            return 0
        return sum(1 for f in os.listdir(self._locks_dir) if f.endswith(".lock"))
