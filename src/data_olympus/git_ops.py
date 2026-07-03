"""Subprocess wrappers around the git CLI."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


class RebaseConflictError(Exception):
    """A rebase of the session branch onto origin/main hit a conflict that cannot
    be auto-resolved (scope item 2). The caller demotes the commit to a pending
    entry for operator resolution rather than retrying forever."""

    def __init__(self, *, worktree_path: str, detail: str) -> None:
        self.worktree_path = worktree_path
        self.detail = detail
        super().__init__(f"rebase conflict in {worktree_path}: {detail}")


class NonFastForwardError(Exception):
    """A push was rejected non-fast-forward and the rebase-and-retry still lost
    the race (origin/main moved again). Retryable on the next drain; distinct
    from a network failure so the push loop can log it clearly."""

    def __init__(self, *, worktree_path: str, detail: str) -> None:
        self.worktree_path = worktree_path
        self.detail = detail
        super().__init__(f"non-fast-forward push in {worktree_path}: {detail}")


@dataclass(frozen=True, slots=True)
class FfMergeResult:
    """Outcome of an ff-only merge from origin/main.

    ``status`` distinguishes the failure modes the refresh loop must surface so a
    broken remote does not masquerade as "fresh, no change":

    - ``changed``      fetch + fast-forward advanced the local checkout.
    - ``no_change``    fetch ok, already up to date.
    - ``no_remote``    no ``origin`` configured (read-only deployment; healthy).
    - ``fetch_failed`` fetch errored (network/auth/unreachable remote).
    - ``ff_failed``    fetch ok but fast-forward refused (diverged history).
    """

    previous_sha: str
    current_sha: str
    changed: bool
    note: str = ""
    status: str = "no_change"
    remote_sha: str | None = None


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

    def _has_origin(self) -> bool:
        result = self._run("remote", check=False)
        remotes = {line.strip() for line in result.stdout.splitlines() if line.strip()}
        return "origin" in remotes

    def ff_merge_origin_main(self, *, timeout_sec: int = 30) -> FfMergeResult:
        """Fetch origin and fast-forward main, returning a status-classified result.

        A read-only deployment with no ``origin`` returns ``no_remote`` (healthy,
        not a failure). Fetch and fast-forward failures return ``fetch_failed`` /
        ``ff_failed`` so the refresh loop can degrade health instead of reporting
        a fresh no-change.
        """
        previous = self.head_sha()
        if not self._has_origin():
            return FfMergeResult(
                previous_sha=previous, current_sha=previous, changed=False,
                status="no_remote",
            )
        # Fetch may fail (no network, auth, unreachable); classify rather than hide.
        fetch = self._run("fetch", "origin", "main", check=False, timeout_sec=timeout_sec)
        if fetch.returncode != 0:
            return FfMergeResult(
                previous_sha=previous,
                current_sha=previous,
                changed=False,
                note=f"fetch_failed: {fetch.stderr.strip()[:200]}",
                status="fetch_failed",
            )
        remote = self._run("rev-parse", "origin/main", check=False, timeout_sec=timeout_sec)
        remote_sha = remote.stdout.strip() if remote.returncode == 0 else None
        merge = self._run("merge", "--ff-only", "origin/main", check=False, timeout_sec=timeout_sec)
        if merge.returncode != 0:
            return FfMergeResult(
                previous_sha=previous,
                current_sha=previous,
                changed=False,
                note=f"ff_merge_failed: {merge.stderr.strip()[:200]}",
                status="ff_failed",
                remote_sha=remote_sha,
            )
        current = self.head_sha()
        changed = current != previous
        return FfMergeResult(
            previous_sha=previous, current_sha=current, changed=changed,
            status="changed" if changed else "no_change",
            remote_sha=remote_sha,
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

    def _branch_exists(self, branch: str) -> bool:
        result = subprocess.run(
            ["git", "-C", str(self._repo), "rev-parse", "--verify", "--quiet",
             f"refs/heads/{branch}"],
            check=False, capture_output=True,
        )
        return result.returncode == 0

    def worktree_add(self, worktree_path: str, *, branch: str) -> None:
        """git worktree add <worktree_path> for <branch>. Idempotent: a no-op
        if the worktree already exists at that path.

        Normally the branch is created fresh (``-b``). If the branch already
        exists (a residual ``kb-session/<safe_id>`` left behind by a GC that
        removed the worktree but crashed before deleting the branch), attach to
        the existing branch instead of failing with "branch already exists".
        This is belt-and-suspenders on top of GC's branch deletion so a
        returning session can always get a worktree."""
        if os.path.isdir(os.path.join(worktree_path, ".git")) or os.path.exists(
            os.path.join(worktree_path, ".git")
        ):
            return  # already exists
        os.makedirs(os.path.dirname(worktree_path), exist_ok=True)
        if self._branch_exists(branch):
            args = ["git", "-C", str(self._repo), "worktree", "add",
                    worktree_path, branch]
        else:
            args = ["git", "-C", str(self._repo), "worktree", "add",
                    worktree_path, "-b", branch]
        subprocess.run(args, check=True, capture_output=True)

    def worktree_remove(self, worktree_path: str, *, force: bool = False) -> None:
        """git worktree remove <worktree_path>. Idempotent: a no-op if absent."""
        if not os.path.exists(worktree_path):
            return
        args = ["git", "-C", str(self._repo), "worktree", "remove", worktree_path]
        if force:
            args.append("--force")
        subprocess.run(args, check=True, capture_output=True)

    def delete_branch(self, branch: str) -> None:
        """git branch -D <branch> in the main repo. Idempotent: a no-op if the
        branch does not exist. Used after worktree removal so a GC'd session's
        ``kb-session/<safe_id>`` branch does not linger and collide with the
        ``worktree add -b`` a returning session performs."""
        subprocess.run(
            ["git", "-C", str(self._repo), "branch", "-D", branch],
            check=False, capture_output=True,
        )

    def files_changed_in_commit(
        self, sha: str, *, worktree_path: str, timeout_sec: int = 10,
    ) -> list[str]:
        """Paths added/modified in ``sha`` (relative to the repo root).

        Used by push-conflict demotion to rebuild pending proposals from a commit
        that could not be rebased. Excludes deletions (``--diff-filter=d`` keeps
        Added/Copied/Modified/Renamed/Type-changed) because a deletion has no
        postimage to re-propose. Returns [] on any error."""
        try:
            result = subprocess.run(
                ["git", "-C", worktree_path, "diff-tree", "--no-commit-id",
                 "-r", "--diff-filter=d", "--name-only", sha],
                check=False, capture_output=True, text=True, timeout=timeout_sec,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def file_at_commit(
        self, sha: str, path: str, *, worktree_path: str, timeout_sec: int = 10,
    ) -> str | None:
        """Return the text content of ``path`` as of commit ``sha`` (``git show
        <sha>:<path>``), or None if the file did not exist there or on error."""
        try:
            result = subprocess.run(
                ["git", "-C", worktree_path, "show", f"{sha}:{path}"],
                check=False, capture_output=True, text=True, timeout=timeout_sec,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout

    def list_unpushed_shas(self, worktree_path: str, *, timeout_sec: int = 10) -> list[str]:
        """SHAs reachable from the worktree's HEAD but not from origin/main.

        Used by push-queue init-recovery to find commits that were made but
        never enqueued (a crash between ``git commit`` and ``push_queue.enqueue``
        orphans them). Returns [] on any error so recovery degrades to a no-op
        rather than crashing startup."""
        try:
            result = subprocess.run(
                ["git", "-C", worktree_path, "rev-list", "HEAD", "--not", "origin/main"],
                check=False, capture_output=True, text=True, timeout=timeout_sec,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def push(self, worktree_path: str, *, timeout_sec: int = 60) -> None:
        """Push the worktree's HEAD to origin/main.

        A bounded ``timeout_sec`` (default 60s) prevents a hung origin from
        blocking the push-retry loop indefinitely; on timeout,
        ``subprocess.TimeoutExpired`` propagates and the caller classifies it as
        a retryable failure (see refresh.push_retry_loop)."""
        subprocess.run(
            ["git", "-C", worktree_path, "push", "origin", "HEAD:main"],
            check=True, capture_output=True, timeout=timeout_sec,
        )

    @staticmethod
    def _is_non_fast_forward(stderr: str) -> bool:
        """Classify a failed ``git push`` stderr as a non-fast-forward rejection
        (as opposed to a network/auth failure).

        A non-FF push is the recoverable case (fetch + rebase + retry); a network
        failure is the retry-later case. Git's wording for a non-FF rejection is
        stable across versions: ``! [rejected] ... (fetch first)`` /
        ``(non-fast-forward)`` and the "Updates were rejected because" hint."""
        s = stderr.lower()
        return (
            "non-fast-forward" in s
            or "fetch first" in s
            or "updates were rejected" in s
            or ("[rejected]" in s and "fetch" in s)
        )

    def refresh_base(self, worktree_path: str, *, timeout_sec: int = 60) -> str:
        """Rebase the worktree's session branch onto the latest ``origin/main`` so
        subsequent reads/commits sit on the refreshed base (scope items 2, 3).

        Session worktrees branch from main at creation and are never refreshed, so
        an auto-commit's CAS check would compare against a STALE base and a push
        would be rejected non-FF. This fetches ``origin/main`` and rebases the
        session branch onto it. Returns the resulting ``origin/main`` sha (or ""
        when there is no origin). Raises :class:`RebaseConflictError` on a conflict
        (the caller demotes to pending). ``subprocess.TimeoutExpired`` propagates
        for a hung remote (retryable). No-op when there is no ``origin`` remote."""
        remotes = subprocess.run(
            ["git", "-C", worktree_path, "remote"],
            check=False, capture_output=True, text=True, timeout=timeout_sec,
        )
        if "origin" not in {
            line.strip() for line in remotes.stdout.splitlines() if line.strip()
        }:
            return ""
        fetch = subprocess.run(
            ["git", "-C", worktree_path, "fetch", "origin", "main"],
            check=False, capture_output=True, text=True, timeout=timeout_sec,
        )
        if fetch.returncode != 0:
            raise RuntimeError(f"fetch_failed: {fetch.stderr.strip()[:200]}")
        remote_sha = subprocess.run(
            ["git", "-C", worktree_path, "rev-parse", "origin/main"],
            check=False, capture_output=True, text=True, timeout=timeout_sec,
        ).stdout.strip()
        rebase = subprocess.run(
            ["git", "-C", worktree_path, "rebase", "origin/main"],
            check=False, capture_output=True, text=True, timeout=timeout_sec,
        )
        if rebase.returncode != 0:
            # Abort the half-applied rebase so the worktree is left clean; then
            # signal the conflict so the caller demotes the commit to pending.
            subprocess.run(
                ["git", "-C", worktree_path, "rebase", "--abort"],
                check=False, capture_output=True, timeout=timeout_sec,
            )
            raise RebaseConflictError(
                worktree_path=worktree_path,
                detail=(rebase.stderr or rebase.stdout).strip()[:400],
            )
        return remote_sha

    def push_with_rebase_recovery(
        self, worktree_path: str, *, timeout_sec: int = 60,
    ) -> None:
        """Push HEAD to origin/main; on a non-fast-forward rejection, fetch +
        rebase the session branch onto origin/main and retry once (scope item 2).

        Distinguishes the two failure modes the plain ``push`` conflated:

        - **Non-fast-forward** (a second overlapping session moved origin/main):
          recoverable. Rebase the session branch onto the new origin/main and push
          again. If the retry still fails non-FF (origin moved again mid-rebase),
          raise :class:`NonFastForwardError` so the caller retries the whole entry
          on the next drain rather than looping in-line.
        - **Rebase conflict**: not auto-resolvable. ``refresh_base`` raises
          :class:`RebaseConflictError`, which propagates so the caller demotes the
          commit to a pending entry for operator resolution.
        - **Anything else** (network, auth, timeout): propagates unchanged so the
          push-retry loop counts it as a retryable failure.
        """
        first = subprocess.run(
            ["git", "-C", worktree_path, "push", "origin", "HEAD:main"],
            check=False, capture_output=True, text=True, timeout=timeout_sec,
        )
        if first.returncode == 0:
            return
        if not self._is_non_fast_forward(first.stderr):
            # Not a non-FF: surface as a generic push failure (retryable).
            raise subprocess.CalledProcessError(
                first.returncode, first.args, first.stdout, first.stderr,
            )
        # Non-FF: rebase onto the moved origin/main (may raise
        # RebaseConflictError -> caller demotes) and retry the push once.
        self.refresh_base(worktree_path, timeout_sec=timeout_sec)
        retry = subprocess.run(
            ["git", "-C", worktree_path, "push", "origin", "HEAD:main"],
            check=False, capture_output=True, text=True, timeout=timeout_sec,
        )
        if retry.returncode == 0:
            return
        if self._is_non_fast_forward(retry.stderr):
            raise NonFastForwardError(
                worktree_path=worktree_path, detail=retry.stderr.strip()[:400],
            )
        raise subprocess.CalledProcessError(
            retry.returncode, retry.args, retry.stdout, retry.stderr,
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
