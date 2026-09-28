"""Return freed heap memory to the operating system (issue #284).

An index rebuild allocates a large, short-lived working set on whichever worker
thread runs it. glibc serves threads from separate arenas and keeps free memory
in them for reuse, so every long-lived thread that ever ran a rebuild held on to
a share of that rebuild's peak. Nothing leaked at the Python level, yet a 1 GiB
server reached its limit after a handful of rebuilds and stayed there while
idle.

Two glibc behaviours combine, and each needs its own remedy:

- Free memory inside an arena stays resident until something releases it.
  :func:`release_free_heap` calls ``malloc_trim(0)``, which walks every arena
  and returns its whole free pages.
- The free space at the top of a worker thread's arena is returned only when it
  exceeds the trim threshold, and glibc raises that threshold on its own after
  large blocks are freed, up to 64 MiB. After a rebuild each worker arena kept
  about 10 MiB of free top that ``malloc_trim`` does not shrink.
  :func:`configure_allocator` pins the threshold. That is process-wide and
  also stops glibc adjusting its mmap threshold, so large blocks keep being
  served by ``mmap`` and returned on free instead of being cached in an arena.
  An operator who sets the trim threshold explicitly
  (``MALLOC_TRIM_THRESHOLD_`` or ``glibc.malloc.trim_threshold`` in
  ``GLIBC_TUNABLES``) keeps that value.

Only glibc provides these calls. Elsewhere (macOS, musl) there is nothing to
call and both functions do nothing.
"""
from __future__ import annotations

import ctypes
import functools
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger("data_olympus.heap")

# mallopt(3) parameter number for M_TRIM_THRESHOLD in glibc's <malloc.h>.
M_TRIM_THRESHOLD = -1
# glibc's own starting value; pinning it is what stops the dynamic increase.
TRIM_THRESHOLD_BYTES = 128 * 1024


@functools.cache
def _libc_function(name: str) -> Callable[..., int] | None:
    try:
        libc = ctypes.CDLL(None)
    except OSError:
        return None
    return getattr(libc, name, None)


def _malloc_trim() -> Callable[[int], int] | None:
    trim = _libc_function("malloc_trim")
    if trim is not None:
        trim.argtypes = [ctypes.c_size_t]  # type: ignore[attr-defined]
        trim.restype = ctypes.c_int  # type: ignore[attr-defined]
    return trim


def _mallopt() -> Callable[[int, int], int] | None:
    opt = _libc_function("mallopt")
    if opt is not None:
        opt.argtypes = [ctypes.c_int, ctypes.c_int]  # type: ignore[attr-defined]
        opt.restype = ctypes.c_int  # type: ignore[attr-defined]
    return opt


def _operator_set_trim_threshold() -> bool:
    if os.environ.get("MALLOC_TRIM_THRESHOLD_"):
        return True
    tunables = os.environ.get("GLIBC_TUNABLES", "")
    return any(t.split("=", 1)[0] == "glibc.malloc.trim_threshold" for t in tunables.split(":"))


def configure_allocator() -> bool:
    """Pin glibc's trim threshold so worker-thread arenas give back their top.

    Call once during single-threaded startup (``mallopt`` is not safe against
    concurrent allocation). Returns True when the allocator accepted the
    setting, False where the operator set the threshold themselves, there is no
    ``mallopt``, the allocator rejected it, or the call failed. Never raises.
    """
    if _operator_set_trim_threshold():
        return False
    try:
        # Lookup belongs inside the guard: loading the C library can fail with
        # more than OSError (an audit hook may veto ctypes.dlopen).
        opt = _mallopt()
        if opt is None:
            return False
        return bool(opt(M_TRIM_THRESHOLD, TRIM_THRESHOLD_BYTES))
    except Exception:  # noqa: BLE001  housekeeping must never fail the caller
        log.debug("mallopt failed", exc_info=True)
        return False


def release_free_heap() -> bool:
    """Ask the allocator to return free memory to the OS.

    Returns True when the request was made, False where the allocator offers no
    way to make it or the call failed. Never raises: this is housekeeping, and a
    failure must not take down the refresh loop or startup.
    """
    try:
        trim = _malloc_trim()
        if trim is None:
            return False
        trim(0)
    except Exception:  # noqa: BLE001  housekeeping must never fail the caller
        log.debug("malloc_trim failed", exc_info=True)
        return False
    return True
