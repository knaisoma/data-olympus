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

Ownership: every claim writes a fresh random token into its marker and returns
it, and ``release`` removes the marker only when the marker still carries that
token. Without this, a claimer whose TTL had expired (its marker reclaimed by a
second claimer) would, on its late ``release``, delete the second claimer's
live marker and let a third bootstrap in while the second is still converging.

Legacy markers written before tokens existed have no ``token`` field. No
release ever removes one: the process that wrote it predates this code and
released (if at all) through its own copy, and a token-carrying release cannot
own it. A legacy marker therefore lives until its recorded expiry and is then
reclaimed like any expired marker, which is the same bound a crashed claimer
already has.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import secrets
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

_log = logging.getLogger("data_olympus.onboarding_inflight")

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

    @staticmethod
    def _read_marker(path: str) -> dict[str, object] | None:
        """The marker's JSON object, or None if it is missing or unreadable."""
        try:
            with open(path) as f:
                data = json.load(f)
        except (FileNotFoundError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _is_expired(self, path: str) -> bool:
        """True if the marker at ``path`` is missing or past its recorded expiry."""
        data = self._read_marker(path)
        if data is None:
            # Missing, or an unreadable/half-written marker: treat as free so a
            # corrupt file cannot wedge a workspace.
            return True
        expires_at = data.get("expires_at")
        if not isinstance(expires_at, (int, float)):
            return True
        return time.time() >= expires_at

    def _lock_path(self, workspace: str, component: str | None) -> str:
        return self._path(workspace, component) + ".lock"

    def claim(self, workspace: str, component: str | None) -> str | None:
        """Atomically claim the bootstrap slot for (workspace, component).

        Returns this claim's ownership token if the caller now holds the slot
        (proceed with bootstrap, and pass the token to ``release``), or None if
        another live claim already holds it (reject as in-progress). A claim
        whose recorded expiry has passed is reclaimed transparently.

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
                return None
            now = time.time()
            token = secrets.token_hex(16)
            payload = json.dumps({
                "workspace": workspace,
                "component": component,
                "claimed_at": now,
                "expires_at": now + self._ttl,
                "token": token,
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
            return token

    def release(self, workspace: str, component: str | None, token: str) -> bool:
        """Drop the in-flight marker if, and only if, it is still this claim's.

        Called on the failure paths where a claim was taken but the bootstrap
        never actually committed, so a retry is not blocked for the full TTL.
        ``token`` is the value ``claim`` returned. The read, the comparison and
        the unlink all run under the same per-key lock as ``claim``, so no
        reclaim can slip in between the check and the removal.

        Returns True if the marker was removed. Returns False, without raising,
        when there is nothing of this claim's to remove: the marker is gone
        (double release), was reclaimed by another claimer after this claim's
        TTL expired, or is a legacy marker without a token.
        """
        path = self._path(workspace, component)
        if not os.path.isdir(self._root):
            return False  # never claimed: nothing to release, and no mkdir side effect
        with _exclusive(self._lock_path(workspace, component)):
            data = self._read_marker(path)
            if data is None:
                return False
            current = data.get("token")
            if not isinstance(current, str) or not secrets.compare_digest(current, token):
                _log.warning(
                    "in-flight release skipped: the marker for this slot is not "
                    "owned by the releasing claim (expired and reclaimed, or legacy)",
                )
                return False
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
            return True
