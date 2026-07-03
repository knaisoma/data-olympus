"""4 MCP write tool functions."""
from __future__ import annotations

import contextlib
import datetime
import logging
import os
import re
import subprocess
import time
from typing import TYPE_CHECKING, Any, Literal

from data_olympus.audit_trailers import build_commit_message
from data_olympus.auth import (
    PathBlocklist,
    is_writable_path,
    normalize_target_path,
    safe_join_under_root,
)
from data_olympus.format.frontmatter import parse_frontmatter
from data_olympus.models import (
    PendingEntry,
    PendingListResponse,
    ProposeResponse,
    ResolvePendingResponse,
)
from data_olympus.pending import PathLockBusyError, PendingQueue, PendingQueueFullError
from data_olympus.write_gate import (
    WriteSerializer,
    check_cas,
    reset_worktree,
    validate_postimage,
)

if TYPE_CHECKING:
    from data_olympus.audit_log import AuditLog
    from data_olympus.index import Index
    from data_olympus.push_queue import PushQueue
    from data_olympus.rate_limit import SlidingWindowLimiter
    from data_olympus.worktrees import WorktreeRegistry


# A shared write serializer used when a caller does not supply one. The
# production server constructs a single instance and threads it into every write
# call so they share one lock; tests that call the write functions directly get
# this module-level default (each call still serializes correctly within a
# process). Threading one explicit instance from the server is what makes the
# guarantee hold across REST + MCP surfaces.
_DEFAULT_SERIALIZER = WriteSerializer()

_log = logging.getLogger("data_olympus.tools_write")


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")
    return s[:50] or "memory"


def _memory_uniquifier(source_session: str, text: str) -> str:
    """Short deterministic suffix so two same-day, same-slug memories do not
    collide on ``<date>-<slug>.md`` and silently overwrite (scope item 6).

    Derived from the session id + body so the same logical memory maps to the
    same filename (idempotent re-proposal) while a different memory gets a
    different name. 6 hex chars is enough entropy for the per-day, per-slug
    bucket and keeps the filename readable."""
    import hashlib
    h = hashlib.sha256(f"{source_session}\0{text}".encode())
    return h.hexdigest()[:6]


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


class _WriteRejected(Exception):
    """Internal control-flow signal: a gate (symlink / CAS / validation) rejected
    the write inside the locked critical section. Carries the ProposeResponse to
    return. Never escapes the module."""

    def __init__(self, response: ProposeResponse) -> None:
        self.response = response
        super().__init__(response.status)


