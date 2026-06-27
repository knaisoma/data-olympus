"""kb_onboarding_status_fn + kb_bootstrap_project_fn."""
from __future__ import annotations

from typing import TYPE_CHECKING

from data_olympus.models import (
    BootstrapResponse,
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

    # For onboarding v1: simplified atomic-commit path.
    # Validate every file via is_writable_path + blocklist before any side effects.
    from data_olympus.auth import is_writable_path
    from data_olympus.index import _classify_by_path
    rejected: list[str] = []
    for f in files:
        if not is_writable_path(f["target_path"]):
            rejected.append(f["target_path"])
            continue
        target_tier, _ = _classify_by_path(f["target_path"])
        if blocklist.blocks(f["target_path"], target_tier):
            rejected.append(f["target_path"])
    if rejected:
        return BootstrapResponse(
            status="rejected_path_not_indexable_or_blocked",
            rejected_paths=rejected,
        )

    if not rate_limiter.allow(remote_addr=remote_addr, agent_identity=agent_identity):
        return BootstrapResponse(status="rejected_rate_limited")

    # Inject git_remote_url into README/AGENTS front-matter if provided.
    if workspace_remote_url:
        files = _inject_remote_url(files, workspace_remote_url, target_filename="README.md")
    if component_remote_url:
        files = _inject_remote_url(files, component_remote_url, target_filename="AGENTS.md")

    if confidence < confidence_threshold:
        # Low confidence: enqueue ALL files as a single pending bundle.
        # For onboarding v1, we enqueue them one-by-one (the pending queue
        # supports per-file entries; resolving "all" is the operator's choice).
        # Future: native bundle support in PendingQueue.
        pending_ids = []
        for f in files:
            from data_olympus.pending import PathLockBusyError
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
