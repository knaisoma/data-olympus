"""kb_onboarding_status_fn + kb_bootstrap_project_fn."""
from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from data_olympus.models import (
    BootstrapResponse,
    CleanupItem,
    CleanupPlanResponse,
    OnboardingStatusResponse,
    RenameCandidateModel,
)
from data_olympus.onboarding import compute_status

if TYPE_CHECKING:
    from data_olympus.audit_log import AuditLog
    from data_olympus.auth import PathBlocklist
    from data_olympus.index import Index
    from data_olympus.pending import PendingQueue
    from data_olympus.push_queue import PushQueue
    from data_olympus.rate_limit import SlidingWindowLimiter
    from data_olympus.worktrees import WorktreeRegistry


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
) -> BootstrapResponse:
    """Bootstrap a new workspace/component. Only callable when status=absent.

    Atomic outcome: ONE commit (high-conf) or ONE pending bundle (low-conf).
    """
    # Server-side re-check that status is absent.
    s = compute_status(
        workspace=workspace, component=component,
        workspace_remote_url=workspace_remote_url,
        component_remote_url=component_remote_url,
        idx=idx,
    )
    if s.state != "absent":
        return BootstrapResponse(
            status="rejected_already_onboarded",
            rejected_paths=[],
        )

    # Aggregate file-count cap: one request must not enqueue/write an unbounded
    # number of (individually capped) files. Aggregate byte size is bounded by the
    # REST body cap upstream.
    if max_files > 0 and len(files) > max_files:
        return BootstrapResponse(
            status="rejected_too_many_files",
            rejected_paths=[f["target_path"] for f in files],
        )

    # For onboarding v1: simplified atomic-commit path.
    # Validate every file via is_writable_path + blocklist before any side effects.
    from data_olympus.auth import is_writable_path
    from data_olympus.index import _classify_by_path
    rejected: list[str] = []
    oversized: list[str] = []
    for f in files:
        if not is_writable_path(f["target_path"]):
            rejected.append(f["target_path"])
            continue
        target_tier, _ = _classify_by_path(f["target_path"])
        if blocklist.blocks(f["target_path"], target_tier):
            rejected.append(f["target_path"])
            continue
        if (max_postimage_bytes > 0
                and len(f["postimage"].encode("utf-8")) > max_postimage_bytes):
            oversized.append(f["target_path"])
    if rejected:
        return BootstrapResponse(
            status="rejected_path_not_indexable_or_blocked",
            rejected_paths=rejected,
        )
    if oversized:
        return BootstrapResponse(
            status="rejected_payload_too_large",
            rejected_paths=oversized,
        )

    if not rate_limiter.allow(remote_addr=remote_addr, agent_identity=agent_identity):
        return BootstrapResponse(status="rejected_rate_limited")

    # Inject git_remote_url into README/AGENTS front-matter if provided.
    if workspace_remote_url:
        files = _inject_remote_url(files, workspace_remote_url, target_filename="README.md")
    if component_remote_url:
        files = _inject_remote_url(files, component_remote_url, target_filename="AGENTS.md")

    if confidence < confidence_threshold or not can_auto_commit:
        # Low confidence (or caller not authorized to auto-commit): enqueue ALL
        # files as a single pending bundle.
        # For onboarding v1, we enqueue them one-by-one (the pending queue
        # supports per-file entries; resolving "all" is the operator's choice).
        # Future: native bundle support in PendingQueue.
        from data_olympus.pending import PathLockBusyError, PendingQueueFullError
        # Atomicity: a bootstrap is one bundle. Reject up front if the whole
        # bundle would not fit, so we never leave a partial set of pending entries.
        if pending.would_exceed(len(files)):
            return BootstrapResponse(
                status="rejected_pending_queue_full",
                rejected_paths=[f["target_path"] for f in files],
            )
        pending_ids = []
        for f in files:
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
                          "workspace": workspace,
                          "component": component},
                )
                pending_ids.append(pid)
            except PathLockBusyError:
                pass  # already pending; skip
            except PendingQueueFullError:
                # Lost a capacity race after the pre-check: roll back this bundle's
                # enqueued entries so the bootstrap stays all-or-nothing.
                for pid in pending_ids:
                    with contextlib.suppress(Exception):
                        pending.reject(pid)
                return BootstrapResponse(
                    status="rejected_pending_queue_full",
                    rejected_paths=[f["target_path"] for f in files],
                )
        return BootstrapResponse(
            status="pending_confirmation",
            pending_id=pending_ids[0] if pending_ids else None,
            operator_prompt=f"Bootstrap of {workspace} pending; run `kb pending` to see entries.",
        )

    # High confidence: one atomic commit with all files.
    import os
    import subprocess

    from data_olympus.audit_trailers import build_commit_message
    from data_olympus.auth import safe_join_under_root
    wt = worktrees.get_or_create(source_session=source_session, agent_identity=agent_identity)
    for f in files:
        # Shared symlink-escape containment guard (see safe_join_under_root).
        full_path = safe_join_under_root(wt.path, f["target_path"])
        if full_path is None:
            return BootstrapResponse(status="rejected_symlink_escape",
                                     rejected_paths=[f["target_path"]])
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as out:
            out.write(f["postimage"])
        subprocess.run(["git", "-C", wt.path, "add", f["target_path"]], check=True)
    subject = (f"bootstrap: workspace={workspace}, component={component or ''}, "
               f"{len(files)} files")
    msg = build_commit_message(
        subject=subject,
        source_session=source_session, agent_identity=agent_identity,
        confidence_original=confidence, operator_confirmed=False,
        proposal_type="edit",
        target_tier="T4" if component else "T3",
        target_path=f"projects/{workspace}/" + (f"components/{component}/" if component else ""),
    )
    subprocess.run(["git", "-C", wt.path, "commit", "-m", msg], check=True)
    sha = subprocess.check_output(["git", "-C", wt.path, "rev-parse", "HEAD"], text=True).strip()
    push_queue.enqueue(sha=sha, worktree_path=wt.path,
                       meta={"source_session": source_session,
                             "agent_identity": agent_identity, "bootstrap": True})
    return BootstrapResponse(status="committed", commit_sha=sha, push_state="queued")


