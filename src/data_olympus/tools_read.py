"""MCP read tool function implementations (decoupled from the FastMCP registration).

The functions below take their dependencies as kwargs so they can be unit-tested
without instantiating a FastMCP server. The server module wires them in.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from data_olympus.health import snapshot
from data_olympus.models import (
    CategoryCount,
    GetResponse,
    HealthResponse,
    ListEntry,
    ListResponse,
    OutlineResponse,
    SearchHitModel,
    SearchResponse,
    TierOutline,
)

if TYPE_CHECKING:
    from data_olympus.index import Index


def kb_health_fn(
    *,
    idx: Index,
    last_git_pull_at: float | None,
    staleness_degraded_sec: int,
    last_git_push_at: float | None = None,
    pending_count: int = 0,
    push_queue_size: int = 0,
    last_index_build_status: str = "ok",
    last_index_error: str | None = None,
    last_index_error_at: float | None = None,
    last_index_conflicts: list[dict[str, object]] | None = None,
    path_locks_held: int = 0,
    last_git_fetch_status: str = "no_change",
    last_git_fetch_error: str | None = None,
    last_git_fetch_at: float | None = None,
    last_successful_refresh_at: float | None = None,
    remote_head_sha: str | None = None,
) -> HealthResponse:
    state = snapshot(
        idx=idx,
        last_git_pull_at=last_git_pull_at,
        staleness_degraded_sec=staleness_degraded_sec,
        last_git_push_at=last_git_push_at,
        pending_count=pending_count,
        push_queue_size=push_queue_size,
        last_index_build_status=last_index_build_status,
        last_index_error=last_index_error,
        last_index_error_at=last_index_error_at,
        last_index_conflicts=last_index_conflicts,
        last_git_fetch_status=last_git_fetch_status,
        last_git_fetch_error=last_git_fetch_error,
        last_git_fetch_at=last_git_fetch_at,
        last_successful_refresh_at=last_successful_refresh_at,
        remote_head_sha=remote_head_sha,
    )
    return HealthResponse(
        kb_commit=state.kb_commit,
        index_built_at=state.index_built_at,
        total_rules=state.total_rules,
        last_git_pull_at=state.last_git_pull_at,
        last_git_push_at=state.last_git_push_at,
        staleness_seconds=state.staleness_seconds,
        degraded=state.degraded,
        db_size_bytes=state.db_size_bytes,
        pending_count=state.pending_count,
        push_queue_size=state.push_queue_size,
        path_locks_held=path_locks_held,
        last_index_build_status=state.last_index_build_status,
        last_index_error=state.last_index_error,
        last_index_error_at=state.last_index_error_at,
        last_index_conflicts=list(state.last_index_conflicts),
        last_git_fetch_status=state.last_git_fetch_status,
        last_git_fetch_error=state.last_git_fetch_error,
        last_git_fetch_at=state.last_git_fetch_at,
        last_successful_refresh_at=state.last_successful_refresh_at,
        remote_head_sha=state.remote_head_sha,
    )


def kb_outline_fn(*, idx: Index) -> OutlineResponse:
    outline = idx.outline()
    tiers = [
        TierOutline(
            name=str(t["name"]),
            categories=[
                CategoryCount(name=str(c["name"]), count=int(c["count"]))
                for c in t["categories"]  # type: ignore[attr-defined]
            ],
        )
        for t in outline
    ]
    health = idx.health()
    return OutlineResponse(tiers=tiers, source_commit=str(health["source_commit"]))


def kb_search_fn(
    *,
    idx: Index,
    query: str,
    limit: int = 20,
    tier: str | None = None,
    category: str | None = None,
    status: str | None = None,
    doc_type: str | None = None,
) -> SearchResponse:
    if limit > 100:
        limit = 100
    hits = idx.search(
        query, limit=limit, tier=tier, category=category, status=status, doc_type=doc_type
    )
    health = idx.health()
    return SearchResponse(
        query=query,
        hits=[
            SearchHitModel(
                id=h.id,
                path=h.path,
                title=h.title,
                snippet=h.snippet,
                score=h.score,
                status=h.status,
                type=h.doc_type,
            )
            for h in hits
        ],
        source_commit=str(health["source_commit"]),
        total_returned=len(hits),
    )


class KbNotFoundError(Exception):
    """Raised when kb_get_fn is asked for an id that does not exist."""


def kb_get_fn(*, idx: Index, id: str) -> GetResponse:
    doc = idx.get(id)
    if doc is None:
        raise KbNotFoundError(f"no document with id={id!r}")
    return GetResponse(
        id=doc.id,
        path=doc.path,
        title=doc.title,
        tier=doc.tier,
        category=doc.category,
        status=doc.status,
        type=doc.doc_type,
        tags=list(doc.tags),
        applies_when=list(doc.applies_when),
        description=doc.description,
        content_markdown=doc.content_markdown,
        last_modified=doc.last_modified,
        last_modified_source=doc.last_modified_source,
        source_commit=doc.source_commit,
        git_remote_url=doc.git_remote_url,
    )


def kb_list_fn(*, idx: Index, tier: str, category: str | None = None) -> ListResponse:
    entries = idx.list(tier=tier, category=category)
    list_entries = [ListEntry(id=e["id"], title=e["title"], path=e["path"]) for e in entries]
    health = idx.health()
    return ListResponse(
        tier=tier,
        category=category,
        entries=list_entries,
        source_commit=str(health["source_commit"]),
        total=len(list_entries),
    )
