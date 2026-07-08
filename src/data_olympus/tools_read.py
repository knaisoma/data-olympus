"""MCP read tool function implementations (decoupled from the FastMCP registration).

The functions below take their dependencies as kwargs so they can be unit-tested
without instantiating a FastMCP server. The server module wires them in.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from data_olympus.health import snapshot
from data_olympus.maintenance import pending_actions_for
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


class _CompactDumpable(Protocol):
    def compact_dump(self) -> dict[str, object]: ...
    def model_dump(self) -> dict[str, object]: ...


def shape_response(resp: _CompactDumpable, *, verbose: bool) -> dict[str, object]:
    """Serialize a read-tool response to the wire dict.

    ``verbose=False`` (the default for every read tool) returns the token-compact
    shape; ``verbose=True`` returns the full, byte-for-byte legacy JSON shape. The
    ``verbose`` parameter threads from the MCP tool wrappers in ``server.py`` and
    the REST handlers in ``rest_api.py`` through here into the per-model
    ``compact_dump`` / ``model_dump`` methods, so MCP and REST stay consistent.
    See issue #65.
    """
    return resp.model_dump() if verbose else resp.compact_dump()


def kb_health_fn(
    *,
    idx: Index,
    last_git_pull_at: float | None,
    staleness_degraded_sec: int,
    last_git_push_at: float | None = None,
    pending_count: int = 0,
    push_queue_size: int = 0,
    push_queue_frozen: int = 0,
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
    live_sessions: int | None = None,
) -> HealthResponse:
    state = snapshot(
        idx=idx,
        last_git_pull_at=last_git_pull_at,
        staleness_degraded_sec=staleness_degraded_sec,
        last_git_push_at=last_git_push_at,
        pending_count=pending_count,
        push_queue_size=push_queue_size,
        push_queue_frozen=push_queue_frozen,
        last_index_build_status=last_index_build_status,
        last_index_error=last_index_error,
        last_index_error_at=last_index_error_at,
        last_index_conflicts=last_index_conflicts,
        last_git_fetch_status=last_git_fetch_status,
        last_git_fetch_error=last_git_fetch_error,
        last_git_fetch_at=last_git_fetch_at,
        last_successful_refresh_at=last_successful_refresh_at,
        remote_head_sha=remote_head_sha,
        live_sessions=live_sessions,
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
        push_queue_frozen=state.push_queue_frozen,
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
        live_sessions=state.live_sessions,
        malformed_frontmatter=state.malformed_frontmatter,
        malformed_validity=state.malformed_validity,
        pending_actions=pending_actions_for(getattr(idx, "maintenance_state", None)),
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
    in_force: bool = False,
    doc_type: str | None = None,
    abstain: bool = False,
    include_expired: bool = False,
    validity_state: str | None = None,
    today: str | None = None,
) -> SearchResponse:
    from data_olympus.format.validate import compute_freshness, today_iso
    from data_olympus.search_gate import abstain_gate

    # Clamp to 1..100. Clamping only the upper bound let a negative
    # ``limit`` (e.g. -1) reach SQLite as ``LIMIT -1``, which SQLite treats as
    # "no limit" and dumps the entire corpus in one request. Bound both ends.
    if limit > 100:
        limit = 100
    elif limit < 1:
        limit = 1
    today = today if today is not None else today_iso()
    search_kwargs: dict[str, object] = {
        "tier": tier,
        "category": category,
        "status": status,
        "in_force": in_force,
        "doc_type": doc_type,
        "include_expired": include_expired,
        "validity_state": validity_state,
        "today": today,
    }
    abstained = False
    abstain_reason: str | None = None
    if abstain:
        # Single-sourced signal gate (search_gate.abstain_gate). ``None`` means
        # the gate fired: return an explicit abstained response, not just zero
        # hits, so a caller can tell "no governing rule" from "search found none".
        gated = abstain_gate(idx, query, limit=limit, **search_kwargs)
        if gated is None:
            hits = []
            abstained = True
            abstain_reason = "no_signal_match"
        else:
            hits = gated
    else:
        hits = idx.search(query, limit=limit, **search_kwargs)  # type: ignore[arg-type]
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
                freshness=compute_freshness(
                    valid_from=h.valid_from, valid_until=h.valid_until,
                    recheck_by=h.recheck_by, today=today,
                ) or "",
            )
            for h in hits
        ],
        source_commit=str(health["source_commit"]),
        total_returned=len(hits),
        abstained=abstained,
        abstain_reason=abstain_reason,
    )


class KbNotFoundError(Exception):
    """Raised when kb_get_fn is asked for an id that does not exist."""


def kb_get_fn(*, idx: Index, id: str, today: str | None = None) -> GetResponse:
    from data_olympus.format.validate import compute_freshness, today_iso

    doc = idx.get(id)
    if doc is None:
        raise KbNotFoundError(f"no document with id={id!r}")
    today = today if today is not None else today_iso()
    validity: dict[str, str] | None = None
    has_validity = (
        doc.valid_from or doc.valid_until or doc.last_verified
        or doc.recheck_by or doc.verification_source
    )
    if has_validity:
        validity = {
            "valid_from": doc.valid_from,
            "valid_until": doc.valid_until,
            "last_verified": doc.last_verified,
            "recheck_by": doc.recheck_by,
            "verification_source": doc.verification_source,
        }
    freshness = compute_freshness(
        valid_from=doc.valid_from, valid_until=doc.valid_until,
        recheck_by=doc.recheck_by, today=today,
    ) or ""
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
        validity=validity,
        freshness=freshness,
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