def _inject_remote_url(
    files: list[dict[str, str]], url: str, *, target_filename: str,
) -> list[dict[str, str]]:
    """Ensure the front-matter of the target file contains git_remote_url: <url>."""
    out = []
    for f in files:
        if f["target_path"].endswith("/" + target_filename):
            postimage = f["postimage"]
            if f"git_remote_url: {url}" not in postimage:
                # Insert into front matter if present, else prepend.
                if postimage.startswith("---\n"):
                    # Insert before closing --- on the first front-matter block.
                    fm_end = postimage.find("\n---\n", 4)
                    if fm_end > 0:
                        new_postimage = (
                            postimage[:fm_end] +
                            f"\ngit_remote_url: {url}" +
                            postimage[fm_end:]
                        )
                    else:
                        new_postimage = postimage
                else:
                    new_postimage = (
                        f"---\ngit_remote_url: {url}\n---\n\n" + postimage
                    )
                out.append({**f, "postimage": new_postimage})
                continue
        out.append(f)
    return out


_RANK = {"imported_duplicate": 2, "partial_overlap": 1, "unique": 0}


def kb_cleanup_plan_fn(
    *,
    idx: Index,
    workspace: str,
    component: str | None,
    local_files: list[dict[str, str]],
    jaccard_threshold: float = 0.6,
) -> CleanupPlanResponse:
    """Read-only: classify each local repo file against the KB content already
    committed for this workspace/component, and render thin-pointer replacements
    for exact duplicates. Server proposes; the agent applies edits locally."""
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

    for lf in local_files:
        local_text = lf.get("content", "")
        best_cls, best_headings, best_doc, best_j = "unique", [], None, -1.0
        local_shingles = shingles(local_text)
        for doc in kb_docs:
            cls, headings = classify_overlap(
                local_text, doc.content_markdown, jaccard_threshold=jaccard_threshold,
            )
            j = jaccard(local_shingles, shingles(doc.content_markdown))
            if _RANK[cls] > _RANK[best_cls] or (_RANK[cls] == _RANK[best_cls] and j > best_j):
                best_cls, best_headings, best_doc, best_j = cls, headings, doc, j

        item = CleanupItem(local_path=lf["path"], classification=best_cls)
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
