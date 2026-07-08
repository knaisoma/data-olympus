"""kb_onboarding_status_fn + kb_bootstrap_project_fn."""
from __future__ import annotations

import contextlib
import math
import os
from typing import TYPE_CHECKING

from data_olympus.models import (
    BootstrapResponse,
    CleanupItem,
    CleanupPlanResponse,
    OnboardingStatusResponse,
    RenameCandidateModel,
)
from data_olympus.onboarding import compute_status
from data_olympus.onboarding_inflight import BootstrapInFlight

if TYPE_CHECKING:
    from data_olympus.audit_log import AuditLog
    from data_olympus.auth import PathBlocklist
    from data_olympus.index import Index
    from data_olympus.onboarding import OnboardingStatus
    from data_olympus.pending import PendingQueue
    from data_olympus.push_queue import PushQueue
    from data_olympus.rate_limit import SlidingWindowLimiter
    from data_olympus.worktrees import WorktreeRegistry
    from data_olympus.write_gate import WriteSerializer


def _pending_root(pending: PendingQueue) -> str:
    """The pending queue's on-disk root, via its public ``root`` property.

    (The Wave-1 write-pipeline package added ``PendingQueue.root`` so this no
    longer reaches into the private ``_root``.) Falls back to KB_PENDING_ROOT
    (the same env config.py reads) if the accessor is somehow unavailable, so the
    in-flight guard still lands on the state volume rather than crashing."""
    root = getattr(pending, "root", None)
    if isinstance(root, str):
        return root
    return os.getenv("KB_PENDING_ROOT", "/state/pending")


def kb_onboarding_status_fn(
    *,
    idx: Index,
    workspace: str,
    component: str | None,
    workspace_remote_url: str | None,
    component_remote_url: str | None,
) -> OnboardingStatusResponse:
    """Compute onboarding status for a workspace + optional component."""
    s = compute_status(
        workspace=workspace, component=component,
        workspace_remote_url=workspace_remote_url,
        component_remote_url=component_remote_url,
        idx=idx,
    )
    return OnboardingStatusResponse(
        state=s.state,
        workspace=s.workspace,
        component=s.component,
        missing_files=s.missing_files,
        rename_candidates=[
            RenameCandidateModel(
                target_tier=c.target_tier,
                target_workspace=c.target_workspace,
                target_component=c.target_component,
                confidence=c.confidence,
                matched_via=c.matched_via,
            ) for c in s.rename_candidates
        ],
    )


