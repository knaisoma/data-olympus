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
    path_rejection_reason,
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
    SecretMatch,
    WriteSerializer,
    check_cas,
    reset_worktree,
    scan_postimage_for_secrets,
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

# Placeholder used wherever a caller-supplied target_path matched a secret
# pattern (issue #71, codex round-3 Blocker 2): the path is echoed in
# responses, audit events, commit subjects/trailers, and would be committed
# as a git path, so a credential-shaped FILENAME is itself a leak and must
# never be repeated back anywhere once flagged.
_REDACTED_PATH = "[target_path redacted: matched a secret pattern]"


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
    normalized in). Delegates to format.validate.memory_inbox_prefix, the
    single source shared with index.py's memory-inbox in-force floor (issue
    #109) -- this wrapper is kept only so existing callers/tests importing
    ``tools_write._memory_inbox_prefix`` are unaffected.
    """
    from data_olympus.format.validate import memory_inbox_prefix
    return memory_inbox_prefix()


# ``evidence`` validation (issue #109): optional supporting-context list on
# kb_propose_memory / kb_propose_edit. Fixed limits (not config-driven, unlike
# max_text_bytes/max_postimage_bytes): a small, cheap-to-review list of short
# strings, not a payload-sizing concern.
_MAX_EVIDENCE_ITEMS = 10
_MAX_EVIDENCE_ITEM_CHARS = 500


def _validate_evidence(evidence: list[str]) -> str | None:
    """Return a rejection reason string if ``evidence`` is invalid, else None.

    Rejects (rather than truncating/coercing) so a caller sees exactly why its
    proposal did not go through, instead of a silently-shortened evidence list.
    """
    if len(evidence) > _MAX_EVIDENCE_ITEMS:
        return (
            f"evidence exceeds max {_MAX_EVIDENCE_ITEMS} items "
            f"(got {len(evidence)})"
        )
    for item in evidence:
        if not isinstance(item, str):
            return "evidence items must be strings"
        if len(item) > _MAX_EVIDENCE_ITEM_CHARS:
            return (
                f"evidence item exceeds max {_MAX_EVIDENCE_ITEM_CHARS} chars "
                f"(got {len(item)})"
            )
    return None


def _redact_evidence(evidence: list[str]) -> list[str]:
    """Scan each evidence item for a secret pattern, replacing a flagged item
    with a redacted placeholder (pattern name only) before it is persisted to
    pending meta or echoed in an audit event -- the same treatment `tags`
    already gets in kb_propose_memory_fn. For a memory proposal the RAW
    evidence still reaches the committed/reviewed postimage via `_render_memory`
    (needed for operator review, same rationale as tags in the body); only this
    separate meta/audit copy is redacted."""
    out: list[str] = []
    for item in evidence:
        scan = scan_postimage_for_secrets(postimage=str(item))
        if scan.match is not None:
            out.append(
                f"[evidence redacted: secret pattern '{scan.match.pattern_name}' "
                f"detected]"
            )
        else:
            out.append(str(item))
    return out


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
    matching_pattern: str | None = None,
    secret_scan_override: bool | None = None,
    evidence: list[str] | None = None,
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
            "matching_pattern": matching_pattern,
            "secret_scan_override": secret_scan_override,
            "evidence": evidence,
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
    secret_scan_override: bool = False,
) -> tuple[str, str, SecretMatch | None]:
    """Serialized write -> git add -> commit -> enqueue critical section.

    Shared by the auto-commit (propose) path and the operator-resolve path so both
    honor identical integrity gates (scope items 1, 3, 4, 8, and the issue #71
    secret-scanning gate). Returns ``(commit_sha, push_state, secret_override)``
    where push_state is ``"queued"`` or ``"enqueue_failed_recovery_pending"``
    (see _enqueue_after_commit), and ``secret_override`` is the
    :class:`~data_olympus.write_gate.SecretMatch` the scanner found IF
    ``secret_scan_override`` was True and a pattern actually matched (else
    ``None``) -- so the resolve caller can audit that an override was
    exercised, without this shared helper needing to know about audit at all.
    Raises :class:`_WriteRejected` (carrying the ProposeResponse) when a gate
    rejects, or :class:`PathLockBusyError` when the per-path advisory lock is
    held.

    ``secret_scan_override`` is ONLY ever passed True by the operator
    resolve path (:func:`kb_resolve_pending_fn`). Neither
    :func:`kb_propose_memory_fn` nor :func:`kb_propose_edit_fn` expose a
    parameter that can reach it, so an agent can never self-authorize past a
    flagged auto-commit -- only a human resolving a pending entry can.

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
            # 1. Refresh the session branch base onto origin/main so CAS compares
            # against, and the commit sits on, current content.
            #
            # A refresh failure (network down, or a rebase conflict) is handled by
            # whether the caller opted into CAS: when an ENFORCEABLE base marker was
            # supplied, we CANNOT verify it against a stale base, so a failed
            # refresh must reject rejected_stale_base rather than commit a possibly
            # stale write against an unrefreshed tree (Codex round-5: the push
            # path's rebase recovery is NOT equivalent, since a compatible rebase
            # would still publish the stale write). When no marker was supplied CAS
            # is a no-op, so a refresh failure stays non-fatal and the commit sits
            # on the unrefreshed base (the push path's non-FF recovery publishes).
            from data_olympus.write_gate import _is_enforceable_base_commit
            cas_enforceable = bool(base_blob_sha) or bool(target_file_hash) or \
                _is_enforceable_base_commit(base_commit)
            refresh_ok = True
            try:
                worktrees.git.refresh_base(wt.path)
            except Exception as exc:  # noqa: BLE001 - classified by cas_enforceable
                refresh_ok = False
                refresh_err = str(exc)
            if cas_enforceable and not refresh_ok:
                raise _WriteRejected(ProposeResponse(
                    status="rejected_stale_base", target_path=target_path,
                    reason=(f"base could not be refreshed onto origin/main, so the "
                            f"supplied base marker cannot be verified: "
                            f"{refresh_err}")))

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

            # 5. Secret-scanning gate (issue #71): reject a postimage carrying
            # credential-shaped content BEFORE it is committed. Only the
            # pattern name + an approximate line number are ever surfaced (see
            # write_gate.scan_postimage_for_secrets); the matched value itself
            # never reaches the response, a log line, or (below) the audit
            # event. ``secret_scan_override`` lets the operator resolve path
            # consciously commit anyway; it is never available here from the
            # auto-commit paths (see the docstring above).
            #
            # This gate runs BEFORE content-validation (step 5a below) on
            # purpose: ``validate_postimage`` echoes the offending value
            # verbatim in an ``invalid_enum`` rejection message (e.g. a
            # postimage with ``status: AKIA...`` in the frontmatter), so if a
            # postimage is BOTH malformed AND carries a credential-shaped
            # value, checking validation first would leak the secret through
            # that message. Scanning first means such a postimage is always
            # rejected as the redacted ``rejected_secret_detected``, never as
            # ``rejected_invalid_document``.
            secret_override: SecretMatch | None = None
            # 5-pre. Defense-in-depth path scan (codex round-3 Blocker 2):
            # the propose paths already reject a credential-shaped
            # target_path before it reaches here, but the RESOLVE path
            # commits pending entries that may predate that check (or were
            # demoted from a push conflict), and the path lands in the commit
            # subject, trailers, and the git tree. The rejection deliberately
            # does NOT echo the path. The operator resolve override applies
            # here exactly as it does to a flagged postimage.
            path_scan = scan_postimage_for_secrets(postimage=target_path)
            if not path_scan.ok:
                assert path_scan.match is not None
                if secret_scan_override:
                    secret_override = path_scan.match
                else:
                    raise _WriteRejected(ProposeResponse(
                        status="rejected_secret_detected",
                        reason=(
                            f"secret pattern '{path_scan.match.pattern_name}' "
                            f"detected in target_path"
                        ),
                        matching_pattern=path_scan.match.pattern_name,
                    ))
            secret_result = scan_postimage_for_secrets(postimage=postimage)
            if not secret_result.ok:
                assert secret_result.match is not None
                if secret_scan_override:
                    secret_override = secret_override or secret_result.match
                else:
                    raise _WriteRejected(ProposeResponse(
                        status="rejected_secret_detected", target_path=target_path,
                        reason=(
                            f"secret pattern '{secret_result.match.pattern_name}' "
                            f"detected near line {secret_result.match.line}"
                        ),
                        matching_pattern=secret_result.match.pattern_name,
                    ))

            # 5a. Content-validation gate (item 4). Pass the worktree so the
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
            return sha, push_state, secret_override


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

        # Containment + secret-scan + validation for every file BEFORE any
        # write. The secret scan runs BEFORE content-validation (same
        # ordering rationale as ``_commit_in_worktree``): ``validate_postimage``
        # echoes an invalid enum value verbatim, so checking it first could
        # leak a credential-shaped value through that message instead of the
        # redacted ``rejected_secret_detected`` path. Bootstrap has no
        # operator override at all (it always commits atomically with no
        # human in the loop), so a single flagged file rejects the whole
        # bundle before any file in it is written.
        for f in files:
            tp, pi = f["target_path"], f["postimage"]
            full = safe_join_under_root(wt.path, tp)
            if full is None:
                raise _WriteRejected(ProposeResponse(
                    status="rejected_symlink_escape", target_path=tp))
            # Path scan first (codex round-3 Blocker 2): a credential-shaped
            # filename would land in the commit and be echoed in
            # responses/audit; the rejection must not echo it either.
            path_scan = scan_postimage_for_secrets(postimage=tp)
            if not path_scan.ok:
                assert path_scan.match is not None
                raise _WriteRejected(ProposeResponse(
                    status="rejected_secret_detected",
                    target_path=_REDACTED_PATH,
                    reason=(
                        f"secret pattern '{path_scan.match.pattern_name}' "
                        f"detected in a bundle target_path"
                    ),
                    matching_pattern=path_scan.match.pattern_name,
                ))
            secret_result = scan_postimage_for_secrets(postimage=pi)
            if not secret_result.ok:
                assert secret_result.match is not None
                raise _WriteRejected(ProposeResponse(
                    status="rejected_secret_detected", target_path=tp,
                    reason=(
                        f"secret pattern '{secret_result.match.pattern_name}' "
                        f"detected near line {secret_result.match.line}"
                    ),
                    matching_pattern=secret_result.match.pattern_name,
                ))
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
    evidence: list[str] | None = None,
) -> ProposeResponse:
    """Propose a new memory file under the memory inbox prefix as
    <date>-<slug>-<uniq>.md.

    Structural rule is satisfied by construction; blocklist still applies.
    High confidence -> auto-commit + push enqueue. Low -> pending queue.

    ``can_auto_commit`` is the caller's authorization to skip operator review:
    when False (an authenticated principal lacking the auto_commit capability, or
    an untrusted caller) the proposal is parked as pending regardless of the
    client-asserted confidence. This is the confidence clamp.

    ``evidence`` (issue #109, optional): supporting-context strings validated
    by ``_validate_evidence`` (max 10 items, 500 chars each), rendered into the
    memory's frontmatter (so a credential-shaped item is caught by the SAME
    full-postimage secret scan below -- see ``_render_memory``), and persisted
    (redacted copy) in pending meta / audit events / ``kb_pending``.
    """
    evidence = evidence or []
    evidence_error = _validate_evidence(evidence)
    today = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    # issue #71: the slug is derived VERBATIM from the caller's free text (no
    # redaction -- `_slugify` only lowercases and normalizes separators), so a
    # credential-shaped substring in ``text`` would otherwise survive intact
    # in the committed filename / the pending entry's target_path / every
    # response and audit event that echoes target_path (all of which are
    # normally safe to log). Scan the raw text FIRST and fall back to a
    # neutral slug when flagged, so no fragment of a real secret ever reaches
    # the filename. ``uniq`` (a session+text hash) still disambiguates same-day
    # flagged proposals; the authoritative scan/reject decision below still
    # runs against the full rendered postimage.
    text_secret_result = scan_postimage_for_secrets(postimage=text)
    slug = _slugify(text) if text_secret_result.ok else "flagged"
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

    # 0. Evidence validation (issue #109): a cheap client-input check, run
    # before any path/blocklist/rate-limit work.
    if evidence_error is not None:
        _emit_audit(audit_log, **{**audit_base, "status": "rejected_invalid_evidence",
                                   "reason": evidence_error})
        return ProposeResponse(status="rejected_invalid_evidence",
                               reason=evidence_error, target_path=target_path)

    # 1. Structural rule (cheap).
    if not is_writable_path(target_path):
        reason = path_rejection_reason(target_path)
        _emit_audit(audit_log, **{**audit_base, "status": "rejected_path_not_indexable",
                                   "reason": reason})
        return ProposeResponse(
            status="rejected_path_not_indexable",
            reason=reason,
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
    postimage = _render_memory(
        text=text, tags=tags, agent_identity=agent_identity, evidence=evidence,
    )

    # 4b. Payload size cap (reject before any disk side effect).
    if max_text_bytes > 0 and len(postimage.encode("utf-8")) > max_text_bytes:
        _emit_audit(audit_log, **{**audit_base, "status": "rejected_payload_too_large"})
        return ProposeResponse(status="rejected_payload_too_large",
                               target_path=target_path)

    if confidence < confidence_threshold or not can_auto_commit:
        # Scan BEFORE enqueueing (issue #71): a low-confidence proposal is
        # never rejected here (that would defeat the operator-override
        # workflow at resolve time -- see kb_resolve_pending_fn), but a
        # flagged proposal's raw text must not be echoed back in THIS
        # response, only the pattern name. The pending entry itself still
        # carries the full postimage on disk (unavoidable: the operator needs
        # something to review/edit/approve), tagged with
        # ``secret_scan_flagged``/``matching_pattern`` metadata (names only,
        # never the matched value) so `kb pending` surfaces the warning
        # without exposing the secret.
        secret_result = scan_postimage_for_secrets(postimage=postimage)
        flagged_pattern = (
            secret_result.match.pattern_name if secret_result.match is not None else None
        )
        # Pending META must never carry a raw credential value (codex round-3
        # Blocker 1): the postimage field is the single reviewed artifact and
        # unavoidably holds the flagged content, but meta is treated as
        # loggable, so a tag that itself matches a secret pattern is stored
        # redacted (pattern name only).
        safe_tags: list[str] = []
        for t in tags:
            tag_scan = scan_postimage_for_secrets(postimage=str(t))
            if tag_scan.match is not None:
                safe_tags.append(
                    f"[tag redacted: secret pattern "
                    f"'{tag_scan.match.pattern_name}' detected]"
                )
            else:
                safe_tags.append(str(t))
        # Evidence (issue #109): same redaction treatment as tags above, so a
        # secret-shaped evidence item never reaches pending meta / audit / the
        # kb_pending response in the clear.
        safe_evidence = _redact_evidence(evidence)
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
                    "tags": safe_tags,
                    "secret_scan_flagged": flagged_pattern is not None,
                    "matching_pattern": flagged_pattern,
                    "evidence": safe_evidence or None,
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
                                   "pending_id": pid, "matching_pattern": flagged_pattern,
                                   "evidence": safe_evidence or None})
        if flagged_pattern is not None:
            return ProposeResponse(
                status="pending_confirmation",
                pending_id=pid,
                matching_pattern=flagged_pattern,
                operator_prompt=(
                    # target_path is deliberately OMITTED here: for a memory
                    # proposal it is slugified straight from the (possibly
                    # secret-bearing) text, so it can itself embed the leaked
                    # value verbatim; pending_id is enough to look the entry
                    # up via `kb pending`.
                    f"Proposed memory (pending_id={pid}) was FLAGGED by the "
                    f"secret scanner (pattern: {flagged_pattern}). Review it "
                    f"via `kb pending` / `kb resolve {pid} --decision reject`, or "
                    f"`kb resolve {pid} --decision approve --override-secret-scan` "
                    f"only if this is a confirmed false positive."
                ),
            )
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
        sha, push_state, _secret_override = _commit_in_worktree(
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
                                   "reason": resp.reason,
                                   "matching_pattern": resp.matching_pattern})
        return resp
    except PathLockBusyError:
        _emit_audit(audit_log, **{**audit_base, "status": "rejected_path_lock_busy"})
        return ProposeResponse(status="rejected_path_lock_busy",
                               target_path=target_path)
    _emit_audit(audit_log, **{**audit_base, "status": "committed", "commit_sha": sha,
                               "evidence": _redact_evidence(evidence) or None})
    return ProposeResponse(status="committed", commit_sha=sha, push_state=push_state)


