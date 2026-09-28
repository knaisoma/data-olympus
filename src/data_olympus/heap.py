"""Return freed heap memory to the operating system (issue #284).

An index rebuild allocates a large, short-lived working set on whichever worker
thread runs it. glibc serves each thread from its own arena and keeps memory
freed there instead of returning it, so every thread that ever ran a rebuild
held on to that rebuild's peak. Nothing leaked at the Python level, yet a 1 GiB
server reached its limit after a handful of rebuilds and stayed there while
idle. ``malloc_trim(0)`` walks every arena and gives its free pages back.

Only glibc provides ``malloc_trim``. Elsewhere (macOS, musl) there is nothing
to call and :func:`release_free_heap` does nothing; those allocators return
memory on their own terms.
"""
from __future__ import annotations

import ctypes
import functools
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger("data_olympus.heap")


@functools.cache
def _malloc_trim() -> Callable[[int], int] | None:
    try:
        libc = ctypes.CDLL(None)
    except OSError:
        return None
    trim = getattr(libc, "malloc_trim", None)
    if trim is None:
        return None
    trim.argtypes = [ctypes.c_size_t]
    trim.restype = ctypes.c_int
    return trim  # type: ignore[no-any-return]


def release_free_heap() -> bool:
    """Ask the allocator to return free memory to the OS.

    Returns True when the request was made, False where the allocator offers no
    way to make it or the call failed. Never raises: this is housekeeping, and a
    failure must not take down the refresh loop or startup.
    """
    trim = _malloc_trim()
    if trim is None:
        return False
    try:
        trim(0)
    except Exception:  # noqa: BLE001  housekeeping must never fail the caller
        log.debug("malloc_trim failed", exc_info=True)
        return False
    return True