def kb_bootstrap_project_fn(
    *,
    idx: Index,
    workspace: str,
    component: str | None,
    workspace_remote_url: str | None,
    component_remote_url: str | None,
    files: list[dict[str, str]],
    source_session: str,
    agent_identity: str,
    confidence: float,
    confidence_threshold: float,
    worktrees: WorktreeRegistry,
    push_queue: PushQueue,
    pending: PendingQueue,
    rate_limiter: SlidingWindowLimiter,
    blocklist: PathBlocklist,
    audit_log: AuditLog | None = None,  # noqa: ARG001  reserved for future audit emission
    remote_addr: str = "mcp",
    can_auto_commit: bool = True,
    max_postimage_bytes: int = 0,
    max_files: int = 0,
    in_flight: BootstrapInFlight | None = None,
    serializer: WriteSerializer | None = None,
) -> BootstrapResponse:
    """Bootstrap a new workspace/component. Callable when status is ``absent`` or
    ``partial``.

    On ``absent`` all supplied files are written. On ``partial`` (some but not all
    canonical files present) the request is narrowed to only the ``missing_files``
    the status reports, so an in-progress onboarding can be completed without ever
    overwriting a file already committed (item 1).

    Atomic outcome: ONE commit (high-conf) or ONE pending bundle (low-conf).

    A committed bootstrap is not visible to ``compute_status`` until the push
    queue drains and the index is rebuilt. To stop a second bootstrap
    double-committing during that convergence window, an in-flight marker is
    claimed once the request is admitted and held across a committed outcome
    (item 2); the marker self-expires so a crash cannot wedge the workspace.
    """
    # Server-side re-check that status is absent OR partial. `partial` is a valid
    # entry point: it means the workspace exists in the KB but is missing some
    # canonical file(s), and this bootstrap completes it (item 1).
    s = compute_status(
        workspace=workspace, component=component,
        workspace_remote_url=workspace_remote_url,
        component_remote_url=component_remote_url,
        idx=idx,
    )
    if s.state not in ("absent", "partial"):
        return BootstrapResponse(
            status="rejected_already_onboarded",
            rejected_paths=[],
        )

    # Lazily materialize the in-flight guard on the same durable state volume as
    # the pending queue, unless the caller injected one (tests). Both entry points
    # (MCP tool, REST route) run in one process and share one PendingQueue, so a
    # filesystem marker beside the pending root is a process-wide guard without
    # threading a new argument through the server.py / rest_api.py wiring. The
    # pending root is read via the public ``PendingQueue.root`` property (added by
    # the Wave-1 write-pipeline package) through ``_pending_root``.
    if in_flight is None:
        in_flight = BootstrapInFlight(
            os.path.join(os.path.dirname(_pending_root(pending)), "bootstrap-inflight"),
        )
    # Claim the slot for this workspace/component. A live claim (a bootstrap
    # already in the convergence window) rejects this one as in-progress and
    # closes the double-bootstrap race the absent-recheck cannot (item 2).
    if not in_flight.claim(workspace, component):
        return BootstrapResponse(
            status="rejected_already_in_progress",
            rejected_paths=[f["target_path"] for f in files],
        )
    # From here on, any outcome that did NOT commit must release the claim so a
    # legitimate retry is not blocked for the full TTL. Only a committed outcome
    # holds the claim across the convergence window. A pre-commit exception (git
    # write / commit / push-queue enqueue failure) must also release, otherwise a
    # crash mid-bootstrap would wedge the workspace as `already_in_progress` until
    # the TTL even though nothing committed (codex Concern). The try/finally guards
    # both the raised and the returned non-committed paths.
    committed = False
    try:
        resp = _bootstrap_admitted(
            idx=idx,
            status_obj=s,
            workspace=workspace, component=component,
            workspace_remote_url=workspace_remote_url,
            component_remote_url=component_remote_url,
            files=files, source_session=source_session,
            agent_identity=agent_identity, confidence=confidence,
            confidence_threshold=confidence_threshold,
            worktrees=worktrees, push_queue=push_queue, pending=pending,
            rate_limiter=rate_limiter, blocklist=blocklist,
            remote_addr=remote_addr, can_auto_commit=can_auto_commit,
            max_postimage_bytes=max_postimage_bytes, max_files=max_files,
            serializer=serializer,
        )
        committed = resp.status == "committed"
        return resp
    finally:
        if not committed:
            in_flight.release(workspace, component)