def _render_memory(
    *,
    text: str,
    tags: list[str],
    agent_identity: str,
    evidence: list[str] | None = None,
) -> str:
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

    ``type: memory`` / ``status: proposed`` (issue #109, memory stamping): every
    server-rendered memory is stamped with the concept schema's real vocabulary
    (SPEC.md 4.2), so the EXISTING status filter (``in_force``), the status
    rerank, and the compact deviating-status emission all apply to it with zero
    new code paths -- an agent-written memory is retrievable but never
    presented as a governing rule (``kb_consult`` hard-filters to in-force)
    until an operator promotes it out of ``proposed`` at review time. Promotion
    itself is out of scope here.

    ``evidence`` (optional, issue #109) is rendered into frontmatter the same
    safe_dump way as ``tags``: a list of caller-supplied supporting strings the
    proposer already validated (see ``_validate_evidence``). Because it lands in
    the SAME postimage this function returns, it passes through the existing
    full-postimage secret scan the propose path already runs -- no separate
    scan is needed here.
    """
    import yaml

    fm: dict[str, Any] = {
        "type": "memory",
        "status": "proposed",
        "created_by": agent_identity,
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    if tags:
        # Coerce to plain str so a non-str element can't smuggle a YAML tag.
        fm["tags"] = [str(t) for t in tags]
    if evidence:
        fm["evidence"] = [str(e) for e in evidence]
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
    evidence: list[str] | None = None,
) -> ProposeResponse:
    """Propose an edit to an existing (or new) file under target_path.

    ``can_auto_commit=False`` clamps the proposal to pending regardless of
    confidence (see kb_propose_memory_fn for the rationale).

    ``evidence`` (issue #109, optional): supporting-context strings validated
    by ``_validate_evidence`` (max 10 items, 500 chars each). Unlike
    kb_propose_memory_fn, ``postimage`` here is caller-supplied verbatim (no
    server-rendered template), so evidence is never injected into committed
    content -- it is redacted (same treatment as ``reason``) and persisted only
    in pending meta / audit events / ``kb_pending``.
    """
    evidence = evidence or []
    evidence_error = _validate_evidence(evidence)
    # issue #71 (codex round-3 Blocker 2): scan the caller-supplied path
    # BEFORE anything echoes it. A credential-shaped filename would otherwise
    # surface in this function's responses and audit events, in the commit
    # subject/trailers, and as a committed git path, even with a clean
    # postimage. The rejection deliberately does NOT echo the path.
    path_scan = scan_postimage_for_secrets(postimage=target_path)
    if not path_scan.ok:
        assert path_scan.match is not None
        path_reason = (f"secret pattern '{path_scan.match.pattern_name}' "
                       f"detected in target_path")
        _emit_audit(audit_log, event_type="propose_edit",
                    status="rejected_secret_detected",
                    agent_identity=agent_identity,
                    source_session=source_session,
                    target_path=_REDACTED_PATH, confidence=confidence,
                    remote_addr=remote_addr, reason=path_reason,
                    matching_pattern=path_scan.match.pattern_name)
        return ProposeResponse(
            status="rejected_secret_detected",
            reason=path_reason,
            matching_pattern=path_scan.match.pattern_name,
        )

    # issue #71 (codex round-3 Blocker 1): ``reason`` is persisted into
    # pending meta, push metadata, and audit events, none of which may carry
    # a raw credential value. A flagged reason is REPLACED with a redacted
    # note rather than rejecting the write: reason is advisory metadata, not
    # committed content, so redaction preserves the operation while keeping
    # every downstream surface clean.
    reason_scan = scan_postimage_for_secrets(postimage=reason)
    if not reason_scan.ok:
        assert reason_scan.match is not None
        reason = (f"[reason redacted: secret pattern "
                  f"'{reason_scan.match.pattern_name}' detected]")

    audit_base: dict[str, Any] = {
        "event_type": "propose_edit",
        "agent_identity": agent_identity,
        "source_session": source_session,
        "target_path": target_path,
        "confidence": confidence,
        "reason": reason,
        "remote_addr": remote_addr,
    }

    # 0. Evidence validation (issue #109): a cheap client-input check, run
    # before any path/blocklist/rate-limit work.
    if evidence_error is not None:
        _emit_audit(audit_log, **{**audit_base, "status": "rejected_invalid_evidence",
                                   "reason": evidence_error})
        return ProposeResponse(status="rejected_invalid_evidence",
                               reason=evidence_error, target_path=target_path)

    # Redacted copy for pending meta / audit events (never the raw value if a
    # scan flagged an item -- postimage here is caller-supplied verbatim, so
    # unlike kb_propose_memory_fn there is no template to render evidence into).
    safe_evidence = _redact_evidence(evidence)

    canonical = normalize_target_path(target_path)
    if canonical is None or not is_writable_path(canonical):
        # audit_base["reason"] otherwise carries the caller's edit rationale;
        # for this terminal rejection (nothing is written) it is overwritten
        # with the machine rejection cause instead, since that is what an
        # operator reading the audit log needs to see here.
        rejection_reason = path_rejection_reason(target_path)
        _emit_audit(audit_log, **{**audit_base, "status": "rejected_path_not_indexable",
                                   "reason": rejection_reason})
        return ProposeResponse(status="rejected_path_not_indexable",
                               reason=rejection_reason,
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
        # Scan BEFORE enqueueing (issue #71): see the matching comment in
        # kb_propose_memory_fn for the rationale (never reject here -- that
        # would remove the operator-override path at resolve time -- but
        # never echo the raw postimage back in THIS response when flagged).
        secret_result = scan_postimage_for_secrets(postimage=postimage)
        flagged_pattern = (
            secret_result.match.pattern_name if secret_result.match is not None else None
        )
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
                      "reason": reason,
                      "secret_scan_flagged": flagged_pattern is not None,
                      "matching_pattern": flagged_pattern,
                      "evidence": safe_evidence or None},
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
                                   "pending_id": pid, "matching_pattern": flagged_pattern,
                                   "evidence": safe_evidence or None})
        if flagged_pattern is not None:
            return ProposeResponse(
                status="pending_confirmation",
                pending_id=pid,
                matching_pattern=flagged_pattern,
                operator_prompt=(
                    f"Proposed edit to {target_path} was FLAGGED by the secret "
                    f"scanner (pattern: {flagged_pattern}). Review it via "
                    f"`kb pending` / `kb resolve {pid} --decision reject`, or "
                    f"`kb resolve {pid} --decision approve --override-secret-scan` "
                    f"only if this is a confirmed false positive."
                ),
            )
        return ProposeResponse(
            status="pending_confirmation",
            pending_id=pid,
            proposal_text=postimage,
            operator_prompt=f"Proposed edit to {target_path}. Accept (y), edit, or reject (n)?",
        )

    # Serialized commit + CAS + validation + enqueue (items 1, 3, 4, 8).
    try:
        sha, push_state, _secret_override = _commit_in_worktree(
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
                                   "reason": resp.reason or reason,
                                   "matching_pattern": resp.matching_pattern})
        return resp
    except PathLockBusyError:
        _emit_audit(audit_log, **{**audit_base, "status": "rejected_path_lock_busy"})
        return ProposeResponse(status="rejected_path_lock_busy",
                               target_path=target_path)
    _emit_audit(audit_log, **{**audit_base, "status": "committed", "commit_sha": sha,
                               "evidence": safe_evidence or None})
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
    override_secret_scan: bool = False,
) -> ResolvePendingResponse:
    """Resolve a pending proposal. ``override_secret_scan`` (default False) is
    the operator's explicit, conscious override of the issue #71 secret-
    scanning gate: when True and the resolved postimage (the original or, if
    supplied, ``edited_text``) matches a credential pattern, the commit
    proceeds anyway instead of being rejected ``rejected_secret_detected``, and
    the resulting audit event records that the override was used. This
    parameter exists ONLY on the operator resolve path -- neither
    ``kb_propose_memory_fn`` nor ``kb_propose_edit_fn`` accept it, so an agent
    can never self-authorize past a flagged auto-commit."""
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
    # issue #71: the resolved entry's target_path may itself be
    # credential-shaped (a legacy entry predating the propose-time path gate),
    # and audit_base flows into EVERY audit event this function emits,
    # including the rejection the commit helper is about to raise for exactly
    # that path. Redact it here so no audit event ever carries the raw value;
    # the commit helper re-scans the real path for the reject/override
    # decision itself.
    resolved_path_scan = scan_postimage_for_secrets(postimage=resolved.target_path)
    audit_base["target_path"] = (
        resolved.target_path if resolved_path_scan.ok else _REDACTED_PATH
    )
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
        sha, push_state, secret_override = _commit_in_worktree(
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
            secret_scan_override=override_secret_scan,
        )
    except _WriteRejected as rej:
        # Gate rejected AFTER the claim: put the entry back so the operator can
        # re-resolve it (the postimage is not lost). The path lock stays held.
        pending.restore_resolve(pending_id)
        resp = rej.response
        _emit_audit(audit_log, **{**audit_base, "status": resp.status,
                                   "reason": resp.reason,
                                   "matching_pattern": resp.matching_pattern})
        return ResolvePendingResponse(status=resp.status, reason=resp.reason)
    except Exception:
        # A git/enqueue failure after the claim: restore the entry rather than
        # silently consuming it, so the write is recoverable.
        with contextlib.suppress(Exception):
            pending.restore_resolve(pending_id)
        raise
    # Commit succeeded: consume the entry and release the lock. The commit is
    # durable regardless of push_state (which is surfaced truthfully below): even
    # an enqueue_failed_recovery_pending commit exists on the branch and is
    # republished by in-process/startup recovery, so re-resolving would duplicate
    # it -- the entry must be consumed, not restored (Codex round-4).
    pending.finalize_resolve(pending_id, resolved.target_path)
    # secret_override is only non-None when the operator explicitly passed
    # override_secret_scan=True AND the scanner actually flagged something, so
    # the audit trail truthfully distinguishes "override requested but nothing
    # to override" from "a flagged write was consciously approved anyway".
    audit_extra: dict[str, Any] = {}
    if secret_override is not None:
        audit_extra["secret_scan_override"] = True
        audit_extra["matching_pattern"] = secret_override.pattern_name
    _emit_audit(audit_log, **{**audit_base, "status": "committed",
                               "commit_sha": sha, **audit_extra})
    return ResolvePendingResponse(status="committed", commit_sha=sha,
                                  push_state=push_state)


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
                secret_scan_flagged=e.get("secret_scan_flagged", False),
                matching_pattern=e.get("matching_pattern"),
                # issue #109: provenance kb_pending already persisted but did
                # not surface until now.
                source_session=e.get("source_session"),
                reason=e.get("reason"),
                evidence=e.get("evidence"),
            )
            for e in pending.list()
        ]
    )
