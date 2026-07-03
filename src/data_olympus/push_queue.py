"""Durable push queue: enqueue commits awaiting `git push`, drain with retry.

Every queue entry write goes through atomic_write_json
(temp -> fsync -> rename -> parent-dir fsync), so a queue entry that was written
survives a crash. The DURABLE object for a completed write is the git commit on
the session branch, not necessarily the queue entry: a rare post-commit enqueue
failure is reported to the caller as ``push_state="enqueue_failed_recovery_pending"``
(see tools_write._enqueue_after_commit) and the orphaned committed sha is recovered
by ``init_recovery`` at startup (and an in-process re-enqueue attempt), so it
publishes eventually. A ``push_state="queued"`` response means the queue entry did
land durably.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any

from data_olympus.durable import atomic_remove, atomic_write_json

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger("data_olympus.push_queue")


class PushQueue:
    def __init__(self, *, queue_root: str) -> None:
        self._root = queue_root
        os.makedirs(self._root, exist_ok=True)
        # Shas already logged as "skipped because frozen" this process, so a
        # frozen entry is announced once per process (including once after a
        # restart re-encounters it) rather than every drain interval.
        self._frozen_skip_logged: set[str] = set()

    def enqueue(self, *, sha: str, worktree_path: str, meta: dict[str, Any]) -> None:
        entry = {
            "sha": sha,
            "worktree_path": worktree_path,
            "meta": meta,
            "enqueued_at": time.time(),
            "attempts": 0,
            "last_error": "",
        }
        atomic_write_json(os.path.join(self._root, f"{sha}.json"), entry)

    def size(self) -> int:
        if not os.path.isdir(self._root):
            return 0
        return sum(1 for f in os.listdir(self._root) if f.endswith(".json"))

    def frozen_count(self) -> int:
        """Number of queue entries that hit max_attempts and were frozen for
        operator inspection. Health surfaces this so a stuck write path is
        visible; the retry loop skips these entries (see drain())."""
        if not os.path.isdir(self._root):
            return 0
        count = 0
        for name in os.listdir(self._root):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(self._root, name)) as f:
                    entry = json.load(f)
            except (FileNotFoundError, ValueError):
                continue
            if entry.get("frozen"):
                count += 1
        return count

    def drain(
        self,
        *,
        push_fn: Callable[[str], None],
        max_attempts: int,
        on_rebase_conflict: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Iterate every queued entry. push_fn(worktree_path) is called; on
        success the entry is removed; on failure the entry is updated with
        attempt count + last error and left in the queue.

        Frozen entries (those that already hit ``max_attempts``) are skipped:
        retrying them every interval forever accomplishes nothing and only spams
        the remote. A frozen entry stays on disk for operator inspection and is
        surfaced via ``frozen_count()`` in health; an operator clears it by
        deleting or requeuing the entry file (see docs/serving.md).

        ``on_rebase_conflict`` (scope item 2): when ``push_fn`` raises
        :class:`~data_olympus.git_ops.RebaseConflictError`, the commit cannot be
        auto-published (the session branch does not rebase cleanly onto the moved
        origin/main). Instead of retrying forever, the callback demotes the commit
        to a pending entry for operator resolution and the queue entry is removed.
        The callback is responsible for the audit event and the pending record; if
        it raises, the entry is treated as a retryable failure (kept in the queue)
        so a demotion bug cannot silently drop the write.
        """
        from data_olympus.git_ops import NonFastForwardError, RebaseConflictError

        # A repeated non-FF race (origin/main moves faster than we can rebase) is
        # contention, not a content conflict, but it can neither publish nor demote
        # on its own. Bound the in-line retries: after this many consecutive
        # attempts that all end non-FF, demote to pending (like a rebase conflict)
        # so pure contention never becomes a silently-frozen queue item.
        non_ff_demote_after = 5

        if not os.path.isdir(self._root):
            return
        for name in sorted(os.listdir(self._root)):
            if not name.endswith(".json"):
                continue
            entry_path = os.path.join(self._root, name)
            try:
                with open(entry_path) as f:
                    entry = json.load(f)
            except FileNotFoundError:
                continue
            if entry.get("frozen"):
                # Already capped; do not retry. Announce the skip once per
                # process per sha so an operator sees WHY a write is stuck even
                # across a restart (the first-freeze WARN below only fires in the
                # process that froze it). Bounded, so no per-interval spam.
                sha = entry.get("sha", name)
                if sha not in self._frozen_skip_logged:
                    self._frozen_skip_logged.add(sha)
                    log.warning(
                        "push queue entry frozen; skipping retry "
                        "(sha=%s worktree=%s last_error=%s); an operator must "
                        "clear it (see docs/serving.md unfreeze path)",
                        sha,
                        entry.get("worktree_path", "?"),
                        entry.get("last_error", "?"),
                    )
                continue
            try:
                push_fn(entry["worktree_path"])
            except RebaseConflictError as conflict:
                # Not auto-resolvable: demote to pending for operator resolution
                # rather than retrying forever (scope item 2).
                if self._demote(entry, entry_path, on_rebase_conflict,
                                reason=f"rebase_conflict: {conflict.detail}"):
                    continue
                # No callback / demotion failed -> keep queued (retryable).
                self._record_retry(entry, entry_path,
                                   f"rebase_conflict: {conflict.detail}")
                continue
            except NonFastForwardError as nff:
                # Pure contention that the rebase-and-retry still lost. Bounded
                # in-line retries, then demote (Codex Concern 1): a persistent
                # non-FF race must never freeze silently.
                nff_attempts = entry.get("non_ff_attempts", 0) + 1
                entry["non_ff_attempts"] = nff_attempts
                if nff_attempts >= non_ff_demote_after and self._demote(
                    entry, entry_path, on_rebase_conflict,
                    reason=f"non_fast_forward_contention: {nff.detail}",
                ):
                    continue
                self._record_retry(entry, entry_path,
                                   f"non_fast_forward: {nff.detail}",
                                   max_attempts=max_attempts, reset_non_ff=False)
                continue
            except Exception as exc:  # noqa: BLE001 -- intentional: capture any push failure
                self._record_retry(entry, entry_path, str(exc),
                                   max_attempts=max_attempts)
                continue
            atomic_remove(entry_path)

    def _demote(
        self,
        entry: dict[str, Any],
        entry_path: str,
        on_rebase_conflict: Callable[[dict[str, Any]], None] | None,
        *,
        reason: str,
    ) -> bool:
        """Demote a queue entry to pending via the callback and remove it.

        Returns True when the demotion succeeded (entry removed), False when there
        is no callback or the callback failed (caller keeps the entry queued so the
        commit is never silently lost)."""
        if on_rebase_conflict is None:
            return False
        try:
            on_rebase_conflict(entry)
        except Exception:  # noqa: BLE001 - demotion failed; keep queued
            log.exception(
                "push-queue demotion failed (sha=%s reason=%s); keeping queued",
                entry.get("sha", "?"), reason,
            )
            return False
        log.warning(
            "push queue entry demoted to pending (%s; sha=%s worktree=%s); "
            "operator must resolve",
            reason, entry.get("sha", "?"), entry.get("worktree_path", "?"),
        )
        atomic_remove(entry_path)
        return True

    def _record_retry(
        self,
        entry: dict[str, Any],
        entry_path: str,
        error: str,
        *,
        max_attempts: int | None = None,
        reset_non_ff: bool = True,
    ) -> None:
        """Record a retryable push failure: increment attempts, store the error,
        and (when ``max_attempts`` is given and reached) freeze the entry.

        ``reset_non_ff`` (default True) zeroes the ``non_ff_attempts`` counter so
        it counts CONSECUTIVE non-fast-forward failures only (Codex round-2
        Concern 2): a network/other failure between two non-FF failures resets the
        contention streak, so a mixed failure history does not accumulate toward
        the non-FF demotion threshold. The non-FF branch passes False so its own
        increment survives."""
        entry["attempts"] = entry.get("attempts", 0) + 1
        entry["last_error"] = error
        entry["last_error_at"] = time.time()
        if reset_non_ff and entry.get("non_ff_attempts"):
            entry["non_ff_attempts"] = 0
        if max_attempts is not None and entry["attempts"] >= max_attempts:
            entry["frozen"] = True
            log.warning(
                "push queue entry frozen after %d attempts "
                "(sha=%s worktree=%s last_error=%s); it will no longer be retried "
                "until an operator clears it (see docs/serving.md unfreeze path)",
                entry["attempts"], entry.get("sha", "?"),
                entry.get("worktree_path", "?"), entry["last_error"],
            )
        atomic_write_json(entry_path, entry)

    def init_recovery(
        self,
        *,
        worktree_root: str,
        list_unpushed_shas: Callable[[str], list[str]],
    ) -> None:
        """At startup, scan every worktree under worktree_root.
        For each commit reachable from HEAD but NOT from origin/main, ensure
        a queue entry exists."""
        if not os.path.isdir(worktree_root):
            return
        for entry in os.listdir(worktree_root):
            wt_path = os.path.join(worktree_root, entry)
            if not os.path.isdir(wt_path) or entry.endswith(".meta.json"):
                continue
            for sha in list_unpushed_shas(wt_path):
                qe = os.path.join(self._root, f"{sha}.json")
                if os.path.exists(qe):
                    continue
                atomic_write_json(qe, {
                    "sha": sha,
                    "worktree_path": wt_path,
                    "meta": {},
                    "enqueued_at": time.time(),
                    "attempts": 0,
                    "last_error": "",
                    "recovered": True,
                })