def _bootstrap_admitted(
    *,
    idx: Index,
    status_obj: OnboardingStatus,
    workspace: str,
    component: str | None,
    workspace_remote_url: str | None,
    component_remote_url: str | None,
    files: list[dict[str, str]],
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
    can_auto_commit: bool,
    max_postimage_bytes: int,
    max_files: int,
    serializer: WriteSerializer | None = None,
) -> BootstrapResponse:
    """Body of a bootstrap that passed the state re-check and won the in-flight
    claim. Split out so the outer function owns the claim/release lifecycle and
    this body owns validation, injection, and the commit/pending outcome.

    On ``partial`` state, ``files`` is narrowed to only those whose canonical
    basename is one of ``status_obj.missing_files`` so an existing committed file
    is never overwritten (item 1)."""
    from data_olympus.auth import (
        is_writable_path,
        normalize_target_path,
    )
    from data_olympus.index import _classify_by_path

    # Partial state: keep only the files that fill a reported gap for THIS exact
    # workspace/component. Match on the full canonical path, not the basename: a
    # basename-only check ("AGENTS.md" in missing) would also admit
    # `projects/other/AGENTS.md` or `projects/{ws}/components/{c}/AGENTS.md`,
    # letting the onboarding endpoint overwrite a file in a different project or
    # component (codex Blocker). missing_files holds bare canonical filenames; the
    # allowed set is exactly those filenames under this workspace/component root.
    if status_obj.state == "partial":
        base = (
            f"projects/{workspace}/components/{component}/"
            if component
            else f"projects/{workspace}/"
        )
        allowed = {base + name for name in status_obj.missing_files}
        kept: list[dict[str, str]] = []
        for f in files:
            canonical = normalize_target_path(f["target_path"])
            if canonical in allowed:
                kept.append(f)
        if not kept:
            # No supplied file fills a gap for this exact target (every file is
            # already present, or targets a different project/component): nothing
            # to do here. Report the completed state truthfully rather than
            # committing an empty change or writing outside the intended gap.
            return BootstrapResponse(
                status="rejected_already_onboarded",
                rejected_paths=[f["target_path"] for f in files],
            )
        files = kept

    # Aggregate file-count cap: one request must not enqueue/write an unbounded
    # number of (individually capped) files. Aggregate byte size is bounded by the
    # REST body cap upstream.
    if max_files > 0 and len(files) > max_files:
        return BootstrapResponse(
            status="rejected_too_many_files",
            rejected_paths=[f["target_path"] for f in files],
        )

    # For onboarding v1: simplified atomic-commit path.
    # Validate every file via normalize_target_path + blocklist before any side
    # effects, and REWRITE each file's target_path to the canonical form so that
    # classification, blocklist, pending enqueue, safe_join, and ``git add`` all
    # operate on the same value that passed validation (item 4). Without this a
    # backslash path like ``decisions\\x.md`` validated as ``decisions/x.md`` yet
    # committed a literal root-level backslash file outside every indexed prefix.
    from data_olympus.write_gate import scan_postimage_for_secrets

    rejected: list[str] = []
    canonical_files: list[dict[str, str]] = []
    for f in files:
        canonical = normalize_target_path(f["target_path"])
        if canonical is None or not is_writable_path(canonical):
            rejected.append(f["target_path"])
            continue
        # issue #71 (codex round-3 Blocker 2): a credential-shaped FILENAME
        # is itself a leak (it would be echoed in responses/audit, used in
        # the commit subject, and committed as a git path even with clean
        # postimages), so a flagged path rejects the whole bundle
        # immediately, and the rejection must not echo the path back.
        path_scan = scan_postimage_for_secrets(postimage=canonical)
        if not path_scan.ok:
            assert path_scan.match is not None
            return BootstrapResponse(
                status="rejected_secret_detected",
                rejected_paths=[
                    f"[target_path redacted: secret pattern "
                    f"'{path_scan.match.pattern_name}' detected]"
                ],
            )
        target_tier, _ = _classify_by_path(canonical)
        if blocklist.blocks(canonical, target_tier):
            rejected.append(f["target_path"])
            continue
        canonical_files.append({**f, "target_path": canonical})
    if rejected:
        return BootstrapResponse(
            status="rejected_path_not_indexable_or_blocked",
            rejected_paths=rejected,
        )
    files = canonical_files

    if not rate_limiter.allow(remote_addr=remote_addr, agent_identity=agent_identity):
        return BootstrapResponse(status="rejected_rate_limited")

    # Inject git_remote_url into README/AGENTS front-matter BEFORE the size cap, so
    # the cap counts exactly the bytes that land in git (item 3). Checking the cap
    # before injection let an injected postimage exceed the cap yet still commit.
    if workspace_remote_url:
        files = _inject_remote_url(files, workspace_remote_url, target_filename="README.md")
    if component_remote_url:
        files = _inject_remote_url(files, component_remote_url, target_filename="AGENTS.md")

    # Payload size cap on the FINAL (post-injection) postimage of every file.
    oversized = [
        f["target_path"] for f in files
        if max_postimage_bytes > 0
        and len(f["postimage"].encode("utf-8")) > max_postimage_bytes
    ]
    if oversized:
        return BootstrapResponse(
            status="rejected_payload_too_large",
            rejected_paths=oversized,
        )

    # Governed-lane write protection (issue #112): a bootstrap file only ever
    # targets a NEW or currently-missing canonical path (never an already
    # in-force document -- see the `partial`-state narrowing above), so only
    # rule 1 (status clamp) applies; check_governed_target=False. The bundle
    # is atomic, so ANY file whose postimage sets/changes status into the
    # in-force class demotes the WHOLE bundle to pending review (it is
    # reviewed/approved as a unit, same as the existing low-confidence bundle
    # path). Same secret-scan/content-validation precedence rule as the
    # propose paths: a bundle that would otherwise be REJECTED outright by a
    # hard gate is never silently demoted instead.
    from data_olympus.governed_lane import (
        GovernedLaneVerdict,
        evaluate_governed_lane,
        governed_lane_protection_enabled,
    )
    per_file_verdict: dict[str, GovernedLaneVerdict] = {}
    bundle_demotion_reason: str | None = None
    if governed_lane_protection_enabled():
        for f in files:
            v = evaluate_governed_lane(
                postimage=f["postimage"], target_path=f["target_path"], idx=idx,
                check_governed_target=False,
            )
            per_file_verdict[f["target_path"]] = v
            if v.demoted and bundle_demotion_reason is None:
                bundle_demotion_reason = v.demotion_reason
        would_auto_commit = confidence >= confidence_threshold and can_auto_commit
        if bundle_demotion_reason is not None and would_auto_commit:
            from data_olympus.format.frontmatter import parse_frontmatter
            from data_olympus.write_gate import (
                _effective_doc_id,
                scan_postimage_for_secrets,
                validate_postimage,
            )
            gates_clean = True
            seen_ids: dict[str, str] = {}
            for f in files:
                tp, pi = f["target_path"], f["postimage"]
                if (
                    not scan_postimage_for_secrets(postimage=tp).ok
                    or not scan_postimage_for_secrets(postimage=pi).ok
                    or not validate_postimage(target_path=tp, postimage=pi, idx=idx).ok
                ):
                    gates_clean = False
                    break
                # Intra-bundle duplicate-id check (mirrors
                # commit_multifile_in_worktree's own check): neither file is
                # in the index/tree yet, so the per-file validate_postimage
                # call above cannot catch two bundle files claiming the same
                # effective id.
                try:
                    bfm, _body = parse_frontmatter(pi)
                except ValueError:
                    bfm = {}
                eid = _effective_doc_id(bfm, tp)
                if eid and eid in seen_ids and seen_ids[eid] != tp:
                    gates_clean = False
                    break
                if eid:
                    seen_ids[eid] = tp
            if not gates_clean:
                bundle_demotion_reason = None

    if (
        confidence < confidence_threshold or not can_auto_commit
        or bundle_demotion_reason is not None
    ):
        # Low confidence (or caller not authorized to auto-commit, or a
        # governed-lane demotion): enqueue ALL
        # files as a single pending bundle. The pending queue stores per-file
        # entries; every entry of one bootstrap shares a `bundle_id` in its meta
        # so the operator (and a future bundle-aware resolve UX) can treat them as
        # a unit. Bundle-aware resolve itself is out of scope here; the id is the
        # seam (item 3).
        import uuid

        from data_olympus.pending import PathLockBusyError, PendingQueueFullError
        bundle_id = uuid.uuid4().hex
        # Atomicity: a bootstrap is one bundle. Reject up front if the whole
        # bundle would not fit, so we never leave a partial set of pending entries.
        if pending.would_exceed(len(files)):
            return BootstrapResponse(
                status="rejected_pending_queue_full",
                rejected_paths=[f["target_path"] for f in files],
            )
        pending_ids: list[str] = []

        def _rollback() -> None:
            # Roll back every entry this bundle already enqueued so a failed
            # bootstrap leaves zero orphan pending entries or held path locks
            # (reject() releases the lock and removes the entry).
            for pid in pending_ids:
                with contextlib.suppress(Exception):
                    pending.reject(pid)

        any_flagged = False
        for f in files:
            # Scan BEFORE enqueueing (issue #71), same rationale as the
            # memory/edit propose paths: never reject a low-confidence
            # bootstrap file here (that would remove the operator-override
            # path at resolve time), but tag the pending entry (pattern name
            # only, never the matched value) so `kb pending` warns the
            # operator without exposing the secret.
            secret_result = scan_postimage_for_secrets(postimage=f["postimage"])
            flagged_pattern = (
                secret_result.match.pattern_name if secret_result.match is not None else None
            )
            any_flagged = any_flagged or flagged_pattern is not None
            file_verdict = per_file_verdict.get(f["target_path"])
            file_demotion_reason = (
                file_verdict.demotion_reason if file_verdict is not None else None
            )
            file_injection_matches = (
                file_verdict.injection_matches if file_verdict is not None else ()
            )
            try:
                pid = pending.enqueue(
                    proposal_type="edit",
                    target_path=f["target_path"],
                    postimage=f["postimage"],
                    base_commit="HEAD", base_blob_sha=None, target_file_hash=None,
                    meta={"agent_identity": agent_identity,
                          "source_session": source_session,
                          "confidence": confidence,
                          "bootstrap": True,
                          "bundle_id": bundle_id,
                          "workspace": workspace,
                          "component": component,
                          "secret_scan_flagged": flagged_pattern is not None,
                          "matching_pattern": flagged_pattern,
                          "demotion_reason": (
                              file_demotion_reason or bundle_demotion_reason
                          ),
                          "injection_suspect": bool(file_injection_matches),
                          "injection_patterns": (
                              [f"{m.pattern_name}:{m.line}" for m in file_injection_matches]
                              or None
                          )},
                )
                pending_ids.append(pid)
            except PathLockBusyError:
                # A path in this bundle is already locked by another pending
                # entry, so the bundle cannot be enqueued whole. Treat this like
                # the queue-full race: roll the whole bundle back and reject it,
                # rather than silently returning only the subset that fit (item 3).
                # Partial enqueue is not a valid onboarding outcome.
                _rollback()
                return BootstrapResponse(
                    status="rejected_path_locked",
                    rejected_paths=[f["target_path"] for f in files],
                )
            except PendingQueueFullError:
                # Lost a capacity race after the pre-check: roll back this bundle's
                # enqueued entries so the bootstrap stays all-or-nothing.
                _rollback()
                return BootstrapResponse(
                    status="rejected_pending_queue_full",
                    rejected_paths=[f["target_path"] for f in files],
                )
        flagged_note = (
            " One or more files were FLAGGED by the secret scanner; review "
            "before resolving (see `kb pending` for the matched pattern name)."
            if any_flagged else ""
        )
        demotion_note = (
            f" This bundle was DEMOTED to pending review by governed-lane "
            f"write protection (reason: {bundle_demotion_reason}). Agents can "
            f"propose; only a human can promote this write. Inform the "
            f"operator that it awaits review -- do not attempt to bypass it."
            if bundle_demotion_reason is not None else ""
        )
        return BootstrapResponse(
            status="pending_confirmation",
            pending_id=pending_ids[0] if pending_ids else None,
            demotion_reason=bundle_demotion_reason,
            operator_prompt=(
                f"Bootstrap of {workspace} pending ({len(pending_ids)} files, "
                f"bundle {bundle_id}); run `kb pending` to see entries."
                f"{flagged_note}{demotion_note}"
            ),
        )

    # High confidence: one atomic commit with all files, through the SHARED
    # serialized/validated write path (Codex Blocker 2). This gives bootstrap the
    # same integrity guarantees as memory/edit/resolve: process-wide write lock,
    # per-path advisory locks (shared with the pending queue), the
    # content-validation gate on every postimage, and reset-on-failure so a
    # partial bundle never leaks into the next commit.
    from data_olympus.pending import PathLockBusyError
    from data_olympus.tools_write import (
        _DEFAULT_SERIALIZER,
        _WriteRejected,
        commit_multifile_in_worktree,
    )
    subject = (f"bootstrap: workspace={workspace}, component={component or ''}, "
               f"{len(files)} files")
    tier = "T4" if component else "T3"
    path_for_msg = (f"projects/{workspace}/"
                    + (f"components/{component}/" if component else ""))
    try:
        sha, push_state = commit_multifile_in_worktree(
            worktrees=worktrees, push_queue=push_queue, pending=pending,
            serializer=serializer or _DEFAULT_SERIALIZER, idx=idx,
            source_session=source_session, agent_identity=agent_identity,
            files=files, subject=subject, target_tier=tier,
            target_path_for_msg=path_for_msg, confidence=confidence,
            push_meta={"source_session": source_session,
                       "agent_identity": agent_identity, "bootstrap": True},
        )
    except _WriteRejected as rej:
        resp = rej.response
        return BootstrapResponse(status=resp.status,
                                 rejected_paths=[resp.target_path or ""])
    except PathLockBusyError as busy:
        return BootstrapResponse(status="rejected_path_lock_busy",
                                 rejected_paths=[str(busy)])
    return BootstrapResponse(status="committed", commit_sha=sha,
                             push_state=push_state)


