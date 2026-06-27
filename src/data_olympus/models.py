"""Pydantic models for MCP tool responses. Slice 2A scope: read tools only."""
from __future__ import annotations

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """kb_health response. Slice 2A added write-side placeholders. Slice 2B adds
    last_index_* fields for build-failure visibility."""

    kb_commit: str
    index_built_at: float | None
    total_rules: int
    last_git_pull_at: float | None
    last_git_push_at: float | None = None
    staleness_seconds: float | None
    degraded: bool
    db_size_bytes: int
    pending_count: int = 0
    push_queue_size: int = 0
    path_locks_held: int = 0
    last_index_build_status: str = "ok"
    last_index_error: str | None = None
    last_index_error_at: float | None = None
    last_index_conflicts: list[dict[str, object]] = []
    # Git sync-failure visibility (a fetch/ff failure must not look "fresh").
    last_git_fetch_status: str = "no_change"
    last_git_fetch_error: str | None = None
    last_git_fetch_at: float | None = None
    last_successful_refresh_at: float | None = None
    remote_head_sha: str | None = None


class CategoryCount(BaseModel):
    """One category with doc count."""

    name: str
    count: int


class TierOutline(BaseModel):
    """A tier with its categories."""

    name: str
    categories: list[CategoryCount]


class OutlineResponse(BaseModel):
    """kb_outline response."""

    tiers: list[TierOutline]
    source_commit: str


class SearchHitModel(BaseModel):
    """One search hit."""

    id: str
    path: str
    title: str
    snippet: str
    score: float
    status: str = ""
    type: str = ""


class SearchResponse(BaseModel):
    """kb_search response."""

    query: str
    hits: list[SearchHitModel]
    source_commit: str
    total_returned: int = Field(description="Number of hits actually returned (after limit).")


class GetResponse(BaseModel):
    """kb_get response."""

    id: str
    path: str
    title: str
    tier: str
    category: str
    status: str = ""
    type: str = ""
    tags: list[str]
    applies_when: list[str] = []
    description: str = ""
    content_markdown: str
    last_modified: str
    last_modified_source: str
    source_commit: str
    git_remote_url: str | None = None


class ListEntry(BaseModel):
    """One entry in kb_list output."""

    id: str
    title: str
    path: str


class ListResponse(BaseModel):
    """kb_list response."""

    tier: str
    category: str | None
    entries: list[ListEntry]
    source_commit: str
    total: int


class ProposeMemoryRequest(BaseModel):
    text: str
    tags: list[str] = []
    source_session: str
    agent_identity: str
    confidence: float


class ProposeEditRequest(BaseModel):
    target_path: str
    postimage: str
    base_commit: str
    base_blob_sha: str | None = None
    target_file_hash: str | None = None
    reason: str = ""
    source_session: str
    agent_identity: str
    confidence: float


class ProposeResponse(BaseModel):
    status: str
    commit_sha: str | None = None
    push_state: str | None = None
    pending_id: str | None = None
    proposal_text: str | None = None
    operator_prompt: str | None = None
    reason: str | None = None
    target_tier: str | None = None
    target_path: str | None = None
    matching_pattern: str | None = None
    resolved_path: str | None = None


class ResolvePendingRequest(BaseModel):
    pending_id: str
    decision: str  # 'approve' | 'reject' | 'edit'
    edited_text: str | None = None


class ResolvePendingResponse(BaseModel):
    status: str
    commit_sha: str | None = None
    conflict_markers: str | None = None
    base_commit: str | None = None
    current_commit: str | None = None


class PendingEntry(BaseModel):
    pending_id: str
    proposal_type: str
    target_path: str
    confidence: float | None = None
    agent_identity: str | None = None
    created_at: float
    expires_at: float | None = None


class PendingListResponse(BaseModel):
    pending: list[PendingEntry]


class AuditEvent(BaseModel):
    ts: float
    event_type: str
    status: str
    agent_identity: str | None = None
    source_session: str | None = None
    target_path: str | None = None
    target_tier: str | None = None
    confidence: float | None = None
    pending_id: str | None = None
    commit_sha: str | None = None
    reason: str | None = None
    remote_addr: str | None = None
    # Tamper-evident chain fields (present on events appended with chaining).
    event_id: str | None = None
    prev_hash: str | None = None
    hash: str | None = None


class AuditResponse(BaseModel):
    events: list[AuditEvent]
    returned: int
    limit_hit: bool = False


class AuditVerifyResponse(BaseModel):
    """Result of recomputing the audit hash chain."""

    ok: bool
    first_broken_index: int = -1


class ConsultResponse(BaseModel):
    """kb_consult response: governing rules plus a recorded consultation."""

    is_governed_decision: bool
    rules: list[SearchHitModel] = []
    consulted_at: float
    ttl_seconds: int


class GateCheckResponse(BaseModel):
    """kb_gate_check verdict for a pending code action."""

    verdict: str  # 'allow' | 'consult_required'
    reason: str = ""
    rules: list[SearchHitModel] = []


class ComplianceResponse(BaseModel):
    """Aggregated enforcement-event counts overall and per agent."""

    counts: dict[str, int] = {}
    by_agent: dict[str, dict[str, int]] = {}


class RecordEventResponse(BaseModel):
    """kb_record_event response."""

    recorded: bool
    event_type: str


class RenameCandidateModel(BaseModel):
    target_tier: str
    target_workspace: str
    target_component: str | None = None
    confidence: float
    matched_via: str


class OnboardingStatusResponse(BaseModel):
    state: str
    workspace: str
    component: str | None = None
    missing_files: list[str] = []
    rename_candidates: list[RenameCandidateModel] = []


class BootstrapFileSpec(BaseModel):
    target_path: str
    postimage: str
    reason: str = ""


class BootstrapRequest(BaseModel):
    workspace: str
    component: str | None = None
    workspace_remote_url: str | None = None
    component_remote_url: str | None = None
    files: list[BootstrapFileSpec]
    source_session: str
    agent_identity: str
    confidence: float


class BootstrapResponse(BaseModel):
    status: str
    commit_sha: str | None = None
    pending_id: str | None = None
    rejected_paths: list[str] = []
    push_state: str | None = None
    operator_prompt: str | None = None