def _commit_in_worktree(
    *,
    worktrees: WorktreeRegistry,
    push_queue: PushQueue,
    pending: PendingQueue,
    serializer: WriteSerializer,
    idx: Index | None,
    source_session: str,
    agent_identity: str,
    target_path: str,
    postimage: str,
    proposal_type: Literal["memory", "edit"],
    subject: str,
    target_tier: str,
    confidence: float,
    operator_confirmed: bool,
    base_commit: str | None = None,
    base_blob_sha: str | None = None,
    target_file_hash: str | None = None,
    push_meta: dict[str, Any] | None = None,
    lock_owner: str | None = None,
    hold_path_lock: bool = False,
) -> tuple[str, str]:
    """Serialized write -> git add -> commit -> enqueue critical section.

    Shared by the auto-commit (propose) path and the operator-resolve path so both
    honor identical integrity gates (scope items 1, 3, 4, 8). Returns
    ``(commit_sha, push_state)`` where push_state is ``"queued"`` or
    ``"enqueue_failed_recovery_pending"`` (see _enqueue_after_commit). Raises
    :class:`_WriteRejected` (carrying the ProposeResponse) when a gate rejects, or
    :class:`PathLockBusyError` when the per-path advisory lock is held.

    ``hold_path_lock`` (resolve path): when True the caller ALREADY holds the
    per-path advisory lock (the one acquired at ``enqueue`` time and kept through
    ``claim_for_resolve``), so this does not re-acquire it. Re-acquiring the same
    file-based lock would self-deadlock / raise PathLockBusyError (Codex round-2
    Blocker B). When False (the propose path) the lock is acquired here.

    Order of operations, all under the process-wide write lock AND the per-path
    advisory lock (shared with the pending queue):

    1. Refresh the worktree base onto origin/main (so CAS compares against, and the
       commit sits on, current content).
    2. Build the commit message FIRST (trailer validation can raise; doing it
       before any disk side effect means a late rejection leaves nothing staged --
       scope item 8).
    3. Symlink-escape containment on the join.
    4. CAS: if the caller supplied a base marker and the refreshed target content
       differs, reject rejected_stale_base without committing (item 3).
    5. Content-validation gate on the postimage (item 4).
    6. Write + git add + commit + enqueue. On ANY failure after the add, hard-reset
       the worktree so no staged leftovers are swept into the next commit (item 8).
    """
    with serializer:
        owner = lock_owner or f"auto-commit:{source_session}"
        path_lock_ctx = (
            contextlib.nullcontext() if hold_path_lock
            else pending.path_lock(target_path, owner=owner)
        )
        with path_lock_ctx:
            wt = worktrees.get_or_create(
                source_session=source_session, agent_identity=agent_identity
            )
            # 1. Refresh the session branch base onto origin/main. On a rebase
            # conflict the current base cannot be advanced; fall back to the
            # unrefreshed base rather than failing the write (the push path's
            # non-FF recovery handles publication). Network/other errors are
            # likewise non-fatal here.
            with contextlib.suppress(Exception):
                worktrees.git.refresh_base(wt.path)

            # 2. Build the commit message BEFORE any disk write (item 8).
            msg = build_commit_message(
                subject=subject,
                source_session=source_session,
                agent_identity=agent_identity,
                confidence_original=confidence,
                operator_confirmed=operator_confirmed,
                proposal_type=proposal_type,
                target_tier=target_tier,
                target_path=target_path,
            )

            # 3. Symlink-escape containment.
            full_path = safe_join_under_root(wt.path, target_path)
            if full_path is None:
                raise _WriteRejected(ProposeResponse(
                    status="rejected_symlink_escape", target_path=target_path))

            # 4. CAS against the refreshed base (item 3).
            cas = check_cas(
                worktree_path=wt.path, target_path=target_path,
                base_commit=base_commit, base_blob_sha=base_blob_sha,
                target_file_hash=target_file_hash,
            )
            if not cas.ok:
                raise _WriteRejected(ProposeResponse(
                    status="rejected_stale_base", target_path=target_path,
                    reason=cas.reason))

            # 5. Content-validation gate (item 4). Pass the worktree so the
            # duplicate-id check also scans the committed tree (catches a
            # same-new-id race between two serialized commits before the index
            # rebuilds).
            vr = validate_postimage(
                target_path=target_path, postimage=postimage, idx=idx,
                worktree_path=wt.path)
            if not vr.ok:
                raise _WriteRejected(ProposeResponse(
                    status="rejected_invalid_document", target_path=target_path,
                    reason="; ".join(e["message"] for e in vr.errors)))

            # 6. Write + add + commit + enqueue; reset on any post-add failure.
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(postimage)
            try:
                subprocess.run(
                    ["git", "-C", wt.path, "add", target_path], check=True)
                subprocess.run(
                    ["git", "-C", wt.path, "commit", "-m", msg], check=True)
                sha = subprocess.check_output(
                    ["git", "-C", wt.path, "rev-parse", "HEAD"], text=True,
                ).strip()
            except Exception:
                # Something failed after the file was written/staged; discard so
                # the leftover is not swept into the session's next commit (item 8).
                with contextlib.suppress(Exception):
                    reset_worktree(wt.path)
                raise
            # Enqueue AFTER the commit is durable. It must not turn a made commit
            # into an exception path (that would drop the bootstrap in-flight guard
            # and lose the resolve claim -- Codex round-2 Blocker C), so a failure
            # is not raised; but the push_state returned to the caller is TRUTHFUL
            # (Codex round-3): "queued" only when the queue entry actually landed,
            # else "enqueue_failed_recovery_pending", and an in-process recovery
            # pass on THIS worktree re-enqueues the orphan so the running push loop
            # picks it up without waiting for a restart.
            push_state = _enqueue_after_commit(push_queue, sha, wt.path, push_meta)
            return sha, push_state


