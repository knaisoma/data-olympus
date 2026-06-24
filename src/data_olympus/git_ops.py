"""Subprocess wrappers around the git CLI."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class FfMergeResult:
    """Outcome of an ff-only merge from origin/main."""

    previous_sha: str
    current_sha: str
    changed: bool
    note: str = ""


class GitOps:
    """Wraps git subprocess calls for a single repo path."""

    def __init__(self, repo_path: Path) -> None:
        self._repo = repo_path

    def _run(
        self,
        *args: str,
        check: bool = True,
        timeout_sec: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if not self._repo.exists():
            raise FileNotFoundError(f"git repo path does not exist: {self._repo}")
        return subprocess.run(
            ["git", "-C", str(self._repo), *args],
            check=check,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )

    def head_sha(self) -> str:
        return self._run("rev-parse", "HEAD").stdout.strip()

    def ff_merge_origin_main(self, *, timeout_sec: int = 30) -> FfMergeResult:
        """Fetch origin and fast-forward main. No-ops cleanly if origin is unset or unreachable."""
        previous = self.head_sha()
        # Fetch may fail (no remote, no network); we tolerate that.
        fetch = self._run("fetch", "origin", "main", check=False, timeout_sec=timeout_sec)
        if fetch.returncode != 0:
            return FfMergeResult(
                previous_sha=previous,
                current_sha=previous,
                changed=False,
                note=f"fetch_failed: {fetch.stderr.strip()[:200]}",
            )
        merge = self._run("merge", "--ff-only", "origin/main", check=False, timeout_sec=timeout_sec)
        if merge.returncode != 0:
            return FfMergeResult(
                previous_sha=previous,
                current_sha=previous,
                changed=False,
                note=f"ff_merge_failed: {merge.stderr.strip()[:200]}",
            )
        current = self.head_sha()
        return FfMergeResult(
            previous_sha=previous, current_sha=current, changed=(current != previous)
        )

    def clone(self, remote_url: str, *, timeout_sec: int = 120) -> None:
        """Clone remote_url into self._repo if it does not yet exist."""
        if (self._repo / ".git").exists():
            return
        self._repo.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", remote_url, str(self._repo)],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )

    def worktree_add(self, worktree_path: str, *, branch: str) -> None:
        """git worktree add <worktree_path> -b <branch>. Idempotent: a no-op
        if the worktree already exists at that path."""
        if os.path.isdir(os.path.join(worktree_path, ".git")) or os.path.exists(
            os.path.join(worktree_path, ".git")
        ):
            return  # already exists
        os.makedirs(os.path.dirname(worktree_path), exist_ok=True)
        subprocess.run(
            ["git", "-C", str(self._repo), "worktree", "add",
             worktree_path, "-b", branch],
            check=True, capture_output=True,
        )

    def worktree_remove(self, worktree_path: str, *, force: bool = False) -> None:
        """git worktree remove <worktree_path>. Idempotent: a no-op if absent."""
        if not os.path.exists(worktree_path):
            return
        args = ["git", "-C", str(self._repo), "worktree", "remove", worktree_path]
        if force:
            args.append("--force")
        subprocess.run(args, check=True, capture_output=True)

    def push(self, worktree_path: str) -> None:
        import subprocess
        subprocess.run(
            ["git", "-C", worktree_path, "push", "origin", "HEAD:main"],
            check=True, capture_output=True,
        )


def normalize_remote_url(url: str) -> str:
    """Collapse ssh and https variants of a remote URL to a canonical form.
    git@github.com:org/repo.git == https://github.com/org/repo == ...etc."""
    import re
    s = url.strip().lower()
    s = re.sub(r"^git@([^:]+):", r"https://\1/", s)
    s = re.sub(r"^ssh://git@", "https://", s)
    s = re.sub(r"\.git/?$", "", s)
    return re.sub(r"/$", "", s)


def get_remote_url(repo_path: str) -> str | None:
    """Return the canonical origin remote URL for a git repo, or None if
    the repo has no remote (or isn't a git repo)."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "remote", "get-url", "origin"],
            check=False, capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    return url or None
