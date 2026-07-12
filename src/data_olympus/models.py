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
    # Docs with present-but-malformed front-matter at the last index build (WP2b).
    # A non-zero value means a doc silently lost its governance metadata. WARNING
    # signal only: it does NOT flip ``degraded`` (alert on it separately).
    malformed_frontmatter: int = 0
    # Docs with a present-but-malformed ``validity`` block at the last index
    # build (issue #107). Same WARNING-only rationale as malformed_frontmatter.
    malformed_validity: int = 0
    # Docs excluded from in-force retrieval by the supersession-graph rule at
    # the last index build (issue #110 slice 2). Same WARNING-only rationale.
    graph_excluded_docs: int = 0
    # Latest published version, cached by the periodic version-check background
    # task (issue #146 / KNA-68). None until the first check completes, or forever
    # when KB_DISABLE_VERSION_CHECK is set (air-gapped: zero outbound calls) or the
    # lookup is offline. update_available is True only when a strictly newer version
    # than the installed one is published. The health route reads this cache only;
    # it never touches the network on the request path. Deviation-only in compact
    # mode: omitted until a newer version is actually detected.
    latest_version: str | None = None
    update_available: bool = False
    # Maintenance-ledger CTA (issue #113): short structured items
    # ({kind, message, count}) an agent should surface to the operator and act
    # on only with operator confirmation. None (field omitted in compact mode)
    # when the computed maintenance state is clean -- token discipline.
    pending_actions: list[dict[str, object]] | None = None

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
    # validity/freshness (issue #107): deviation-only indicator, one of
    # "stale" | "expired" | "upcoming", "" when fresh/absent. "expired" only
    # ever appears when the hit was explicitly included (include_expired=True
    # or the validity_state facet); default kb_search never returns an
    # expired doc at all.
    freshness: str = ""
    # Computed in-force boolean (issue #109): the single-sourced predicate
    # (status class AND validity window AND not-inbox AND not-graph-excluded,
    # composing issue #110 slice 2), NEVER stored in frontmatter. Always
    # present verbose; compact emits it deviation-only (`in_force: false`,
    # see compact_dump). Exposed so a caller reading a hit that was NOT
    # retrieved via in_force=True (e.g. a default kb_search) can still tell
    # whether it may govern now.
    in_force: bool = True
    # Lifecycle-relationship surfacing (issue #110 slice 2): deviation-only,
    # sorted list of ids that supersede this doc (the UNION of its own
    # frontmatter `superseded_by` and any reverse `supersedes` edge -- see
    # index._superseded_by_map), empty when not superseded. Computed and
    # attached to EVERY hit regardless of `in_force` (same as `freshness`);
    # carries no ranking or filtering effect of its own.
    superseded_by: list[str] = []

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
        ``type`` only when present, ``freshness`` only when non-empty
        (issue #107: a fresh/absent-validity hit omits it entirely), and
        ``superseded_by`` only when non-empty (issue #110 slice 2: same
        deviation-only pattern -- a doc nobody supersedes omits it entirely).
        ``verbose=True`` restores the full shape.

        ``in_force`` is emitted deviation-only, i.e. ONLY when False (issue
        #109, codex review blocker). The status/freshness emissions above key
        off the RAW frontmatter status, so a memory-inbox doc with a forged
        ``status: active`` would otherwise render as an ordinary current rule
        in compact hits with no deviation signal at all -- the floor made it
        not-in-force, but nothing said so. An in-force hit stays byte-for-byte
        unchanged (no key).
        """
        snippet = self.snippet
        if len(snippet) > COMPACT_SNIPPET_CHARS:
            snippet = snippet[:COMPACT_SNIPPET_CHARS] + "…"
        d: dict[str, object] = {"id": self.id, "title": self.title, "snippet": snippet}
        if self.status and self.status not in _IN_FORCE_STATUSES:
            d["status"] = self.status
        if self.type:
            d["type"] = self.type
        if self.freshness:
            d["freshness"] = self.freshness
        if self.superseded_by:
            d["superseded_by"] = list(self.superseded_by)
        if not self.in_force:
            d["in_force"] = False
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
    # validity/freshness (issue #107). kb_get ALWAYS resolves regardless of
    # expiry (ids never dangle), so ``validity`` carries the full object (None
    # when the doc has no validity metadata at all) and ``freshness`` is the
    # same deviation-only indicator as SearchHitModel's, computed against the
    # request's ``today`` -- INCLUDING "expired", which a search hit only ever
    # shows when explicitly included.
    validity: dict[str, str] | None = None
    freshness: str = ""
    # Computed in-force boolean (issue #109): see SearchHitModel.in_force for
    # the rationale (full predicate: status class AND validity window AND
    # not-inbox AND not-graph-excluded). Always present verbose; compact emits
    # it deviation-only (`in_force: false`, see compact_dump). kb_get always
    # resolves regardless of expiry, so this is the caller's single signal for
    # "may this doc govern now", independent of status/validity/inbox/edges
    # being read apart.
    in_force: bool = True
    # Lifecycle-relationship surfacing (issue #110 slice 2), always resolved
    # (kb_get by id ignores in-force/graph-exclusion filtering entirely, same
    # as it already ignores expiry). ``superseded_by`` is the UNION of this
    # doc's own frontmatter claim and any reverse `supersedes` edge naming it
    # (see index._superseded_by_map: ONE consistent shape for both the
    # honest self-declared case and the "forgotten status flip" case).
    # ``contradicts`` is this doc's own frontmatter list (never affects
    # filtering/ranking); ``contradicted_by`` is the computed reverse: every
    # other doc whose `contradicts` names this one.
    superseded_by: list[str] = []
    contradicts: list[str] = []
    contradicted_by: list[str] = []

    def compact_dump(self) -> dict[str, object]:
        """Token-lean kb_get response (issue #65).

        Keeps the full ``content_markdown`` body by default: agents call kb_get
        precisely to read the doc, so truncating it would break the primary use.
        Retains provenance a direct caller needs: ``source_commit`` (the commit
        the doc was read at) and ``last_modified`` are kept. Trims only low-value
        envelope fields: ``path`` (recoverable with ``kb_get(id, verbose=True)``),
        ``git_remote_url`` (null for most docs), and ``last_modified_source`` (a
        provenance *label* like ``git``/``mtime-fallback``, not the timestamp).
        Omits empty ``status`` / ``type`` / ``applies_when`` / ``description`` /
        ``validity`` / ``freshness`` (issue #107: a doc with no validity
        metadata omits both entirely) and ``superseded_by`` / ``contradicts`` /
        ``contradicted_by`` (issue #110 slice 2: omitted when empty, same
        deviation-only pattern). ``verbose=True`` restores the full shape.

        ``in_force`` is emitted deviation-only, i.e. ONLY when False (issue
        #109, codex review blocker): compact kb_get shows the RAW frontmatter
        ``status``, so a memory-inbox doc with a forged ``status: active``
        would otherwise read as an ordinary current rule with no signal that
        the in-force floor disqualified it. An in-force doc's compact shape is
        byte-for-byte unchanged (no key).
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
        d["source_commit"] = self.source_commit
        if self.validity:
            d["validity"] = dict(self.validity)
        if self.freshness:
            d["freshness"] = self.freshness
        if self.superseded_by:
            d["superseded_by"] = list(self.superseded_by)
        if self.contradicts:
            d["contradicts"] = list(self.contradicts)
        if self.contradicted_by:
            d["contradicted_by"] = list(self.contradicted_by)
        if not self.in_force:
            d["in_force"] = False
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
    evidence: list[str] = []


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
    evidence: list[str] = []


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
    # Governed-lane write protection (issue #112): set when a pending_confirmation
    # outcome was NOT a plain low-confidence park but a demotion under
    # KB_GOVERNED_LANE_PROTECTION -- "status_promotion" (the postimage sets/
    # changes status into the in-force class), "governed_target" (the edit
    # target is verified currently in force), or "governed_target_unverified"
    # (the target's in-force state could NOT be verified -- no index / index
    # read failure -- and the rule fails closed). None on every other outcome
    # (including a plain low-confidence pending_confirmation).
    demotion_reason: str | None = None


class ResolvePendingRequest(BaseModel):
    pending_id: str
    decision: str  # 'approve' | 'reject' | 'edit'
    edited_text: str | None = None
    # Operator-only override of the issue #71 secret-scanning gate: when True
    # and the resolved postimage is flagged, the commit proceeds anyway and the
    # audit event records the override. Never available on the propose (auto-
    # commit) request models.
    override_secret_scan: bool = False


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
    # issue #71: True when the ORIGINAL postimage was flagged by the secret
    # scanner at propose time. Surfaced so an operator running `kb pending`
    # sees the warning (and the pattern name) without needing to inspect the
    # raw postimage; the matched value itself is never included here.
    secret_scan_flagged: bool = False
    matching_pattern: str | None = None
    # Provenance (issue #109): already persisted in pending meta at enqueue
    # time (source_session on both proposal types, reason on edit proposals),
    # simply not surfaced by kb_pending until now. None when the entry
    # predates this field or the proposal type does not carry it (e.g. a
    # memory proposal has no `reason`).
    source_session: str | None = None
    reason: str | None = None
    # Optional supporting evidence the proposer supplied (kb_propose_memory /
    # kb_propose_edit `evidence` param), redacted item-by-item by the same
    # secret scanner as `tags` before it ever reaches pending meta. None when
    # no evidence was supplied.
    evidence: list[str] | None = None
    # Governed-lane write protection (issue #112): see ProposeResponse.demotion_reason
    # for the rationale. None when this entry parked for a plain low-confidence
    # reason rather than a governed-lane demotion.
    demotion_reason: str | None = None
    # Injection-pattern annotation (issue #112, advisory only -- never demotes
    # or rejects by itself): True when the postimage matched at least one
    # agent-directed injection heuristic. `injection_patterns` carries
    # `"pattern_name:line"` entries (never the matched text), mirroring the
    # issue #71 secret-scan redaction discipline.
    injection_suspect: bool = False
    injection_patterns: list[str] | None = None


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
    # issue #71 secret-scanning gate: the pattern NAME (never the matched
    # value) on a rejected_secret_detected event, or on a committed resolve
    # where the operator consciously overrode a flagged postimage.
    matching_pattern: str | None = None
    # True only on a resolve that committed a postimage the scanner flagged,
    # via an explicit operator override (never set by an auto-commit path).
    secret_scan_override: bool | None = None
    # Provenance (issue #109): the redacted evidence list echoed on
    # propose_memory / propose_edit audit events, when supplied. Never the raw
    # value if a scan flagged an item (see tools_write._redact_evidence).
    evidence: list[str] | None = None
    # Governed-lane write protection (issue #112): the demotion reason
    # ("status_promotion" | "governed_target" | "governed_target_unverified")
    # when a pending_confirmation event was a governed-lane demotion rather
    # than a plain low-confidence park; and whether the postimage matched an
    # advisory injection-pattern heuristic (never blocks/demotes by itself --
    # see governed_lane.py).
    demotion_reason: str | None = None
    injection_suspect: bool | None = None
    # Tamper-evident chain fields (present on events appended with chaining).
    event_id: str | None = None
    prev_hash: str | None = None
    hash: str | None = None


class AuditResponse(BaseModel):
    events: list[AuditEvent]
    returned: int
    limit_hit: bool = False


class SessionRecapResponse(BaseModel):
    """kb_session_recap response (issue #112): a per-session write summary
    over the audit log -- N committed, M demoted-to-pending
    (pending_confirmation, including but not limited to governed-lane
    demotions), K rejected (any rejected_* status). Used by the per-session
    feedback loop (bin/kb session-recap, the SessionEnd hook, and the
    kb_consult pending_actions envelope) so a demotion is never silent."""

    source_session: str
    committed: int = 0
    demoted_to_pending: int = 0
    rejected: int = 0


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
    # Maintenance-ledger CTA (issue #113). See HealthResponse.pending_actions;
    # omitted (None) when the computed maintenance state is clean. Callers
    # serialize this response with ``exclude_none=True`` so a clean state
    # drops the field entirely rather than emitting a null.
    pending_actions: list[dict[str, object]] | None = None


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
    # Governed-lane write protection (issue #112): see ProposeResponse.demotion_reason.
    demotion_reason: str | None = None


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