def _enqueue_after_commit(
    push_queue: PushQueue, sha: str, worktree_path: str,
    push_meta: dict[str, Any] | None,
) -> str:
    """Enqueue a committed sha for push and return a truthful push_state.

    Returns:
    - ``"queued"`` when the queue entry landed durably.
    - ``"enqueue_failed_recovery_pending"`` when the initial enqueue AND an
      in-process recovery retry both failed. The commit is durable on the session
      branch, so it is still recoverable (an operator / a restart's init_recovery
      re-enqueues it), but the caller must not claim it is queued.

    The recovery retry re-runs the same durable enqueue once. This lands the
    orphan in the live queue so the running push loop drains it without a restart
    (closing the "silently unpublished until restart" gap Codex round-3 raised)."""
    try:
        push_queue.enqueue(sha=sha, worktree_path=worktree_path,
                           meta=push_meta or {})
        return "queued"
    except Exception:
        _log.exception(
            "push-queue enqueue failed after commit %s; attempting in-process "
            "recovery re-enqueue", sha)
    # In-process recovery: retry the durable enqueue once more. If it lands, the
    # live push loop will drain it; if not, the commit is still on the branch and
    # startup init_recovery is the backstop.
    try:
        push_queue.enqueue(sha=sha, worktree_path=worktree_path,
                           meta={**(push_meta or {}), "recovered_in_process": True})
        _log.warning("in-process recovery re-enqueued orphaned commit %s", sha)
        return "queued"
    except Exception:
        _log.exception(
            "in-process recovery re-enqueue ALSO failed for commit %s; the commit "
            "is durable on the session branch and will be recovered by startup "
            "init_recovery, but is NOT queued in this process", sha)
        return "enqueue_failed_recovery_pending"


