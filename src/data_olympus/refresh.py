"""Periodic git pull + index rebuild loop. Runs as an asyncio task inside the
main server process; no sidecar (single owner of git state)."""
from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import time
from typing import TYPE_CHECKING, Any

from data_olympus.index import DuplicateIdError, Index

if TYPE_CHECKING:
    from contextlib import AbstractContextManager
    from pathlib import Path

    from data_olympus.audit_log import AuditLog
    from data_olympus.git_ops import GitOps
    from data_olympus.pending import PendingQueue
    from data_olympus.push_queue import PushQueue
    from data_olympus.server import ServerState
    from data_olympus.worktrees import WorktreeRegistry

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
    """One iteration of the refresh loop: fast-forward main, rebuild on SHA change.

    The returned dict carries both the index ``outcome`` (no_change / rebuilt /
    failed) and the git ``sync_status`` (changed / no_change / no_remote /
    fetch_failed / ff_failed) plus ``remote_head_sha``, so the loop reports sync
    failures distinctly from index-build failures."""
    result = git.ff_merge_origin_main(timeout_sec=30)
    sync = {
        "sync_status": result.status,
        "remote_head_sha": result.remote_sha,
        "note": result.note,
    }
    if not result.changed:
        return {"outcome": "no_change", "error": None, "conflicts": [],
                "sha": result.current_sha, **sync}
    rebuilt = rebuild_index_safely(
        idx=idx, kb_main_path=kb_main_path, source_commit=result.current_sha
    )
    return {**rebuilt, "sha": result.current_sha, **sync}


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
            now = time.time()
            # Sync-status visibility: a fetch/ff failure must NOT look "fresh".
            # We record the failure and deliberately do not advance
            # last_git_pull_at, so staleness climbs and health degrades instead of
            # reporting a fresh no-change against a broken remote.
            sync_status = outcome.get("sync_status", "no_change")
            state.last_git_fetch_status = sync_status
            state.last_git_fetch_at = now
            state.remote_head_sha = outcome.get("remote_head_sha")
            if sync_status in ("fetch_failed", "ff_failed"):
                state.last_git_fetch_error = outcome.get("note") or sync_status
            else:
                state.last_git_fetch_error = None
                state.last_git_pull_at = now
                if sync_status in ("changed", "no_change"):
                    state.last_successful_refresh_at = now
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
            # Maintenance ledger (issue #113): checked on EVERY tick, not only
            # a "rebuilt" outcome. A fresh deployment whose remote never
            # changes would otherwise get "no_change" forever and the ledger
            # would never be created. maybe_update_ledger is a cheap no-op
            # comparison when the state has not changed (idx.maintenance_state
            # vs. the state parsed back out of the currently-indexed ledger
            # doc), so checking every tick is inexpensive. Only attempted when
            # the write pipeline is initialised (not a read-only replica /
            # no remote configured); best-effort, never raises.
            if (
                state.worktrees is not None
                and state.push_queue is not None
                and state.pending is not None
            ):
                from data_olympus.maintenance import maybe_update_ledger
                try:
                    fn2 = functools.partial(
                        maybe_update_ledger,
                        idx=state.idx, worktrees=state.worktrees,
                        push_queue=state.push_queue, pending=state.pending,
                        serializer=state.write_serializer,
                        audit_log=state.audit_log,
                        ledger_path=state.config.maintenance_ledger_path,
                    )
                    await asyncio.get_event_loop().run_in_executor(None, fn2)
                except Exception:
                    log.exception("maintenance ledger update failed")
        except asyncio.CancelledError:
            log.info("git_pull_loop cancelled")
            raise
        except Exception as e:
            log.warning("git_pull_loop iteration failed: %s", e)
        await asyncio.sleep(interval_sec)


