"""Crash-safe filesystem primitives used by push queue, pending queue, locks.

Sequence:
  write_to_tmp -> fsync_tmp -> os.replace(tmp, target) -> fsync_parent_dir
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any


def atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    """Crash-safe JSON write. Returns only after bytes are durable AND the
    rename + parent-dir entry are durable on disk.

    Raises FileNotFoundError if the parent directory does not exist (caller
    must ensure mkdir before first write).
    """
    parent = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".tmp.", dir=parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        dirfd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def atomic_remove(path: str) -> None:
    """Crash-safe removal: unlink + fsync parent. Missing file is a no-op."""
    parent = os.path.dirname(path) or "."
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    dirfd = os.open(parent, os.O_RDONLY)
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)
