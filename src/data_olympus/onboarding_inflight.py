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
import time

_DEFAULT_TTL_SECONDS = 900.0  # convergence window: commit -> push -> pull -> reindex


def _marker_filename(workspace: str, component: str | None) -> str:
    key = f"{workspace}\x00{component or ''}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest() + ".inflight"


class BootstrapInFlight:
    """Filesystem-backed set of workspaces with a bootstrap in the convergence
    window. Claims are atomic (O_CREAT|O_EXCL) and self-expiring."""

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

    def claim(self, workspace: str, component: str | None) -> bool:
        """Atomically claim the bootstrap slot for (workspace, component).

        Returns True if this caller now holds the slot (proceed with bootstrap),
        False if another live claim already holds it (reject as in-progress).
        A claim whose recorded expiry has passed is reclaimed transparently.
        """
        os.makedirs(self._root, exist_ok=True)
        path = self._path(workspace, component)
        now = time.time()
        payload = json.dumps({
            "workspace": workspace,
            "component": component,
            "claimed_at": now,
            "expires_at": now + self._ttl,
        })
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            # A marker exists. If it has expired, reclaim it; a still-live claim
            # means a bootstrap for this key is genuinely in flight.
            if not self._is_expired(path):
                return False
            # Reclaim: overwrite the stale marker in place. os.O_TRUNC is safe
            # here because we only reach this branch for an expired claim.
            try:
                fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            except OSError:
                return False
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        return True

    def release(self, workspace: str, component: str | None) -> None:
        """Drop the in-flight marker (best effort). Called on the failure paths
        where a claim was taken but the bootstrap never actually committed, so a
        retry is not blocked for the full TTL."""
        path = self._path(workspace, component)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
