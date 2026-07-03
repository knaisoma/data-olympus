"""Durable push queue: enqueue commits awaiting `git push`, drain with retry.

Every queue entry write goes through atomic_write_json
(temp -> fsync -> rename -> parent-dir fsync) so a commit returning "committed"
guarantees the queue entry survives a crash.
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

    def drain(self, *, push_fn: Callable[[str], None], max_attempts: int) -> None:
        """Iterate every queued entry. push_fn(worktree_path) is called; on
        success the entry is removed; on failure the entry is updated with
        attempt count + last error and left in the queue.

        Frozen entries (those that already hit ``max_attempts``) are skipped:
        retrying them every interval forever accomplishes nothing and only spams
        the remote. A frozen entry stays on disk for operator inspection and is
        surfaced via ``frozen_count()`` in health; an operator clears it by
        deleting or requeuing the entry file (see docs/serving.md).
        """
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
            except Exception as exc:  # noqa: BLE001 -- intentional: capture any push failure
                entry["attempts"] = entry.get("attempts", 0) + 1
                entry["last_error"] = str(exc)
                entry["last_error_at"] = time.time()
                if entry["attempts"] >= max_attempts:
                    # Cap reached; freeze for operator inspection and stop
                    # retrying. Log once, at the moment it first freezes.
                    entry["frozen"] = True
                    log.warning(
                        "push queue entry frozen after %d attempts "
                        "(sha=%s worktree=%s last_error=%s); it will no longer "
                        "be retried until an operator clears it "
                        "(see docs/serving.md unfreeze path)",
                        entry["attempts"],
                        entry.get("sha", "?"),
                        entry.get("worktree_path", "?"),
                        entry["last_error"],
                    )
                atomic_write_json(entry_path, entry)
                continue
            atomic_remove(entry_path)

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
