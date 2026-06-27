"""Durable push queue: enqueue commits awaiting `git push`, drain with retry.

Every queue entry write goes through atomic_write_json
(temp -> fsync -> rename -> parent-dir fsync) so a commit returning "committed"
guarantees the queue entry survives a crash.
"""
from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING, Any

from data_olympus.durable import atomic_remove, atomic_write_json

if TYPE_CHECKING:
    from collections.abc import Callable


class PushQueue:
    def __init__(self, *, queue_root: str) -> None:
        self._root = queue_root
        os.makedirs(self._root, exist_ok=True)

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

    def drain(self, *, push_fn: Callable[[str], None], max_attempts: int) -> None:
        """Iterate every queued entry. push_fn(worktree_path) is called; on
        success the entry is removed; on failure the entry is updated with
        attempt count + last error and left in the queue."""
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
            try:
                push_fn(entry["worktree_path"])
            except Exception as exc:  # noqa: BLE001 -- intentional: capture any push failure
                entry["attempts"] = entry.get("attempts", 0) + 1
                entry["last_error"] = str(exc)
                entry["last_error_at"] = time.time()
                if entry["attempts"] >= max_attempts:
                    # Cap reached; leave for operator inspection.
                    entry["frozen"] = True
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
