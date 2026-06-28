"""4 MCP write tool functions."""
from __future__ import annotations

import contextlib
import datetime
import os
import re
import subprocess
import time
from typing import TYPE_CHECKING, Any

from data_olympus.audit_trailers import build_commit_message
from data_olympus.auth import PathBlocklist, is_writable_path, safe_join_under_root
from data_olympus.models import (
    PendingEntry,
    PendingListResponse,
    ProposeResponse,
    ResolvePendingResponse,
)
from data_olympus.pending import PathLockBusyError, PendingQueue, PendingQueueFullError

if TYPE_CHECKING:
    from data_olympus.audit_log import AuditLog
    from data_olympus.push_queue import PushQueue
    from data_olympus.rate_limit import SlidingWindowLimiter
    from data_olympus.worktrees import WorktreeRegistry


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")
    return s[:50] or "memory"


def _memory_inbox_prefix() -> str:
    """Directory prefix new memory proposals are written under.

    Defaults to the generic ``memory/inbox/``; a deployment with a different
    layout overrides it via KB_MEMORY_INBOX_PREFIX (a trailing slash is
    normalized in).
    """
    prefix = os.environ.get("KB_MEMORY_INBOX_PREFIX", "memory/inbox/").strip()
    return prefix if prefix.endswith("/") else prefix + "/"


def _classify(target_path: str) -> tuple[str, str]:
    """Reuse index.py's path classification."""
    from data_olympus.index import _classify_by_path
    return _classify_by_path(target_path)


def _emit_audit(
    audit_log: AuditLog | None,
    *,
    event_type: str,
    status: str,
    agent_identity: str | None = None,
    source_session: str | None = None,
    target_path: str | None = None,
    target_tier: str | None = None,
    confidence: float | None = None,
    pending_id: str | None = None,
    commit_sha: str | None = None,
    reason: str | None = None,
    remote_addr: str | None = None,
) -> None:
    if audit_log is None:
        return
    # audit emission is best-effort; don't fail the write
    with contextlib.suppress(Exception):
        audit_log.append({
            "ts": time.time(),
            "event_type": event_type,
            "status": status,
            "agent_identity": agent_identity,
            "source_session": source_session,
            "target_path": target_path,
            "target_tier": target_tier,
            "confidence": confidence,
            "pending_id": pending_id,
            "commit_sha": commit_sha,
            "reason": reason,
            "remote_addr": remote_addr,
        })


