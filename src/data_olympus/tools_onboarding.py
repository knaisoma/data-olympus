"""kb_onboarding_status_fn + kb_bootstrap_project_fn."""
from __future__ import annotations

import contextlib
import math
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
    # Validate every file via normalize_target_path + blocklist before any side
    # effects, and REWRITE each file's target_path to the canonical form so that
    # classification, blocklist, pending enqueue, safe_join, and ``git add`` all
    # operate on the same value that passed validation (item 4). Without this a
    # backslash path like ``decisions\\x.md`` validated as ``decisions/x.md`` yet
    # committed a literal root-level backslash file outside every indexed prefix.
    from data_olympus.auth import is_writable_path, normalize_target_path
    from data_olympus.index import _classify_by_path
    rejected: list[str] = []
    canonical_files: list[dict[str, str]] = []
    for f in files:
        canonical = normalize_target_path(f["target_path"])
        if canonical is None or not is_writable_path(canonical):
            rejected.append(f["target_path"])
            continue
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
                    if not isinstance(fm, dict):
                        fm = {}
                except yaml.YAMLError:
                    fm = {}
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
