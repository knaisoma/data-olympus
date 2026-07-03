"""Pydantic models for MCP tool responses. Slice 2A scope: read tools only."""
from __future__ import annotations

from pydantic import BaseModel, Field

# Status values that mean "this guidance currently applies" (the in-force class,
# mirrors index._IN_FORCE_STATUSES). In compact search hits, a hit's ``status``
# is OMITTED when it is in-force or absent, and EMITTED only when it deviates
# (superseded/deprecated/rejected/...), because a deviation is the signal an
# agent must act on (do not follow superseded guidance) while the in-force
# default carries no information. See issue #65.
_IN_FORCE_STATUSES = frozenset({"active", "accepted", "approved"})


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
    # Push-queue entries that hit max_attempts and were frozen for operator
    # inspection. Frozen entries are skipped by the retry loop, so a nonzero
    # value means writes are stuck and need manual intervention (see the
    # operator unfreeze path in docs/serving.md).
    push_queue_frozen: int = 0
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
    # Live streamable-http transport sessions. None when the count cannot be
    # observed (e.g. before the HTTP app's lifespan starts, or in-memory tests).
    # Surfaced so session accumulation is diagnosable in production (issue #43).
    live_sessions: int | None = None

    # Health fields always present in compact mode even when falsy, because a
    # consumer branches on them (bin/kb and the REST 503 path read ``degraded``;
    # a monitor reads the core snapshot). Everything else is a diagnostic that
    # only matters when set, so compact mode omits it when None/empty.
    _COMPACT_ALWAYS = (
        "kb_commit",
        "total_rules",
        "degraded",
        "db_size_bytes",
        "staleness_seconds",
        "index_built_at",
        "pending_count",
        "push_queue_size",
        "last_index_build_status",
    )

    def compact_dump(self) -> dict[str, object]:
        """Token-lean kb_health (issue #65). Keeps the core snapshot fields a
        consumer always branches on; omits the many diagnostic fields when they
        are ``None`` or an empty list (a no-error steady state carries a run of
        nulls that cost tokens and say nothing). This filter is generic, so a
        field a concurrent package adds flows through unchanged: it appears in
        compact output only when populated. ``verbose=True`` restores the full,
        every-field shape."""
        full = self.model_dump()
        always = set(self._COMPACT_ALWAYS)
        out: dict[str, object] = {}
        for k, v in full.items():
            if k in always or (v is not None and v != []):
                out[k] = v
        return out


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

    def compact_dump(self) -> dict[str, object]:
        """kb_outline is already a lean tier/category/count tree with no
        redundant or low-value fields (measured saving from any further trim is
        below the 5% adopt threshold in issue #65), so compact mode returns the
        same shape as full. The method exists so every read tool honours the
        ``verbose`` parameter uniformly."""
        return self.model_dump()


# Compact-mode snippet cap (characters). Measured against the example-bundle:
# real snippets sit well under this, so the cap is a guard against pathological
# long snippets, not a routine truncation. See issue #65 / benchmarks/token_compact.py.
COMPACT_SNIPPET_CHARS = 160


class SearchHitModel(BaseModel):
    """One search hit."""

    id: str
    path: str
    title: str
    snippet: str
    score: float
    status: str = ""
    type: str = ""

    def compact_dump(self) -> dict[str, object]:
        """Token-lean hit shape (issue #65).

        Drops:
          - ``path``: redundant with ``id`` (fetch the doc via kb_get(id) to get
            its path); saved the single largest per-hit token cost.
          - ``score``: a raw bm25 float (e.g. -4.877023162317077) that mixes
            incompatible conventions across pipeline paths and is uninterpretable
            to an agent. Array order already conveys rank.
        Emits ``status`` only when it deviates from the in-force default (i.e. a
        superseded/deprecated/rejected hit an agent must NOT treat as current),
        and ``type`` only when present. ``verbose=True`` restores the full shape.
        """
        snippet = self.snippet
        if len(snippet) > COMPACT_SNIPPET_CHARS:
            snippet = snippet[:COMPACT_SNIPPET_CHARS] + "…"
        d: dict[str, object] = {"id": self.id, "title": self.title, "snippet": snippet}
        if self.status and self.status not in _IN_FORCE_STATUSES:
            d["status"] = self.status
        if self.type:
            d["type"] = self.type
        return d