def demote_conflict_to_pending(
    entry: dict[str, Any],
    *,
    git: GitOps,
    pending: PendingQueue,
    audit_log: AuditLog | None,
) -> None:
    """Demote a push-queue entry whose commit could not be rebased onto the moved
    origin/main to a pending proposal for operator resolution (scope item 2).

    Called by the push loop's ``on_rebase_conflict`` hook. Reads the target path
    and postimage FROM THE COMMIT (``git show <sha>:<path>``) rather than the
    worktree (which may have moved on), enqueues a pending entry carrying the base
    the commit sat on, and records a ``push_conflict_demoted`` audit event with a
    distinct status so health and ``kb_list_pending`` surface it. The commit stays
    in the session branch history; publishing it is now gated on operator resolve.
    """
    from data_olympus.pending import PathLockBusyError, PendingQueueFullError

    sha = entry.get("sha", "")
    wt = entry.get("worktree_path", "")
    changed = git.files_changed_in_commit(sha, worktree_path=wt)
    demoted = 0
    for path in changed:
        postimage = git.file_at_commit(sha, path, worktree_path=wt)
        if postimage is None:
            continue  # deletion or unreadable; nothing to re-propose
        try:
            pid = pending.enqueue(
                proposal_type="edit",
                target_path=path,
                postimage=postimage,
                base_commit=f"{sha}^",
                base_blob_sha=None,
                target_file_hash=None,
                meta={
                    "agent_identity": entry.get("meta", {}).get(
                        "agent_identity", "unknown"),
                    "source_session": entry.get("meta", {}).get(
                        "source_session", "push-conflict"),
                    "confidence": 0.0,
                    "reason": "demoted from push queue after rebase conflict",
                    "demoted_from_sha": sha,
                },
            )
        except (PathLockBusyError, PendingQueueFullError):
            # A pending proposal for this path already exists (or the queue is
            # full). Leaving the commit un-demoted here would drop it, so re-raise
            # so the drain keeps the queue entry and retries the demotion later.
            raise
        demoted += 1
        if audit_log is not None:
            with contextlib.suppress(Exception):
                audit_log.append({
                    "ts": time.time(),
                    "event_type": "push_conflict_demoted",
                    "status": "demoted_to_pending",
                    "pending_id": pid,
                    "target_path": path,
                    "commit_sha": sha,
                    "reason": "non-fast-forward push did not rebase cleanly",
                })
    if demoted == 0:
        # Nothing was re-proposed (e.g. the commit only deleted files). Raise so
        # the entry is kept and surfaces rather than being silently dropped.
        raise RuntimeError(f"nothing demotable in commit {sha}")


async def push_retry_loop(
    *,
    push_queue: PushQueue,
    git: GitOps,
    interval_sec: int,
    max_attempts: int = 30,
    push_timeout_sec: int = 60,
    pending: PendingQueue | None = None,
    audit_log: AuditLog | None = None,
) -> None:
    """Periodically drain the push queue. Cancellable via asyncio.

    ``drain`` shells out to ``git push`` (blocking I/O), so it runs in a thread
    executor rather than on the event loop: a hung origin must never block the
    loop that also answers the readiness probe (the probe timing out would
    crash-loop the pod). The push now goes through ``push_with_rebase_recovery``:
    a non-fast-forward rejection (a second overlapping session moved origin/main)
    triggers a fetch + rebase + retry instead of retrying identically forever. A
    rebase CONFLICT raises ``RebaseConflictError``, which ``drain`` routes to the
    ``on_rebase_conflict`` demotion hook (the commit becomes a pending entry for
    operator resolution). Every other failure (network, timeout, auth) stays a
    retryable failure exactly as before.
    """
    on_conflict = None
    if pending is not None:
        on_conflict = functools.partial(
            demote_conflict_to_pending, git=git, pending=pending,
            audit_log=audit_log,
        )
    while True:
        try:
            fn = functools.partial(
                push_queue.drain,
                push_fn=lambda wt: git.push_with_rebase_recovery(
                    wt, timeout_sec=push_timeout_sec),
                max_attempts=max_attempts,
                on_rebase_conflict=on_conflict,
            )
            await asyncio.get_event_loop().run_in_executor(None, fn)
        except Exception:
            log.exception("push_retry_loop iteration failed")
        await asyncio.sleep(interval_sec)


