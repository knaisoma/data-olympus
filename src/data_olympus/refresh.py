"""Periodic git pull + index rebuild loop. Runs as an asyncio task inside the
main server process; no sidecar (single owner of git state per Phase 2 spec §2.2)."""
from __future__ import annotations

import asyncio
import functools
import logging
import time
from typing import TYPE_CHECKING, Any

from data_olympus.index import DuplicateIdError, Index

if TYPE_CHECKING:
    from pathlib import Path

    from data_olympus.git_ops import GitOps
    from data_olympus.pending import PendingQueue
    from data_olympus.push_queue import PushQueue
    from data_olympus.server import ServerState

log = logging.getLogger("data_olympus.refresh")


def rebuild_index_safely(
    *, idx: Index, kb_main_path: Path, source_commit: str
) -> dict[str, Any]:
    """Try to rebuild the index. On DuplicateIdError or other failures, the
    previous index is preserved (because Index.build uses atomic swap)."""
    try:
        idx.build(kb_main_path, source_commit=source_commit)
        return {"outcome": "rebuilt", "error": None, "conflicts": []}
    except DuplicateIdError as e:
        log.warning("index rebuild failed (duplicate ids): %s", e)
        conflicts = [{"id": id_, "paths": paths} for id_, paths in e.conflicts.items()]
        return {"outcome": "failed", "error": str(e), "conflicts": conflicts}
    except Exception as e:
        log.exception("index rebuild failed: %s", e)
        return {"outcome": "failed", "error": f"{type(e).__name__}: {e}", "conflicts": []}


def refresh_once(
    *, git: GitOps, idx: Index, kb_main_path: Path
) -> dict[str, Any]:
    """One iteration of the refresh loop: fast-forward main, rebuild on SHA change."""
    result = git.ff_merge_origin_main(timeout_sec=30)
    if not result.changed:
        return {"outcome": "no_change", "error": None, "conflicts": [], "sha": result.current_sha}
    rebuilt = rebuild_index_safely(
        idx=idx, kb_main_path=kb_main_path, source_commit=result.current_sha
    )
    return {**rebuilt, "sha": result.current_sha}


async def git_pull_loop(state: ServerState, interval_sec: int) -> None:
    """Background asyncio task. Spawned via server.py lifespan; cancel on shutdown."""
    log.info("git_pull_loop started (interval=%ss)", interval_sec)
    while True:
        try:
            # refresh_once is keyword-only; functools.partial preserves that
            # when handed to run_in_executor which only forwards positional args.
            fn = functools.partial(
                refresh_once,
                git=state.git,
                idx=state.idx,
                kb_main_path=state.config.kb_main_path,
            )
            outcome = await asyncio.get_event_loop().run_in_executor(None, fn)
            state.last_git_pull_at = time.time()
            if outcome["outcome"] == "failed":
                state.last_index_build_status = "failed"
                state.last_index_error = outcome["error"]
                state.last_index_error_at = time.time()
                state.last_index_conflicts = outcome["conflicts"]
            elif outcome["outcome"] == "rebuilt":
                state.last_index_build_status = "ok"
                state.last_index_error = None
                state.last_index_error_at = None
                state.last_index_conflicts = []
                log.info("kb refreshed to %s", outcome.get("sha"))
        except asyncio.CancelledError:
            log.info("git_pull_loop cancelled")
            raise
        except Exception as e:
            log.warning("git_pull_loop iteration failed: %s", e)
        await asyncio.sleep(interval_sec)


async def push_retry_loop(
    *,
    push_queue: PushQueue,
    git: GitOps,
    interval_sec: int,
    max_attempts: int = 30,
) -> None:
    """Periodically drain the push queue. Cancellable via asyncio."""
    while True:
        try:
            push_queue.drain(
                push_fn=lambda wt: git.push(wt),
                max_attempts=max_attempts,
            )
        except Exception:
            log.exception("push_retry_loop iteration failed")
        await asyncio.sleep(interval_sec)


async def pending_gc_loop(
    *,
    pending: PendingQueue,
    timeout_sec: int,
    interval_sec: int,
) -> None:
    """Periodically expire pending entries older than timeout_sec."""
    while True:
        try:
            now = time.time()
            for entry in pending.list():
                if now - entry["created_at"] > timeout_sec:
                    pending.reject(entry["pending_id"])
        except Exception:
            log.exception("pending_gc_loop iteration failed")
        await asyncio.sleep(interval_sec)
