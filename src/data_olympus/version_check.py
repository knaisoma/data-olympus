"""Periodic 'a newer version is published' check (issue #146 / KNA-68).

``setup_wizard.latest_version()`` is offline-tolerant but SYNCHRONOUS and
network-bound (blocking urllib). It MUST NOT be called on the async request
path: a slow or unreachable PyPI/GitHub would block the Starlette event loop for
the whole timeout and stall the readiness probe. Instead this module runs it on
a worker thread (``asyncio.to_thread``) once per interval and caches the result
on ``ServerState``; the /api/v1/health route only reads the cache.

Config-gated by ``KB_DISABLE_VERSION_CHECK`` (handled by the caller in
server.main): when disabled the loop is never spawned, so an air-gapped
deployment makes ZERO outbound calls. Read of the public latest version only; no
telemetry is sent.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from data_olympus.server import ServerState

log = logging.getLogger("data_olympus.version_check")


def _compute_once() -> tuple[str | None, str, bool]:
    """Run the blocking lookup and derive (latest, installed, update_available).
    Pure of asyncio so it can run on a worker thread and be unit-tested directly.

    References ``setup_wizard`` via the module object (not ``from ... import``)
    so tests can monkeypatch ``setup_wizard.latest_version`` and see it here. The
    installed version is read off ``VersionInfo.installed`` (set to
    ``setup_wizard.INSTALLED_VERSION`` by ``latest_version()``), so this module
    never touches the re-exported name directly."""
    from data_olympus import setup_wizard

    info = setup_wizard.latest_version()
    if info.latest is None:
        return None, info.installed, False
    try:
        newer = setup_wizard._version_tuple(info.latest) > setup_wizard._version_tuple(
            info.installed
        )
    except ValueError:
        newer = info.latest != info.installed
    return info.latest, info.installed, newer


async def version_check_loop(
    state: ServerState,
    *,
    interval_sec: int,
    run_once_immediately: bool = True,
) -> None:
    """Background asyncio task: refresh the cached latest-version fields.

    Spawned from the server lifespan ONLY when the check is enabled. Runs the
    blocking lookup on a worker thread so the event loop stays free. Logs a
    ONE-TIME line the first time a newer version is detected. Cancellable on
    shutdown; a lookup failure degrades quietly (the cache is left as-is) and the
    loop keeps going."""
    log.info("version_check_loop started (interval=%ss)", interval_sec)
    warned = False
    first = True
    while True:
        if not first:
            await asyncio.sleep(interval_sec)
        first = False
        if not run_once_immediately and state.latest_version is None:
            # Honour the flag: skip the very first immediate check, wait a cycle.
            run_once_immediately = True
            continue
        try:
            latest, installed, update_available = await asyncio.to_thread(_compute_once)
            state.latest_version = latest
            state.update_available = update_available
            if update_available and not warned:
                log.warning(
                    "a newer data-olympus version is available: %s installed, "
                    "%s published (run `uv tool upgrade data-olympus`)",
                    installed, latest,
                )
                warned = True
        except asyncio.CancelledError:
            log.info("version_check_loop cancelled")
            raise
        except Exception as e:  # pragma: no cover - defensive; never crash the loop
            log.warning("version_check_loop iteration failed: %s", e)
