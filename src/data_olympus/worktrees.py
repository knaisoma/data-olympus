"""Per-session worktree lifecycle.

Each session writes into its own worktree under <worktree_root>/<safe_id>/.
A small JSON metadata file alongside tracks creation + last activity for GC.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from data_olympus.durable import atomic_write_json
from data_olympus.safe_id import make_safe_id

if TYPE_CHECKING:
    from data_olympus.git_ops import GitOps


@dataclass(frozen=True, slots=True)
class Worktree:
    path: str
    meta_path: str

    def read_meta(self) -> dict[str, Any]:
        with open(self.meta_path) as f:
            data: dict[str, Any] = json.load(f)
            return data

    def touch(self, *, timestamp: float | None = None) -> None:
        meta = self.read_meta()
        meta["last_activity"] = timestamp if timestamp is not None else time.time()
        atomic_write_json(self.meta_path, meta)


class WorktreeRegistry:
    def __init__(self, *, git: GitOps, worktree_root: str) -> None:
        self._git = git
        self._root = worktree_root
        os.makedirs(self._root, exist_ok=True)

    def get_or_create(
        self,
        *,
        source_session: str,
        agent_identity: str,
    ) -> Worktree:
        safe = make_safe_id(source_session)
        wt_path = os.path.join(self._root, safe)
        meta_path = os.path.join(self._root, f"{safe}.meta.json")
        if os.path.isdir(wt_path) and os.path.exists(meta_path):
            wt = Worktree(path=wt_path, meta_path=meta_path)
            wt.touch()
            return wt
        self._git.worktree_add(wt_path, branch=f"kb-session/{safe}")
        atomic_write_json(meta_path, {
            "safe_id": safe,
            "source_session": source_session,
            "agent_identity": agent_identity,
            "created_at": time.time(),
            "last_activity": time.time(),
        })
        return Worktree(path=wt_path, meta_path=meta_path)

    def gc(self, *, idle_sec: int) -> list[str]:
        """Remove worktrees whose last_activity is older than idle_sec AND
        whose commits are all reachable from origin/main. Defer otherwise.

        Returns the list of worktree paths that were actually removed.
        """
        removed: list[str] = []
        now = time.time()
        if not os.path.isdir(self._root):
            return removed
        for entry in os.listdir(self._root):
            if entry.endswith(".meta.json"):
                continue
            wt_path = os.path.join(self._root, entry)
            meta_path = os.path.join(self._root, f"{entry}.meta.json")
            if not os.path.isdir(wt_path) or not os.path.exists(meta_path):
                continue
            with open(meta_path) as f:
                meta = json.load(f)
            if now - float(meta.get("last_activity", 0)) < idle_sec:
                continue
            # All commits reachable from origin/main? If not, defer (push queue
            # will retry; once pushed, next GC pass will clean up).
            if self._has_unpushed_commits(wt_path):
                continue
            self._git.worktree_remove(wt_path, force=True)
            os.unlink(meta_path)
            removed.append(wt_path)
        return removed

    def _has_unpushed_commits(self, wt_path: str) -> bool:
        import subprocess
        try:
            result = subprocess.run(
                ["git", "-C", wt_path, "rev-list", "HEAD", "--not", "origin/main"],
                check=False, capture_output=True, text=True, timeout=10,
            )
            return bool(result.stdout.strip())
        except (subprocess.TimeoutExpired, FileNotFoundError):
            # If we can't tell, defer the GC.
            return True
