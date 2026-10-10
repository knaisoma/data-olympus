"""Server-side in-flight guard for onboarding bootstrap (item 2).

A committed bootstrap is only reflected in the index after the push queue
drains and the next pull rebuilds it. During that convergence window
``kb_onboarding_status`` still reports ``absent`` for the just-bootstrapped
workspace, so a second ``kb_bootstrap_project`` call (a retry, a concurrent
agent, a double-click) passes the ``state == absent`` re-check and double-commits.

This module records a short-lived, workspace+component-keyed marker on disk the
moment a bootstrap is admitted. A second bootstrap for the same key inside the
window is rejected as ``already_in_progress``. The marker is a plain file with an
embedded expiry so a crashed process cannot wedge a workspace forever: a claim
whose recorded expiry is in the past is treated as free and reclaimed.

The store lives on the same durable state volume as the pending queue, so it
survives a normal restart (the convergence window is the same order of
magnitude as a restart) yet self-heals via the TTL.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

try:  # POSIX: advisory lock released by the kernel when the holder dies.
    import fcntl
except ImportError:  # pragma: no cover - Windows only
    fcntl = None  # type: ignore[assignment]
    import msvcrt

_DEFAULT_TTL_SECONDS = 900.0  # convergence window: commit -> push -> pull -> reindex


def _marker_filename(workspace: str, component: str | None) -> str:
    key = f"{workspace}\x00{component or ''}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest() + ".inflight"


@contextlib.contextmanager
def _exclusive(lock_path: str) -> Iterator[None]:
    """Hold an exclusive OS-level lock on ``lock_path`` for the duration.

    The lock file itself is never unlinked: unlinking a lock file while another
    process waits on the old inode would let two holders coexist. Each call
    opens its own file description, so threads of one process contend exactly
    like separate processes do. The kernel drops the lock if the holder dies, so
    a crash cannot wedge the slot (unlike an O_EXCL lock file).
    """
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:  # pragma: no cover - Windows only
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    continue
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock


class BootstrapInFlight:
    """Filesystem-backed set of workspaces with a bootstrap in the convergence
    window. Claims are serialized by a per-key OS lock and self-expiring."""

    def __init__(self, root: str, *, ttl_seconds: float = _DEFAULT_TTL_SECONDS) -> None:
        self._root = root
        self._ttl = ttl_seconds
        # The marker directory is created lazily on first claim, not here, so
        # merely constructing the guard never touches the filesystem. This keeps
        # the guard cheap on the reject-before-side-effect paths and avoids an
        # eager mkdir against a not-yet-provisioned (or, in tests, read-only)
        # state volume.

    def _path(self, workspace: str, component: str | None) -> str:
        return os.path.join(self._root, _marker_filename(workspace, component))

    def _is_expired(self, path: str) -> bool:
        """True if the marker at ``path`` is missing or past its recorded expiry."""
        try:
            with open(path) as f:
                data = json.load(f)
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            # Missing, or an unreadable/half-written marker: treat as free so a
            # corrupt file cannot wedge a workspace.
            return True
        expires_at = data.get("expires_at")
        if not isinstance(expires_at, (int, float)):
            return True
        return time.time() >= expires_at

    def _lock_path(self, workspace: str, component: str | None) -> str:
        return self._path(workspace, component) + ".lock"

    def claim(self, workspace: str, component: str | None) -> bool:
        """Atomically claim the bootstrap slot for (workspace, component).

        Returns True if this caller now holds the slot (proceed with bootstrap),
        False if another live claim already holds it (reject as in-progress).
        A claim whose recorded expiry has passed is reclaimed transparently.

        The whole check-then-write runs under one per-key exclusive lock, so it
        is single-winner for both a fresh slot and the reclaim of a stale one.
        Do not reintroduce a lock-free O_CREAT|O_EXCL fast path beside a locked
        reclaim: the two do not exclude each other, and that combination granted
        two concurrent winners (a fast-path marker created but not yet written
        reads as expired and was reclaimed under its writer).
        """
        os.makedirs(self._root, exist_ok=True)
        path = self._path(workspace, component)
        with _exclusive(self._lock_path(workspace, component)):
            if not self._is_expired(path):
                return False
            now = time.time()
            payload = json.dumps({
                "workspace": workspace,
                "component": component,
                "claimed_at": now,
                "expires_at": now + self._ttl,
            })
            # Write a temp file and rename it over the marker, so the marker is
            # never observed half-written, even by a reader outside the lock.
            fd, tmp = tempfile.mkstemp(dir=self._root, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(payload)
                os.replace(tmp, path)
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp)
                raise
            return True

    def release(self, workspace: str, component: str | None) -> None:
        """Drop the in-flight marker (best effort). Called on the failure paths
        where a claim was taken but the bootstrap never actually committed, so a
        retry is not blocked for the full TTL."""
        path = self._path(workspace, component)
        if not os.path.isdir(self._root):
            return  # never claimed: nothing to release, and no mkdir side effect
        with _exclusive(self._lock_path(workspace, component)), \
                contextlib.suppress(FileNotFoundError):
            os.unlink(path)