def kb_propose_memory_fn(
    *,
    text: str,
    tags: list[str],
    source_session: str,
    agent_identity: str,
    confidence: float,
    confidence_threshold: float,
    worktrees: WorktreeRegistry,
    push_queue: PushQueue,
    pending: PendingQueue,
    rate_limiter: SlidingWindowLimiter,
    blocklist: PathBlocklist,
    remote_addr: str,
    audit_log: AuditLog | None = None,
    can_auto_commit: bool = True,
    max_text_bytes: int = 0,
) -> ProposeResponse:
    """Propose a new memory file under the memory inbox prefix as <date>-<slug>.md.

    Structural rule is satisfied by construction; blocklist still applies.
    High confidence -> auto-commit + push enqueue. Low -> pending queue.

    ``can_auto_commit`` is the caller's authorization to skip operator review:
    when False (an authenticated principal lacking the auto_commit capability, or
    an untrusted caller) the proposal is parked as pending regardless of the
    client-asserted confidence. This is the confidence clamp.
    """
    today = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    slug = _slugify(text)
    target_path = f"{_memory_inbox_prefix()}{today}-{slug}.md"
    audit_base: dict[str, Any] = {
        "event_type": "propose_memory",
        "agent_identity": agent_identity,
        "source_session": source_session,
        "target_path": target_path,
        "confidence": confidence,
        "remote_addr": remote_addr,
    }

    # 1. Structural rule (cheap).
    if not is_writable_path(target_path):
        _emit_audit(audit_log, **{**audit_base, "status": "rejected_path_not_indexable",
                                   "reason": "not_md_or_excluded"})
        return ProposeResponse(
            status="rejected_path_not_indexable",
            reason="not_md_or_excluded",
            target_path=target_path,
        )

    # 2. Policy blocklist.
    target_tier, _ = _classify(target_path)
    audit_base["target_tier"] = target_tier
    if blocklist.blocks(target_path, target_tier):
        _emit_audit(audit_log, **{**audit_base, "status": "rejected_path_blocked",
                                   "reason": "tier_blocked"})
        return ProposeResponse(
            status="rejected_path_blocked",
            reason="tier_blocked",
            target_tier=target_tier,
            target_path=target_path,
        )

    # 3. Rate limit.
    if not rate_limiter.allow(remote_addr=remote_addr, agent_identity=agent_identity):
        _emit_audit(audit_log, **{**audit_base, "status": "rejected_rate_limited"})
        return ProposeResponse(status="rejected_rate_limited")

    # 3b. Payload size cap (reject before any disk side effect).
    if max_text_bytes > 0 and len(text.encode("utf-8")) > max_text_bytes:
        _emit_audit(audit_log, **{**audit_base, "status": "rejected_payload_too_large"})
        return ProposeResponse(status="rejected_payload_too_large",
                               target_path=target_path)

    # 4. Render the postimage (front matter + body).
    postimage = _render_memory(text=text, tags=tags, agent_identity=agent_identity)

    if confidence < confidence_threshold or not can_auto_commit:
        try:
            pid = pending.enqueue(
                proposal_type="memory",
                target_path=target_path,
                postimage=postimage,
                base_commit="HEAD",
                base_blob_sha=None,
                target_file_hash=None,
                meta={
                    "agent_identity": agent_identity,
                    "source_session": source_session,
                    "confidence": confidence,
                    "tags": tags,
                },
            )
        except PathLockBusyError:
            _emit_audit(audit_log, **{**audit_base, "status": "rejected_path_lock_busy"})
            return ProposeResponse(
                status="rejected_path_lock_busy",
                target_path=target_path,
            )
        except PendingQueueFullError:
            _emit_audit(audit_log, **{**audit_base, "status": "rejected_pending_queue_full"})
            return ProposeResponse(
                status="rejected_pending_queue_full",
                target_path=target_path,
            )
        _emit_audit(audit_log, **{**audit_base, "status": "pending_confirmation",
                                   "pending_id": pid})
        return ProposeResponse(
            status="pending_confirmation",
            pending_id=pid,
            proposal_text=text,
            operator_prompt=(
                f"Proposed memory at {target_path}. Accept (y), edit, or reject (n)?"
            ),
        )

    # High confidence: commit + enqueue push.
    wt = worktrees.get_or_create(
        source_session=source_session, agent_identity=agent_identity
    )
    # Symlink-escape containment (Codex blocker 1): a malicious KB commit can
    # plant the inbox dir as a symlink pointing outside the worktree. Reject
    # before any makedirs/open side effect.
    full_path = safe_join_under_root(wt.path, target_path)
    if full_path is None:
        _emit_audit(audit_log, **{**audit_base, "status": "rejected_symlink_escape"})
        return ProposeResponse(status="rejected_symlink_escape", target_path=target_path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(postimage)
    subprocess.run(["git", "-C", wt.path, "add", target_path], check=True)
    subject = f"propose: {target_path}"
    msg = build_commit_message(
        subject=subject,
        source_session=source_session,
        agent_identity=agent_identity,
        confidence_original=confidence,
        operator_confirmed=False,
        proposal_type="memory",
        target_tier=target_tier,
        target_path=target_path,
    )
    subprocess.run(["git", "-C", wt.path, "commit", "-m", msg], check=True)
    sha = subprocess.check_output(
        ["git", "-C", wt.path, "rev-parse", "HEAD"], text=True,
    ).strip()
    push_queue.enqueue(
        sha=sha,
        worktree_path=wt.path,
        meta={"source_session": source_session, "agent_identity": agent_identity},
    )
    _emit_audit(audit_log, **{**audit_base, "status": "committed", "commit_sha": sha})
    return ProposeResponse(status="committed", commit_sha=sha, push_state="queued")


def _render_memory(*, text: str, tags: list[str], agent_identity: str) -> str:
    fm = ["---"]
    fm.append(f"created_by: {agent_identity}")
    fm.append(f"created_at: {datetime.datetime.now(datetime.UTC).isoformat()}")
    if tags:
        fm.append("tags: [" + ", ".join(tags) + "]")
    fm.append("---")
    return "\n".join(fm) + "\n\n" + text + "\n"


def kb_propose_edit_fn(
    *,
    target_path: str,
    postimage: str,
    base_commit: str,
    base_blob_sha: str | None,
    target_file_hash: str | None,
    reason: str,
    source_session: str,
    agent_identity: str,
    confidence: float,
    confidence_threshold: float,
    worktrees: WorktreeRegistry,
    push_queue: PushQueue,
    pending: PendingQueue,
    rate_limiter: SlidingWindowLimiter,
    blocklist: PathBlocklist,
    remote_addr: str,
    audit_log: AuditLog | None = None,
    can_auto_commit: bool = True,
    max_postimage_bytes: int = 0,
) -> ProposeResponse:
    """Propose an edit to an existing (or new) file under target_path.

    ``can_auto_commit=False`` clamps the proposal to pending regardless of
    confidence (see kb_propose_memory_fn for the rationale)."""
    audit_base: dict[str, Any] = {
        "event_type": "propose_edit",
        "agent_identity": agent_identity,
        "source_session": source_session,
        "target_path": target_path,
        "confidence": confidence,
        "reason": reason,
        "remote_addr": remote_addr,
    }
    if not is_writable_path(target_path):
        _emit_audit(audit_log, **{**audit_base, "status": "rejected_path_not_indexable"})
        return ProposeResponse(status="rejected_path_not_indexable",
                               reason="traversal_or_excluded",
                               target_path=target_path)
    target_tier, _ = _classify(target_path)
    audit_base["target_tier"] = target_tier
    if blocklist.blocks(target_path, target_tier):
        _emit_audit(audit_log, **{**audit_base, "status": "rejected_path_blocked"})
        return ProposeResponse(status="rejected_path_blocked",
                               reason="tier_blocked",
                               target_tier=target_tier,
                               target_path=target_path)
    if not rate_limiter.allow(remote_addr=remote_addr, agent_identity=agent_identity):
        _emit_audit(audit_log, **{**audit_base, "status": "rejected_rate_limited"})
        return ProposeResponse(status="rejected_rate_limited")

    if max_postimage_bytes > 0 and len(postimage.encode("utf-8")) > max_postimage_bytes:
        _emit_audit(audit_log, **{**audit_base, "status": "rejected_payload_too_large"})
        return ProposeResponse(status="rejected_payload_too_large",
                               target_path=target_path)

    if confidence < confidence_threshold or not can_auto_commit:
        try:
            pid = pending.enqueue(
                proposal_type="edit",
                target_path=target_path,
                postimage=postimage,
                base_commit=base_commit,
                base_blob_sha=base_blob_sha,
                target_file_hash=target_file_hash,
                meta={"agent_identity": agent_identity,
                      "source_session": source_session,
                      "confidence": confidence,
                      "reason": reason},
            )
        except PathLockBusyError:
            _emit_audit(audit_log, **{**audit_base, "status": "rejected_path_lock_busy"})
            return ProposeResponse(status="rejected_path_lock_busy",
                                   target_path=target_path)
        except PendingQueueFullError:
            _emit_audit(audit_log, **{**audit_base, "status": "rejected_pending_queue_full"})
            return ProposeResponse(status="rejected_pending_queue_full",
                                   target_path=target_path)
        _emit_audit(audit_log, **{**audit_base, "status": "pending_confirmation",
                                   "pending_id": pid})
        return ProposeResponse(
            status="pending_confirmation",
            pending_id=pid,
            proposal_text=postimage,
            operator_prompt=f"Proposed edit to {target_path}. Accept (y), edit, or reject (n)?",
        )

    wt = worktrees.get_or_create(source_session=source_session, agent_identity=agent_identity)
    # Symlink-escape containment via the shared guard (see safe_join_under_root).
    full_path = safe_join_under_root(wt.path, target_path)
    if full_path is None:
        _emit_audit(audit_log, **{**audit_base, "status": "rejected_symlink_escape"})
        return ProposeResponse(status="rejected_symlink_escape",
                               target_path=target_path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(postimage)
    subprocess.run(["git", "-C", wt.path, "add", target_path], check=True)
    subject = f"edit: {target_path}"
    msg = build_commit_message(
        subject=subject,
        source_session=source_session,
        agent_identity=agent_identity,
        confidence_original=confidence,
        operator_confirmed=False,
        proposal_type="edit",
        target_tier=target_tier,
        target_path=target_path,
    )
    subprocess.run(["git", "-C", wt.path, "commit", "-m", msg], check=True)
    sha = subprocess.check_output(
        ["git", "-C", wt.path, "rev-parse", "HEAD"], text=True,
    ).strip()
    push_queue.enqueue(sha=sha, worktree_path=wt.path,
                       meta={"source_session": source_session,
                             "agent_identity": agent_identity, "reason": reason})
    _emit_audit(audit_log, **{**audit_base, "status": "committed", "commit_sha": sha})
    return ProposeResponse(status="committed", commit_sha=sha, push_state="queued")


def kb_resolve_pending_fn(
    *,
    pending_id: str,
    decision: str,
    edited_text: str | None,
    worktrees: WorktreeRegistry,
    push_queue: PushQueue,
    pending: PendingQueue,
    source_session: str,
    agent_identity: str,
    audit_log: AuditLog | None = None,
) -> ResolvePendingResponse:
    audit_base: dict[str, Any] = {
        "event_type": "resolve",
        "agent_identity": agent_identity,
        "source_session": source_session,
        "pending_id": pending_id,
    }
    if decision == "reject":
        pending.reject(pending_id)
        _emit_audit(audit_log, **{**audit_base, "status": "rejected"})
        return ResolvePendingResponse(status="rejected")
    if decision not in ("approve", "edit"):
        _emit_audit(audit_log, **{**audit_base, "status": "rejected_bad_decision"})
        return ResolvePendingResponse(status="rejected_bad_decision")

    resolved = pending.approve(pending_id, edited_text=edited_text)
    target_tier, _ = _classify(resolved.target_path)
    audit_base["target_path"] = resolved.target_path
    audit_base["target_tier"] = target_tier
    try:
        audit_base["confidence"] = float(resolved.meta.get("confidence", 0.0))
    except (TypeError, ValueError):
        audit_base["confidence"] = None
    wt = worktrees.get_or_create(source_session=source_session,
                                 agent_identity=agent_identity)
    full_path = safe_join_under_root(wt.path, resolved.target_path)
    if full_path is None:
        _emit_audit(audit_log, **{**audit_base, "status": "rejected_symlink_escape"})
        return ResolvePendingResponse(status="rejected_symlink_escape")
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(resolved.postimage)
    subprocess.run(["git", "-C", wt.path, "add", resolved.target_path], check=True)
    subject = f"resolve: {resolved.target_path}"
    msg = build_commit_message(
        subject=subject,
        source_session=resolved.meta.get("source_session", source_session),
        agent_identity=resolved.meta.get("agent_identity", agent_identity),
        confidence_original=float(resolved.meta.get("confidence", 0.0)),
        operator_confirmed=True,
        proposal_type=resolved.proposal_type,
        target_tier=target_tier,
        target_path=resolved.target_path,
    )
    subprocess.run(["git", "-C", wt.path, "commit", "-m", msg], check=True)
    sha = subprocess.check_output(
        ["git", "-C", wt.path, "rev-parse", "HEAD"], text=True,
    ).strip()
    push_queue.enqueue(sha=sha, worktree_path=wt.path, meta={})
    _emit_audit(audit_log, **{**audit_base, "status": "committed", "commit_sha": sha})
    return ResolvePendingResponse(status="committed", commit_sha=sha)


def kb_list_pending_fn(*, pending: PendingQueue) -> PendingListResponse:
    return PendingListResponse(
        pending=[
            PendingEntry(
                pending_id=e["pending_id"],
                proposal_type=e["proposal_type"],
                target_path=e["target_path"],
                confidence=e.get("confidence"),
                agent_identity=e.get("agent_identity"),
                created_at=e["created_at"],
            )
            for e in pending.list()
        ]
    )