class SearchResponse(BaseModel):
    """kb_search response."""

    query: str
    hits: list[SearchHitModel]
    source_commit: str
    total_returned: int = Field(description="Number of hits actually returned (after limit).")
    abstained: bool = Field(
        default=False,
        description=(
            "True when abstain=True was requested and the signal gate fired: the "
            "query matched no discriminating column, so the search deliberately "
            "returned no hits instead of surfacing a weak, out-of-scope match. "
            "Distinct from an ordinary zero-hit search (abstained stays False)."
        ),
    )
    abstain_reason: str | None = Field(
        default=None,
        description=(
            "Machine-readable reason present only when abstained is True (e.g. "
            "'no_signal_match'). None otherwise."
        ),
    )

    def compact_dump(self) -> dict[str, object]:
        """Token-lean search response (issue #65).

        Drops the ``query`` echo (the caller knows what it searched) and shapes
        each hit via :meth:`SearchHitModel.compact_dump`. Keeps the small,
        informative envelope (source_commit, total_returned, and the abstain
        signal when it fired). ``verbose=True`` restores the full JSON shape.
        """
        d: dict[str, object] = {
            "hits": [h.compact_dump() for h in self.hits],
            "source_commit": self.source_commit,
            "total_returned": self.total_returned,
        }
        if self.abstained:
            d["abstained"] = True
            d["abstain_reason"] = self.abstain_reason
        return d


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

    def compact_dump(self) -> dict[str, object]:
        """Token-lean kb_get response (issue #65).

        Keeps the full ``content_markdown`` body by default: agents call kb_get
        precisely to read the doc, so truncating it would break the primary use.
        Trims only the low-value envelope fields that the body already implies or
        that a caller rarely needs inline: ``path`` (derivable from ``id`` /
        present in the body's own links), ``git_remote_url``,
        ``last_modified_source``, and ``source_commit``. Omits empty ``status`` /
        ``type`` / ``applies_when`` / ``description``. ``verbose=True`` restores
        the full shape.
        """
        d: dict[str, object] = {
            "id": self.id,
            "title": self.title,
            "tier": self.tier,
            "category": self.category,
        }
        if self.status:
            d["status"] = self.status
        if self.type:
            d["type"] = self.type
        d["tags"] = list(self.tags)
        if self.applies_when:
            d["applies_when"] = list(self.applies_when)
        if self.description:
            d["description"] = self.description
        d["content_markdown"] = self.content_markdown
        d["last_modified"] = self.last_modified
        return d


class ListEntry(BaseModel):
    """One entry in kb_list output."""

    id: str
    title: str
    path: str

    def compact_dump(self) -> dict[str, object]:
        """Drop ``path`` (derivable via kb_get(id)); keep id + title."""
        return {"id": self.id, "title": self.title}


class ListResponse(BaseModel):
    """kb_list response."""

    tier: str
    category: str | None
    entries: list[ListEntry]
    source_commit: str
    total: int

    def compact_dump(self) -> dict[str, object]:
        """Token-lean kb_list response (issue #65): per-entry ``path`` dropped
        (fetch via kb_get(id)). Omits a null ``category``. ``verbose=True``
        restores the full shape."""
        d: dict[str, object] = {"tier": self.tier}
        if self.category is not None:
            d["category"] = self.category
        d["entries"] = [e.compact_dump() for e in self.entries]
        d["source_commit"] = self.source_commit
        d["total"] = self.total
        return d


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
    # Machine-readable rejection detail for the CAS / validation gates
    # (rejected_stale_base, rejected_invalid_document) on the resolve path.
    reason: str | None = None
    # Truthful publish state on a committed resolve: "queued" when the push-queue
    # entry landed, else "enqueue_failed_recovery_pending" (the commit is durable
    # but is recovered by in-process/startup recovery, not queued this attempt).
    push_state: str | None = None


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
    """kb_gate_check verdict for a pending code action.

    ``session_id`` and ``workspace`` echo the exact gate key back so an MCP
    caller that is blocked can construct the clearing kb_consult call without
    guessing either value (the session id in particular is not agent-guessable)."""

    verdict: str  # 'allow' | 'consult_required'
    reason: str = ""
    rules: list[SearchHitModel] = []
    session_id: str = ""
    workspace: str = ""


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


class CleanupItem(BaseModel):
    local_path: str
    classification: str  # imported_duplicate | partial_overlap | unique
    kb_id: str | None = None
    kb_path: str | None = None
    overlap_headings: list[str] = []
    thin_pointer_text: str | None = None


class CleanupPlanResponse(BaseModel):
    workspace: str
    component: str | None = None
    items: list[CleanupItem] = []
    summary: dict[str, int] = {}