def _inject_remote_url(
    files: list[dict[str, str]], url: str, *, target_filename: str,
) -> list[dict[str, str]]:
    """Ensure the front-matter of the target file contains git_remote_url: <url>.

    The URL is set as a structured YAML value and the frontmatter is re-emitted
    with ``yaml.safe_dump`` (item 3). Previously the raw ``git_remote_url: {url}``
    string was spliced into the document, so a ``url`` containing a newline could
    forge additional top-level frontmatter keys (e.g. ``id`` / ``status``). Safe
    dumping quotes/escapes any structure-breaking value so it survives only as the
    intended scalar.
    """
    import yaml
    out = []
    for f in files:
        if not f["target_path"].endswith("/" + target_filename):
            out.append(f)
            continue
        postimage = f["postimage"]
        if postimage.startswith("---\n"):
            fm_end = postimage.find("\n---\n", 4)
            if fm_end > 0:
                fm_text = postimage[4:fm_end]
                rest = postimage[fm_end + len("\n---\n"):]
                try:
                    fm = yaml.safe_load(fm_text) or {}
                except yaml.YAMLError:
                    fm = None
                if not isinstance(fm, dict):
                    # Unparseable or non-mapping frontmatter: leave the file
                    # untouched rather than clobber caller data with a rebuilt
                    # block (codex round-2 Concern: silent metadata loss). The
                    # injection is skipped, matching the no-closing-fence case.
                    out.append(f)
                    continue
                if fm.get("git_remote_url") == url:
                    out.append(f)
                    continue
                fm["git_remote_url"] = url
                dumped = yaml.safe_dump(
                    fm, sort_keys=False, default_flow_style=False,
                    allow_unicode=True,
                )
                new_postimage = "---\n" + dumped + "---\n" + rest
            else:
                # Malformed frontmatter (no closing fence): leave untouched.
                new_postimage = postimage
        else:
            new_postimage = "---\n" + yaml.safe_dump(
                {"git_remote_url": url}, default_flow_style=False,
                allow_unicode=True,
            ) + "---\n\n" + postimage
        out.append({**f, "postimage": new_postimage})
    return out