def commit_multifile_in_worktree(
    *,
    worktrees: WorktreeRegistry,
    push_queue: PushQueue,
    pending: PendingQueue,
    serializer: WriteSerializer,
    idx: Index | None,
    source_session: str,
    agent_identity: str,
    files: list[dict[str, str]],
    subject: str,
    target_tier: str,
    target_path_for_msg: str,
    confidence: float,
    push_meta: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Serialized multi-file write -> add -> ONE commit -> enqueue (Codex Blocker 2).

    The bootstrap path writes several files in a single atomic commit. It now goes
    through the SAME integrity discipline as the single-file path: the process-wide
    write serializer, per-path advisory locks on every target (shared with the
    pending queue), the content-validation gate on every postimage, and a hard
    reset on any post-add failure so a partial bundle never leaks into the next
    commit. CAS is not applied here (bootstrap creates NEW files under a
    not-yet-onboarded workspace; there is no base to compare). Returns
    ``(commit_sha, push_state)``. Raises :class:`_WriteRejected` on a gate failure
    or :class:`PathLockBusyError` when any target path is already locked.
    """
    with serializer, contextlib.ExitStack() as stack:
        owner = f"bootstrap:{source_session}"
        # Lock every target path up front (all-or-nothing): if any is busy the
        # whole bundle is rejected, matching the atomic-commit contract.
        for f in files:
            stack.enter_context(pending.path_lock(f["target_path"], owner=owner))
        wt = worktrees.get_or_create(
            source_session=source_session, agent_identity=agent_identity)
        with contextlib.suppress(Exception):
            worktrees.git.refresh_base(wt.path)

        # Build the commit message first (trailer validation can raise).
        msg = build_commit_message(
            subject=subject, source_session=source_session,
            agent_identity=agent_identity, confidence_original=confidence,
            operator_confirmed=False, proposal_type="edit",
            target_tier=target_tier, target_path=target_path_for_msg,
        )

        # Intra-bundle duplicate-id check (Codex round-2 Blocker A): the per-file
        # validate_postimage below only sees the live index + the committed tree,
        # neither of which yet contains this bundle. Two bundle files carrying the
        # SAME effective id at different paths would both pass and then poison the
        # next index rebuild. Catch it up front by computing each file's effective
        # id (explicit or path-derived, matching the rebuild) and rejecting any id
        # claimed by more than one path in the bundle.
        from data_olympus.write_gate import _effective_doc_id
        seen_ids: dict[str, str] = {}
        for f in files:
            tp, pi = f["target_path"], f["postimage"]
            try:
                bfm, _ = parse_frontmatter(pi)
            except ValueError:
                bfm = {}
            eid = _effective_doc_id(bfm, tp)
            if eid and eid in seen_ids and seen_ids[eid] != tp:
                raise _WriteRejected(ProposeResponse(
                    status="rejected_invalid_document", target_path=tp,
                    reason=(f"id '{eid}' used by two files in the same bundle: "
                            f"'{seen_ids[eid]}' and '{tp}'")))
            if eid:
                seen_ids[eid] = tp

        # Containment + validation for every file BEFORE any write.
        for f in files:
            tp, pi = f["target_path"], f["postimage"]
            full = safe_join_under_root(wt.path, tp)
            if full is None:
                raise _WriteRejected(ProposeResponse(
                    status="rejected_symlink_escape", target_path=tp))
            vr = validate_postimage(
                target_path=tp, postimage=pi, idx=idx, worktree_path=wt.path)
            if not vr.ok:
                raise _WriteRejected(ProposeResponse(
                    status="rejected_invalid_document", target_path=tp,
                    reason="; ".join(e["message"] for e in vr.errors)))

        # Write + add every file, then one commit; reset on any failure.
        try:
            for f in files:
                tp, pi = f["target_path"], f["postimage"]
                full = safe_join_under_root(wt.path, tp)
                assert full is not None  # re-checked; validated above
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w", encoding="utf-8") as out:
                    out.write(pi)
                subprocess.run(["git", "-C", wt.path, "add", tp], check=True)
            subprocess.run(["git", "-C", wt.path, "commit", "-m", msg],
                           check=True)
            sha = subprocess.check_output(
                ["git", "-C", wt.path, "rev-parse", "HEAD"], text=True).strip()
        except Exception:
            with contextlib.suppress(Exception):
                reset_worktree(wt.path)
            raise
        push_state = _enqueue_after_commit(push_queue, sha, wt.path, push_meta)
        return sha, push_state


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
    serializer: WriteSerializer | None = None,
    idx: Index | None = None,
) -> ProposeResponse:
    """Propose a new memory file under the memory inbox prefix as
    <date>-<slug>-<uniq>.md.

    Structural rule is satisfied by construction; blocklist still applies.
    High confidence -> auto-commit + push enqueue. Low -> pending queue.

    ``can_auto_commit`` is the caller's authorization to skip operator review:
    when False (an authenticated principal lacking the auto_commit capability, or
    an untrusted caller) the proposal is parked as pending regardless of the
    client-asserted confidence. This is the confidence clamp.
    """
    today = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    slug = _slugify(text)
    uniq = _memory_uniquifier(source_session, text)
    target_path = f"{_memory_inbox_prefix()}{today}-{slug}-{uniq}.md"
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

    # 4. Render the postimage (front matter + body) BEFORE the size cap so the
    # cap counts the FULL committed bytes, not just the body: the frontmatter the
    # server prepends (tags, agent identity, ISO timestamp) is part of what gets
    # written and pushed, and an unbounded tags list would otherwise slip past a
    # body-only check (item 3).
    postimage = _render_memory(text=text, tags=tags, agent_identity=agent_identity)

    # 4b. Payload size cap (reject before any disk side effect).
    if max_text_bytes > 0 and len(postimage.encode("utf-8")) > max_text_bytes:
        _emit_audit(audit_log, **{**audit_base, "status": "rejected_payload_too_large"})
        return ProposeResponse(status="rejected_payload_too_large",
                               target_path=target_path)

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

    # High confidence: serialized commit + enqueue push (items 1, 4, 8).
    try:
        sha, push_state = _commit_in_worktree(
            worktrees=worktrees, push_queue=push_queue, pending=pending,
            serializer=serializer or _DEFAULT_SERIALIZER, idx=idx,
            source_session=source_session, agent_identity=agent_identity,
            target_path=target_path, postimage=postimage,
            proposal_type="memory", subject=f"propose: {target_path}",
            target_tier=target_tier, confidence=confidence,
            operator_confirmed=False,
            push_meta={"source_session": source_session,
                       "agent_identity": agent_identity},
        )
    except _WriteRejected as rej:
        resp = rej.response
        _emit_audit(audit_log, **{**audit_base, "status": resp.status,
                                   "reason": resp.reason})
        return resp
    except PathLockBusyError:
        _emit_audit(audit_log, **{**audit_base, "status": "rejected_path_lock_busy"})
        return ProposeResponse(status="rejected_path_lock_busy",
                               target_path=target_path)
    _emit_audit(audit_log, **{**audit_base, "status": "committed", "commit_sha": sha})
    return ProposeResponse(status="committed", commit_sha=sha, push_state=push_state)


def _render_memory(*, text: str, tags: list[str], agent_identity: str) -> str:
    """Render a memory file: YAML frontmatter block + body.

    The frontmatter is built as a dict and serialized with ``yaml.safe_dump``
    (item 3). String-concatenating ``agent_identity`` / ``tags`` into the YAML
    (the previous approach) let a value containing a newline or ``]`` inject
    arbitrary top-level keys: e.g. an ``agent_identity`` of
    ``x\\nid: GDEC-001\\nstatus: accepted`` or a tag of ``a], id: forged`` would
    forge reserved keys (``id`` / ``status`` / ``supersedes``). A forged duplicate
    ``id`` breaks every index rebuild. ``safe_dump`` quotes and escapes any value
    that would otherwise change the document structure, so newline/bracket
    payloads survive only as data inside the intended key.
    """
    import yaml

    fm: dict[str, Any] = {
        "created_by": agent_identity,
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    if tags:
        # Coerce to plain str so a non-str element can't smuggle a YAML tag.
        fm["tags"] = [str(t) for t in tags]
    dumped = yaml.safe_dump(
        fm, sort_keys=False, default_flow_style=False, allow_unicode=True
    )
    return "---\n" + dumped + "---\n\n" + text + "\n"


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
    serializer: WriteSerializer | None = None,
    idx: Index | None = None,
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
    canonical = normalize_target_path(target_path)
    if canonical is None or not is_writable_path(canonical):
        _emit_audit(audit_log, **{**audit_base, "status": "rejected_path_not_indexable"})
        return ProposeResponse(status="rejected_path_not_indexable",
                               reason="traversal_or_excluded",
                               target_path=target_path)
    # From here on operate ONLY on the canonical path: classification, blocklist,
    # the pending record, the filesystem join, and ``git add`` must all agree with
    # the value that passed validation (item 4). Using the raw string below would
    # let ``decisions\\x.md`` validate as ``decisions/x.md`` yet write a literal
    # ``decisions\\x.md`` outside every indexed prefix.
    target_path = canonical
    audit_base["target_path"] = target_path
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

    # Serialized commit + CAS + validation + enqueue (items 1, 3, 4, 8).
    try:
        sha, push_state = _commit_in_worktree(
            worktrees=worktrees, push_queue=push_queue, pending=pending,
            serializer=serializer or _DEFAULT_SERIALIZER, idx=idx,
            source_session=source_session, agent_identity=agent_identity,
            target_path=target_path, postimage=postimage,
            proposal_type="edit", subject=f"edit: {target_path}",
            target_tier=target_tier, confidence=confidence,
            operator_confirmed=False,
            base_commit=base_commit, base_blob_sha=base_blob_sha,
            target_file_hash=target_file_hash,
            push_meta={"source_session": source_session,
                       "agent_identity": agent_identity, "reason": reason},
        )
    except _WriteRejected as rej:
        resp = rej.response
        _emit_audit(audit_log, **{**audit_base, "status": resp.status,
                                   "reason": resp.reason or reason})
        return resp
    except PathLockBusyError:
        _emit_audit(audit_log, **{**audit_base, "status": "rejected_path_lock_busy"})
        return ProposeResponse(status="rejected_path_lock_busy",
                               target_path=target_path)
    _emit_audit(audit_log, **{**audit_base, "status": "committed", "commit_sha": sha})
    return ProposeResponse(status="committed", commit_sha=sha, push_state=push_state)


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
    max_postimage_bytes: int = 0,
    serializer: WriteSerializer | None = None,
    idx: Index | None = None,
) -> ResolvePendingResponse:
    from data_olympus.pending import PendingAlreadyResolvedError

    audit_base: dict[str, Any] = {
        "event_type": "resolve",
        "agent_identity": agent_identity,
        "source_session": source_session,
        "pending_id": pending_id,
    }
    if decision == "reject":
        try:
            pending.reject(pending_id)
        except PendingAlreadyResolvedError:
            # Lost the double-resolve race (item 5): another resolver already
            # decided this id. Surface a distinct status so the loser does not
            # report a second decision.
            _emit_audit(audit_log, **{**audit_base, "status": "already_resolved"})
            return ResolvePendingResponse(status="already_resolved")
        _emit_audit(audit_log, **{**audit_base, "status": "rejected"})
        return ResolvePendingResponse(status="rejected")
    if decision not in ("approve", "edit"):
        _emit_audit(audit_log, **{**audit_base, "status": "rejected_bad_decision"})
        return ResolvePendingResponse(status="rejected_bad_decision")

    # ``edited_text`` becomes the committed postimage, bypassing the cap the
    # propose path enforced on the original postimage (item 2). Enforce it here
    # too, with a distinct status so the operator sees WHY the edit was refused.
    # Checked BEFORE the atomic claim so a too-large edit leaves the pending entry
    # in place to be re-edited rather than silently dropped.
    if edited_text is not None and max_postimage_bytes > 0 and (
        len(edited_text.encode("utf-8")) > max_postimage_bytes
    ):
        _emit_audit(audit_log,
                    **{**audit_base, "status": "rejected_edited_text_too_large"})
        return ResolvePendingResponse(status="rejected_edited_text_too_large")

    # Atomic claim that HOLDS the path lock + the claimed entry through the gates
    # (Codex round-2 Blocker B): a post-claim CAS/validation rejection puts the
    # entry back (restore_resolve) instead of losing the operator's proposal, and
    # no other write can grab the path in the claim->commit window. Exactly one
    # concurrent resolve of this id proceeds (item 5).
    try:
        resolved = pending.claim_for_resolve(pending_id, edited_text=edited_text)
    except PendingAlreadyResolvedError:
        _emit_audit(audit_log, **{**audit_base, "status": "already_resolved"})
        return ResolvePendingResponse(status="already_resolved")

    target_tier, _ = _classify(resolved.target_path)
    audit_base["target_path"] = resolved.target_path
    audit_base["target_tier"] = target_tier
    try:
        audit_base["confidence"] = float(resolved.meta.get("confidence", 0.0))
    except (TypeError, ValueError):
        audit_base["confidence"] = None

    # Serialized commit + CAS (from the pending entry's base markers) + validation
    # + reset-on-late-failure, shared with the auto-commit path (items 1, 3, 4, 8).
    # hold_path_lock=True: the lock is already held from enqueue via the claim, so
    # _commit_in_worktree must not re-acquire it (would deadlock / raise busy).
    try:
        sha, _push_state = _commit_in_worktree(
            worktrees=worktrees, push_queue=push_queue, pending=pending,
            serializer=serializer or _DEFAULT_SERIALIZER, idx=idx,
            source_session=resolved.meta.get("source_session", source_session),
            agent_identity=resolved.meta.get("agent_identity", agent_identity),
            target_path=resolved.target_path, postimage=resolved.postimage,
            proposal_type=resolved.proposal_type,
            subject=f"resolve: {resolved.target_path}",
            target_tier=target_tier,
            confidence=float(resolved.meta.get("confidence", 0.0)),
            operator_confirmed=True,
            base_commit=resolved.base_commit,
            base_blob_sha=resolved.base_blob_sha,
            target_file_hash=resolved.target_file_hash,
            push_meta={},
            lock_owner=f"resolve:{pending_id}",
            hold_path_lock=True,
        )
    except _WriteRejected as rej:
        # Gate rejected AFTER the claim: put the entry back so the operator can
        # re-resolve it (the postimage is not lost). The path lock stays held.
        pending.restore_resolve(pending_id)
        resp = rej.response
        _emit_audit(audit_log, **{**audit_base, "status": resp.status,
                                   "reason": resp.reason})
        return ResolvePendingResponse(status=resp.status, reason=resp.reason)
    except Exception:
        # A git/enqueue failure after the claim: restore the entry rather than
        # silently consuming it, so the write is recoverable.
        with contextlib.suppress(Exception):
            pending.restore_resolve(pending_id)
        raise
    # Commit + enqueue succeeded: now consume the entry and release the lock.
    pending.finalize_resolve(pending_id, resolved.target_path)
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