async def worktree_gc_loop(
    *,
    worktrees: WorktreeRegistry,
    idle_sec: int,
    interval_sec: int,
) -> None:
    """Periodically GC idle per-session worktrees. Cancellable via asyncio.

    Without this loop one full KB checkout accumulates per session forever.
    ``WorktreeRegistry.gc`` removes only worktrees idle beyond ``idle_sec`` whose
    commits are all reachable from origin/main (it defers any with unpushed
    commits so the push queue can drain first) AND deletes the session's
    ``kb-session/<safe_id>`` branch so a returning session can create its
    worktree again. Runs in a thread executor because gc shells out to git."""
    while True:
        try:
            fn = functools.partial(worktrees.gc, idle_sec=idle_sec)
            removed = await asyncio.get_event_loop().run_in_executor(None, fn)
            if removed:
                log.info("worktree gc removed %d idle worktree(s): %s",
                         len(removed), ", ".join(removed))
        except Exception:
            log.exception("worktree_gc_loop iteration failed")
        await asyncio.sleep(interval_sec)


async def pending_gc_loop(
    *,
    pending: PendingQueue,
    timeout_sec: int,
    interval_sec: int,
    auto_commit_lock_ttl_sec: float = 600,
    write_serializer: AbstractContextManager[Any] | None = None,
    audit_log: AuditLog | None = None,
) -> None:
    """Periodically expire pending entries older than timeout_sec and reclaim
    orphaned path locks.

    On expiry an audit event is emitted (scope item 7): the old loop silently
    rejected entries >timeout, so an operator who lost a proposal to expiry had no
    record. Each expired entry now records an ``expired`` / ``auto_rejected``
    audit event before it is rejected. A concurrent operator resolve can win the
    race (the entry is already gone by the time we reject it); that
    ``PendingAlreadyResolvedError`` / ``PendingNotFoundError`` is swallowed so the
    loop does not crash on a benign race.

    Orphaned path locks (a crash between lock create and entry write, scope item
    5) are reclaimed each pass via ``gc_orphan_locks`` so a path is not locked
    forever with no entry to release it. Crash-orphaned AUTO-COMMIT locks (held
    across a seconds-long commit, wedged by a hard kill) are separately reclaimed
    each pass once older than ``auto_commit_lock_ttl_sec``; pending-proposal locks
    are never TTL-reclaimed.
    """
    from data_olympus.pending import (
        PendingAlreadyResolvedError,
        PendingNotFoundError,
    )
    while True:
        try:
            now = time.time()
            for entry in pending.list():
                if now - entry["created_at"] > timeout_sec:
                    pid = entry["pending_id"]
                    # Emit the expiry audit BEFORE rejecting so the record exists
                    # even if the reject then races an operator resolve.
                    if audit_log is not None:
                        with contextlib.suppress(Exception):
                            audit_log.append({
                                "ts": now,
                                "event_type": "pending_expired",
                                "status": "auto_rejected",
                                "pending_id": pid,
                                "target_path": entry.get("target_path"),
                                "agent_identity": entry.get("agent_identity"),
                                "reason": f"pending age exceeded {timeout_sec}s",
                            })
                    with contextlib.suppress(
                        PendingAlreadyResolvedError, PendingNotFoundError,
                    ):
                        pending.reject(pid)
            reclaimed = pending.gc_orphan_locks()
            if reclaimed:
                log.warning("pending_gc reclaimed %d orphaned path lock(s)",
                            reclaimed)
            # Defense in depth: never pass max_age_sec<=0 from the periodic path,
            # since 0 is the unconditional (startup) reclaim sentinel and would free
            # fresh auto-commit locks. Config already clamps this; guard anyway.
            if auto_commit_lock_ttl_sec > 0:
                stale_ac = pending.reclaim_stale_auto_commit_locks(
                    max_age_sec=auto_commit_lock_ttl_sec,
                    serializer=write_serializer,
                )
                if stale_ac:
                    log.warning(
                        "pending_gc reclaimed %d stale auto-commit path lock(s) "
                        "(older than %ss)", stale_ac, auto_commit_lock_ttl_sec,
                    )
        except Exception:
            log.exception("pending_gc_loop iteration failed")
        await asyncio.sleep(interval_sec)