_RANK = {"imported_duplicate": 2, "partial_overlap": 1, "unique": 0}


class CleanupInputError(ValueError):
    """Raised by kb_cleanup_plan_fn when local_files / jaccard_threshold input
    fails validation. Both the REST route and the MCP tool catch this and
    surface it as a 400 / rejected_invalid_input response respectively."""


def kb_cleanup_plan_fn(
    *,
    idx: Index,
    workspace: str,
    component: str | None,
    local_files: list[dict[str, str]],
    jaccard_threshold: float = 0.6,
    max_files: int = 0,
    max_content_bytes: int = 0,
) -> CleanupPlanResponse:
    """Read-only: classify each local repo file against the KB content already
    committed for this workspace/component, and render thin-pointer replacements
    for exact duplicates. Server proposes; the agent applies edits locally.

    Validation happens here (not just in the REST route) so that the MCP tool
    path, which calls this function directly, is protected too. Raises
    CleanupInputError on invalid input; callers translate that into their own
    surface's error shape (REST: 400 JSON; MCP: rejected_invalid_input dict).
    """
    if not isinstance(local_files, list):
        raise CleanupInputError("local_files must be a list")
    validated_contents: list[str] = []
    for entry in local_files:
        if not isinstance(entry, dict):
            raise CleanupInputError("each local_files entry must be an object")
        if not isinstance(entry.get("path"), str):
            raise CleanupInputError(
                "each local_files entry must be an object with a string 'path'",
            )
        content = entry.get("content", "")
        if not isinstance(content, str):
            raise CleanupInputError(
                "each local_files entry's 'content' must be a string",
            )
        if max_content_bytes > 0 and len(content.encode("utf-8")) > max_content_bytes:
            raise CleanupInputError(
                f"local_files entry '{entry.get('path')}' content exceeds "
                f"max_content_bytes={max_content_bytes}",
            )
        validated_contents.append(content)

    if max_files > 0 and len(local_files) > max_files:
        raise CleanupInputError(f"too many files (max_files={max_files})")

    try:
        t = float(jaccard_threshold)
    except (TypeError, ValueError) as e:
        raise CleanupInputError("jaccard_threshold must be a number") from e
    if not math.isfinite(t) or not (0.0 <= t <= 1.0):
        raise CleanupInputError("jaccard_threshold must be a finite number in [0.0, 1.0]")
    jaccard_threshold = t

    from data_olympus.dedup import classify_overlap, jaccard, shingles
    from data_olympus.thin_pointer import render_thin_pointer

    prefix = f"projects/{workspace}/"
    if component:
        prefix = f"projects/{workspace}/components/{component}/"
        entries = idx.list_by_prefix(prefix)
    else:
        entries = idx.list_by_prefix(prefix, exclude_under="components/")
    kb_docs = []
    for entry in entries:
        doc = idx.get(entry["id"])
        if doc is not None:
            kb_docs.append(doc)

    kind = "component" if component else "project"
    items: list[CleanupItem] = []
    summary = {"imported_duplicate": 0, "partial_overlap": 0, "unique": 0}

    # Precompute each KB doc's shingles once, outside the per-local-file loop,
    # rather than recomputing them for every local file (O(files * docs) shingle
    # builds otherwise). classify_overlap() still recomputes internally for its
    # own exact-hash + jaccard check; that duplication is harmless and kept
    # simple/behavior-preserving here.
    kb_docs_shingled = [(doc, shingles(doc.content_markdown)) for doc in kb_docs]

    for lf, local_text in zip(local_files, validated_contents, strict=True):
        best_cls, best_headings, best_doc, best_j = "unique", [], None, -1.0
        local_shingles = shingles(local_text)
        for doc, doc_shingles in kb_docs_shingled:
            cls, headings = classify_overlap(
                local_text, doc.content_markdown, jaccard_threshold=jaccard_threshold,
            )
            j = jaccard(local_shingles, doc_shingles)
            if _RANK[cls] > _RANK[best_cls] or (_RANK[cls] == _RANK[best_cls] and j > best_j):
                best_cls, best_headings, best_doc, best_j = cls, headings, doc, j

        item = CleanupItem(local_path=lf.get("path", ""), classification=best_cls)
        if best_doc is not None and best_cls == "imported_duplicate":
            item.kb_id, item.kb_path = best_doc.id, best_doc.path
            item.thin_pointer_text = render_thin_pointer(
                kb_path=best_doc.path, kb_id=best_doc.id, kind=kind,
            )
        elif best_doc is not None and best_cls == "partial_overlap":
            item.kb_id, item.kb_path = best_doc.id, best_doc.path
            item.overlap_headings = best_headings
        summary[best_cls] += 1
        items.append(item)

    return CleanupPlanResponse(
        workspace=workspace, component=component, items=items, summary=summary,
    )
