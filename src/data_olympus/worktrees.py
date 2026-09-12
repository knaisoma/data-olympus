"""Per-session worktree lifecycle.

Each session writes into its own worktree under <worktree_root>/<safe_id>/.
A small JSON metadata file alongside tracks creation + last activity for GC.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from data_olympus.durable import atomic_write_json
from data_olympus.safe_id import make_safe_id

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

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
    def __init__(
        self,
        *,
        git: GitOps,
        worktree_root: str,
        serializer: AbstractContextManager[Any] | None = None,
    ) -> None:
        self._git = git
        self._root = worktree_root
        # GC tears a session down in several steps. It holds the shared write
        # serializer across the whole sequence so a claim cannot begin between
        # the eligibility check and the removal (issues #253, #254).
        self._serializer = serializer or contextlib.nullcontext()
        os.makedirs(self._root, exist_ok=True)

    @property
    def git(self) -> GitOps:
        """The GitOps handle for the main repo backing this registry. The write
        path uses it to refresh a session worktree's base onto origin/main (git
        subcommands accept ``-C <worktree>``, so one handle serves any worktree)."""
        return self._git

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
            # Removing the worktree removes the lookup location a claim's
            # commit would be searched from, and deleting the branch below
            # removes the commit itself. Both destroy the only evidence that an
            # interrupted resolve committed, so defer while such a claim is
            # outstanding on this session (issues #253, #254). The next GC pass
            # retries; reconciliation runs on its own schedule meanwhile.
            # The whole teardown runs inside ONE serializer acquisition, with
            # its eligibility re-checked there. Guarding first and tearing down
            # afterwards left a window in which a claim could start between the
            # two, and the branch guard would then defer only AFTER the worktree
            # was already gone, leaving a half-removed session (issues #253,
            # #254).
            try:
                with self._serializer:
                    if self._has_unpushed_commits(wt_path):
                        continue
                    self._git.delete_branch_guard(f"kb-session/{entry}")
                    self._git.worktree_remove_guarded(
                        wt_path, branch=f"kb-session/{entry}", force=True,
                    )
                    self._git.delete_branch(f"kb-session/{entry}")
                    # Metadata removal belongs in the SAME acquisition. Outside
                    # it, a returning writer can recreate the worktree, branch
                    # and metadata at the release boundary, and this delete
                    # then removes the NEW metadata, leaving the next
                    # get_or_create to fail against the existing branch.
                    os.unlink(meta_path)
            except Exception:  # noqa: BLE001 - defer, never fail the GC loop
                continue
            # The branch and the metadata are both handled inside the guarded
            # block above. get_or_create() uses
            # `worktree add -b kb-session/<safe_id>`, which FAILS if the branch
            # already exists, so removing the worktree alone would leave a
            # returning session unable to write.
            removed.append(wt_path)
        return removed

    def _has_unpushed_commits(self, wt_path: str) -> bool:
        """True if the worktree has commits not reachable from origin/main, i.e.
        it is unsafe to GC. Fail closed: if we cannot *prove* every commit is
        pushed, return True and defer.

        The one exception is a repo with no ``origin`` remote at all (a local-only
        / read-only demo): there is nothing to push to, so ``git rev-list ...
        origin/main`` would legitimately fail with an unknown-ref error. In that
        case there is no unpushed state to protect and GC may proceed."""
        import subprocess
        try:
            result = subprocess.run(
                ["git", "-C", wt_path, "rev-list", "HEAD", "--not", "origin/main"],
                check=False, capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            # Can't tell -> defer the GC (fail closed).
            return True
        if result.returncode != 0:
            # rev-list failed. This is either a missing/corrupt origin/main ref
            # or a repo with no origin. If there is genuinely no origin remote,
            # there is nothing to push and GC is safe; otherwise (origin exists
            # but the ref could not be resolved) we cannot prove commits are
            # pushed, so we fail closed and defer.
            try:
                remotes = subprocess.run(
                    ["git", "-C", wt_path, "remote"],
                    check=False, capture_output=True, text=True, timeout=10,
                )
            except (subprocess.TimeoutExpired, FileNotFoundError):
                return True
            # origin exists but rev-list still failed => cannot prove pushed =>
            # defer (True). No origin => nothing to push => safe (False).
            return "origin" in {
                line.strip() for line in remotes.stdout.splitlines() if line.strip()
            }
        return bool(result.stdout.strip())
