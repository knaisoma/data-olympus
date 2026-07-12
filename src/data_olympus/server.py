"""FastMCP server entry. Streamable HTTP transport at the configured port."""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import time
from typing import TYPE_CHECKING, Annotated, Any

from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware
from pydantic import Field

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import mcp.types as mt
    from fastmcp.server.middleware import MiddlewareContext
    from fastmcp.server.middleware.middleware import CallNext
    from fastmcp.tools.base import ToolResult

from data_olympus.audit_log import AuditLog
from data_olympus.auth import PathBlocklist
from data_olympus.config import Config, load_config
from data_olympus.cooccurrence import (
    DEFAULT_MAX_TERMS,
    compose_expanders,
    cooccurrence_enabled,
)
from data_olympus.embeddings import EmbeddingsConfig, build_embedder
from data_olympus.enforce_policy import ConsultationLedger, IntentClassifier
from data_olympus.git_ops import GitOps
from data_olympus.index import Index, SearchHit, make_status_reranker
from data_olympus.pending import PendingQueue
from data_olympus.principals import (
    AUTH_REQUIRED_TOOLS,
    LOCAL_TRUSTED,
    WRITE_TOOL_CAPABILITY,
    Principal,
    PrincipalRegistry,
)
from data_olympus.push_queue import PushQueue
from data_olympus.query_expansion import default_query_expander
from data_olympus.rate_limit import SlidingWindowLimiter
from data_olympus.search_shortcut import make_id_tag_reranker
from data_olympus.tools_read import (
    kb_health_fn,
    kb_outline_fn,
    kb_search_fn,
    shape_response,
)
from data_olympus.worktrees import WorktreeRegistry

log = logging.getLogger("data_olympus")

# The principal resolved by the MCP auth middleware for the in-flight tool call.
# Defaults to the fully-trusted local principal so direct (non-HTTP) tool calls
# in tests behave as before. Read by the write-tool closures for the clamp.
_current_principal: contextvars.ContextVar[Principal] = contextvars.ContextVar(
    "current_principal", default=LOCAL_TRUSTED
)

READ_ONLY_TOOL = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
STATEFUL_NONDESTRUCTIVE_TOOL = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}
PROPOSAL_WRITE_TOOL = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": True,
}
PROPOSAL_EDIT_TOOL = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": True,
}
DESTRUCTIVE_WRITE_TOOL = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": True,
}

VerboseParam = Annotated[
    bool,
    Field(description="False returns the compact response; true includes all fields."),
]
QueryParam = Annotated[str, Field(description="Natural-language search query.")]
LimitParam = Annotated[int, Field(description="Maximum number of results, clamped to 1..100.")]
TierParam = Annotated[
    str | None,
    Field(description="Optional tier filter such as T1, T2, T3, or T4."),
]
RequiredTierParam = Annotated[
    str,
    Field(description="Required tier such as T1, T2, T3, or T4."),
]
CategoryParam = Annotated[
    str | None,
    Field(description="Optional category filter within a tier."),
]
StatusParam = Annotated[str | None, Field(description="Optional frontmatter status filter.")]
InForceParam = Annotated[
    bool,
    Field(description="True returns only currently governing documents."),
]
DocTypeParam = Annotated[str | None, Field(description="Optional document type filter.")]
AbstainParam = Annotated[
    bool,
    Field(description="True returns an explicit abstention when the query has no KB signal."),
]
IncludeExpiredParam = Annotated[
    bool,
    Field(description="True allows expired documents in search results."),
]
ValidityStateParam = Annotated[
    str | None,
    Field(description="Optional validity facet: expired, stale, or expiring_within:N."),
]
DocumentIdParam = Annotated[str, Field(description="Stable KB document id to retrieve.")]
WorkspaceParam = Annotated[str, Field(description="Project or workspace key in the KB.")]
ComponentParam = Annotated[
    str | None,
    Field(description="Optional component key under the workspace."),
]
RemoteUrlParam = Annotated[
    str | None,
    Field(description="Optional git remote URL used for matching."),
]
LocalFilesParam = Annotated[
    list[dict[str, str]],
    Field(description="Local docs to compare, each with path and content keys."),
]
JaccardParam = Annotated[
    float,
    Field(description="Similarity threshold above which a local doc is treated as duplicate."),
]
TextParam = Annotated[str, Field(description="Markdown memory text to propose.")]
TagsParam = Annotated[list[str], Field(description="Short tags for the proposed memory.")]
SourceSessionParam = Annotated[
    str,
    Field(description="Stable id of the agent session making the call."),
]
AgentIdentityParam = Annotated[
    str,
    Field(description="Human-readable agent identity for audit events."),
]
ConfidenceParam = Annotated[
    float,
    Field(description="Caller confidence in the proposal, from 0.0 to 1.0."),
]
EvidenceParam = Annotated[
    list[str] | None,
    Field(description="Optional supporting evidence strings, max 10 items of 500 chars each."),
]
TargetPathParam = Annotated[
    str,
    Field(description="KB-relative markdown path to create or edit."),
]
PostimageParam = Annotated[
    str,
    Field(description="Complete markdown file content after the proposed edit."),
]
BaseCommitParam = Annotated[str, Field(description="Git commit the proposal was based on.")]
BaseBlobShaParam = Annotated[
    str | None,
    Field(description="Optional git blob sha for compare-and-swap protection."),
]
TargetFileHashParam = Annotated[
    str | None,
    Field(description="Optional content hash for compare-and-swap protection."),
]
ReasonParam = Annotated[str, Field(description="Short reason for the proposed change.")]
PendingIdParam = Annotated[str, Field(description="Pending proposal id to resolve.")]
DecisionParam = Annotated[
    str,
    Field(description="Resolution decision: approve or reject."),
]
EditedTextParam = Annotated[
    str | None,
    Field(description="Optional replacement postimage used when approving."),
]
OverrideSecretScanParam = Annotated[
    bool,
    Field(description="Operator override for false-positive secret-scan matches."),
]
SinceParam = Annotated[float | None, Field(description="Optional Unix timestamp lower bound.")]
AgentParam = Annotated[str | None, Field(description="Optional agent_identity filter.")]
EventStatusParam = Annotated[str | None, Field(description="Optional audit event status filter.")]
AuditLimitParam = Annotated[int, Field(description="Maximum audit events to return.")]
BootstrapFilesParam = Annotated[
    list[dict[str, str]],
    Field(description="Bootstrap files, each with target_path and postimage keys."),
]
TriggerParam = Annotated[
    str,
    Field(description="Consult trigger: explicit or prompt_hook."),
]
SessionIdParam = Annotated[
    str,
    Field(description="Agent session id checked against consult history."),
]
ToolNameParam = Annotated[str, Field(description="Name of the tool or command about to run.")]
ActionPathParam = Annotated[
    str | None,
    Field(description="Optional path or URL affected by the pending action."),
]
ActionDiffParam = Annotated[
    str,
    Field(description="Short description or diff summary of the pending action."),
]
EventTypeParam = Annotated[
    str,
    Field(description="Client-reported event type: gate_bypass or gate_degraded."),
]


class MCPAuthMiddleware(Middleware):
    """Enforce principal capabilities on MCP write tools.

    REST routes are authorized in rest_api.py; this is the MCP-transport
    counterpart so the two surfaces share one policy and KB_AUTH_TOKEN actually
    protects MCP write tools (the gap the security review flagged). The resolved
    principal is stashed in a contextvar so the write-tool closures can apply the
    confidence clamp (auto-commit only when the principal holds that capability).
    """

    def __init__(self, registry: PrincipalRegistry) -> None:
        self._registry = registry

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        from fastmcp.exceptions import ToolError
        from fastmcp.server.dependencies import get_http_headers

        # Authorization is stripped by get_http_headers' default exclude set;
        # include it explicitly so bearer auth reaches the MCP transport.
        headers = get_http_headers(include={"authorization"})
        principal = self._registry.resolve(headers.get("authorization"))
        token = _current_principal.set(principal)
        try:
            name = context.message.name
            cap = WRITE_TOOL_CAPABILITY.get(name)
            if cap is not None:
                if not principal.has(cap):
                    raise ToolError(
                        f"unauthorized: principal '{principal.name}' lacks "
                        f"capability '{cap}' required by '{name}'"
                    )
            elif (name in AUTH_REQUIRED_TOOLS
                  and self._registry.auth_configured
                  and not principal.authenticated):
                # Observability/enforcement tools: require authentication when
                # configured, matching the REST gating of these surfaces.
                raise ToolError(
                    f"unauthorized: authentication required for '{name}'"
                )
            return await call_next(context)
        finally:
            _current_principal.reset(token)


class ServerState:
    """Mutable runtime state shared across tool calls."""

    def __init__(
        self,
        *,
        idx: Index,
        git: GitOps,
        config: Config,
        worktrees: WorktreeRegistry | None = None,
        push_queue: PushQueue | None = None,
        pending: PendingQueue | None = None,
        rate_limiter: SlidingWindowLimiter | None = None,
        gate_rate_limiter: SlidingWindowLimiter | None = None,
        blocklist: PathBlocklist | None = None,
        audit_log: AuditLog | None = None,
        classifier: IntentClassifier | None = None,
        ledger: ConsultationLedger | None = None,
    ) -> None:
        self.idx = idx
        self.git = git
        self.config = config
        self.last_git_pull_at: float | None = None
        self.last_git_push_at: float | None = None
        # Sync-failure visibility (see refresh.git_pull_loop).
        self.last_git_fetch_status: str = "no_change"
        self.last_git_fetch_error: str | None = None
        self.last_git_fetch_at: float | None = None
        self.last_successful_refresh_at: float | None = None
        self.remote_head_sha: str | None = None
        self.last_index_build_status: str = "ok"
        self.last_index_error: str | None = None
        self.last_index_error_at: float | None = None
        self.last_index_conflicts: list[dict[str, object]] = []
        self.worktrees: WorktreeRegistry | None = worktrees
        self.push_queue: PushQueue | None = push_queue
        self.pending: PendingQueue | None = pending
        self.rate_limiter: SlidingWindowLimiter | None = rate_limiter
        # Separate limiter for /api/v1/gate/check (None = unthrottled, the default).
        # Kept distinct from `rate_limiter` so gate-check traffic and the write /
        # consult / cleanup-plan quota never share a bucket.
        self.gate_rate_limiter: SlidingWindowLimiter | None = gate_rate_limiter
        self.blocklist: PathBlocklist | None = blocklist
        self.audit_log: AuditLog | None = audit_log
        # Process-wide write serializer (scope item 1): one instance shared by
        # every write path so the write -> add -> commit -> enqueue critical
        # section never interleaves across the REST threadpool and the MCP
        # off-loop tool executor.
        from data_olympus.write_gate import WriteSerializer
        self.write_serializer = WriteSerializer()
        self.classifier: IntentClassifier = classifier or IntentClassifier()
        self.ledger: ConsultationLedger = ledger or ConsultationLedger()
        # Set at serve time (main) to observe the live streamable-http session
        # count. None until then / in tests, so health reports live_sessions=None
        # rather than a misleading 0. See session_metrics.
        self.session_count_provider: Callable[[], int | None] | None = None

    @property
    def pending_count(self) -> int:
        """Live count of entries in the pending queue. Computed on read so it can
        never drift from the queue's actual contents (the previous static
        attribute was initialised to 0 and never updated, so health reported 0
        forever). 0 when the pending queue is not initialised (read-only replica
        / tests)."""
        if self.pending is None:
            return 0
        try:
            return self.pending.size()
        except Exception:  # pragma: no cover - defensive; health must not crash
            return 0

    @property
    def push_queue_size(self) -> int:
        """Live count of entries in the push queue (see pending_count for why
        this is computed rather than stored). 0 when the push queue is not
        initialised."""
        if self.push_queue is None:
            return 0
        try:
            return self.push_queue.size()
        except Exception:  # pragma: no cover - defensive; health must not crash
            return 0

    @property
    def push_queue_frozen(self) -> int:
        """Live count of frozen push-queue entries (hit max_attempts, skipped by
        the retry loop). A nonzero value means writes are stuck and need
        operator intervention. 0 when the push queue is not initialised."""
        if self.push_queue is None:
            return 0
        try:
            return self.push_queue.frozen_count()
        except Exception:  # pragma: no cover - defensive; health must not crash
            return 0

    def record_pull(self, ts: float) -> None:
        self.last_git_pull_at = ts

    def live_session_count(self) -> int | None:
        """Best-effort live streamable-http session count for health. None when
        no provider is wired (in-memory tests, pre-serve) or on any error."""
        provider = self.session_count_provider
        if provider is None:
            return None
        try:
            return provider()
        except Exception:  # pragma: no cover - defensive; must not break health
            return None


def build_app(
    *,
    kb_main_path: Path,
    kb_index_path: Path,
    sync_interval_sec: int,
    staleness_degraded_sec: int,
    bootstrap_now: bool = False,
    kb_remote_url: str = "",
    worktree_root: str = "/kb-worktrees",
    pending_root: str = "/state/pending",
    push_queue_root: str = "/state/push-queue",
    audit_log_path: str | None = None,
    write_block_tiers: list[str] | None = None,
    write_block_paths: list[str] | None = None,
    confidence_threshold: float = 0.85,
    rate_limit_per_hour: int = 100,
    rate_limit_per_ip_per_hour: int = 0,
    gate_check_rate_limit_per_hour: int = 0,
    max_text_bytes: int = 262144,
    max_postimage_bytes: int = 1048576,
    max_body_bytes: int = 2097152,
    max_bootstrap_files: int = 50,
    pending_timeout_sec: int = 86400,
    pending_queue_cap: int = 100,
    worktree_idle_sec: int = 3600,
    git_key_path: str = "/tmp/git-key",
    auth_token: str = "",
    auth_principals: list[dict[str, Any]] | None = None,
    audit_hmac_key: str = "",
    audit_max_bytes: int = 0,
    ledger_path: str | None = None,
    session_idle_timeout_sec: int = 300,
    session_reap_interval_sec: int = 60,
    session_touch_interval_sec: int = 30,
    status_weights: dict[str, float] | None = None,
    read_only: bool = False,
    embeddings_enabled: bool = False,
    embeddings_weight: float = 0.35,
    embeddings_model: str = "BAAI/bge-small-en-v1.5",
    maintenance_ledger_path: str = "tooling/maintenance-ledger.md",
    maintenance_recently_expired_days: int = 30,
    maintenance_expiring_soon_days: int = 30,
) -> FastMCP:
    """Construct a FastMCP app with the read tools registered.

    If `bootstrap_now` is True (used in tests), the index is built synchronously
    before returning. In production main(), bootstrap happens via the lifespan hook.

    When `read_only` is True the server runs as a scaling replica (issue #44):
    only the read tools and read REST routes are registered, the write pipeline
    (worktrees / push queue / pending) is not initialised, and no write or
    enforcement-write tools/routes are exposed. The git_pull_loop still runs (in
    main()) so the replica refreshes its own index snapshot from the git remote.
    """
    config_kwargs: dict[str, object] = dict(
        kb_main_path=kb_main_path,
        kb_index_path=kb_index_path,
        kb_remote_url=kb_remote_url,
        sync_interval_sec=sync_interval_sec,
        staleness_degraded_sec=staleness_degraded_sec,
        confidence_threshold=confidence_threshold,
        http_port=8080,
        worktree_root=worktree_root,
        pending_root=pending_root,
        push_queue_root=push_queue_root,
        write_block_tiers=write_block_tiers or [],
        write_block_paths=write_block_paths or [],
        rate_limit_per_hour=rate_limit_per_hour,
        rate_limit_per_ip_per_hour=rate_limit_per_ip_per_hour,
        gate_check_rate_limit_per_hour=gate_check_rate_limit_per_hour,
        max_text_bytes=max_text_bytes,
        max_postimage_bytes=max_postimage_bytes,
        max_body_bytes=max_body_bytes,
        max_bootstrap_files=max_bootstrap_files,
        pending_timeout_sec=pending_timeout_sec,
        pending_queue_cap=pending_queue_cap,
        worktree_idle_sec=worktree_idle_sec,
        git_key_path=git_key_path,
        auth_token=auth_token,
        auth_principals=auth_principals or [],
        audit_hmac_key=audit_hmac_key,
        audit_max_bytes=audit_max_bytes,
        session_idle_timeout_sec=session_idle_timeout_sec,
        session_reap_interval_sec=session_reap_interval_sec,
        session_touch_interval_sec=session_touch_interval_sec,
        status_weights=status_weights,
        read_only=read_only,
        embeddings_enabled=embeddings_enabled,
        embeddings_weight=embeddings_weight,
        embeddings_model=embeddings_model,
        maintenance_ledger_path=maintenance_ledger_path,
        maintenance_recently_expired_days=maintenance_recently_expired_days,
        maintenance_expiring_soon_days=maintenance_expiring_soon_days,
    )
    if audit_log_path is not None:
        config_kwargs["audit_log_path"] = audit_log_path
    config = Config(**config_kwargs)  # type: ignore[arg-type]
    # Composed search wiring. Expand-query seam: synonym/acronym expansion (issue
    # #38, KB_SYNONYMS / KB_SYNONYMS_MODE) composed with corpus co-occurrence
    # expansion (issue #40, KB_COOCCURRENCE_*). Synonyms run FIRST, then
    # co-occurrence broadens the synonym-expanded set from the corpus-learned
    # related_terms table; the composed output is de-duplicated and bounded.
    # Co-occurrence is bound to this Index (it reads the swapped related_terms
    # table), so we build ``idx`` first and set the composed expander after.
    # Re-rank seam: the exact-id / exact-tag short-circuit (issue #39) runs
    # outermost -- a single-token query that is a document id or an exact tag
    # ranks that document first -- and delegates to the status-aware reranker
    # (issue #37) as its ``inner``, so among the remaining hits an active doc
    # still outranks the superseded one it replaced. status_weights=None uses the
    # built-in map; KB_STATUS_WEIGHTS overrides it.
    # Embeddings (issue #42) are resolved ONCE here from Config (reviewer concern
    # 2), never re-read from env on the enabled path. The resolved EmbeddingsConfig
    # and the shared embedder are threaded into the Index so build() embeds the
    # corpus and search()'s dense candidate source both honour the programmatic
    # Config, not KB_EMBEDDINGS_* env. The embedder is loaded once (loud failure if
    # enabled but unavailable) and reused by the hybrid reranker below.
    emb_config: EmbeddingsConfig | None = None
    embedder = None
    if config.embeddings_enabled:
        emb_config = EmbeddingsConfig(
            model_name=config.embeddings_model, weight=config.embeddings_weight
        )
        embedder = build_embedder(emb_config)
    idx = Index(
        kb_index_path,
        trigram_fallback=config.trigram_fallback_enabled,
        trigram_fallback_threshold=config.trigram_fallback_threshold,
        embeddings=emb_config,
        embedder=embedder,
        maintenance_ledger_path=config.maintenance_ledger_path,
        maintenance_recently_expired_days=config.maintenance_recently_expired_days,
        maintenance_expiring_soon_days=config.maintenance_expiring_soon_days,
    )
    synonym_expander = default_query_expander()
    cooc_expander = idx.cooccurrence_expander() if cooccurrence_enabled() else None
    idx.query_expander = compose_expanders(
        synonym_expander, cooc_expander, max_terms=DEFAULT_MAX_TERMS
    )
    # Re-rank stack (innermost first): status prior -> optional hybrid embedding
    # blend (issue #42) -> id/tag short-circuit (outermost). The hybrid layer,
    # when enabled, blends normalised bm25 with query-doc cosine over the
    # candidate pool; it is composed as the ``inner`` of the id/tag short-circuit
    # so an exact id/tag still wins, and it wraps the status reranker so an active
    # doc still outranks the superseded one among semantically-blended hits. When
    # disabled the stack is exactly today's (status inner, id/tag outer). The
    # embedder is loaded once here (loud failure if enabled but unavailable).
    status_reranker = make_status_reranker(config.status_weights)
    inner_reranker = status_reranker
    if config.embeddings_enabled and embedder is not None:
        hybrid_reranker = idx.make_hybrid_reranker(
            embedder, weight=config.embeddings_weight
        )

        def _status_then_hybrid(
            query: str, hits: list[SearchHit]
        ) -> list[SearchHit]:
            # Status prior first (so an active doc's boost feeds the blend's bm25
            # component), then the semantic blend re-sorts the pool.
            return hybrid_reranker(query, list(status_reranker(query, hits)))

        inner_reranker = _status_then_hybrid
    idx.reranker = make_id_tag_reranker(idx, inner=inner_reranker)
    git = GitOps(kb_main_path)
    # retention_sec = the consult TTL: an entry older than that can never be
    # fresh, so it is safe to evict and keeps the ledger bounded (see
    # ConsultationLedger). is_fresh is always called with this same ttl.
    ledger = ConsultationLedger(
        path=ledger_path, retention_sec=float(config.consult_ttl_sec)
    )
    state = ServerState(idx=idx, git=git, config=config, ledger=ledger)

    if config.read_only:
        # Unambiguous startup marker: if a replica is misconfigured (e.g. built
        # from an image that predates KB_READ_ONLY, or the flag failed to reach
        # the pod), this line is absent from the logs and the misconfiguration
        # is visible. A read-only replica never initialises the write pipeline
        # below and never becomes a git writer.
        log.warning("starting in READ-ONLY mode; write pipeline disabled")

    # A read-only replica sets kb_remote_url (so the git_pull_loop has a remote
    # to refresh from) but must NOT bring up the write pipeline.
    if config.kb_remote_url and not config.read_only:
        worktrees = WorktreeRegistry(git=git, worktree_root=config.worktree_root)
        push_queue = PushQueue(queue_root=config.push_queue_root)
        pending = PendingQueue(
            pending_root=config.pending_root, cap=config.pending_queue_cap
        )
        rate_limiter = SlidingWindowLimiter(
            max_per_hour=config.rate_limit_per_hour,
            max_per_ip_per_hour=config.rate_limit_per_ip_per_hour,
        )
        # gate/check gets its own limiter only when a positive ceiling is set;
        # 0 (default) leaves it unthrottled (see Config.gate_check_rate_limit_per_hour).
        gate_rate_limiter = (
            SlidingWindowLimiter(max_per_hour=config.gate_check_rate_limit_per_hour)
            if config.gate_check_rate_limit_per_hour > 0
            else None
        )
        blocklist = PathBlocklist(
            tier_blocks=config.write_block_tiers,
            path_blocks=config.write_block_paths,
        )
        state.worktrees = worktrees
        state.push_queue = push_queue
        state.pending = pending
        state.rate_limiter = rate_limiter
        state.gate_rate_limiter = gate_rate_limiter
        state.blocklist = blocklist

    if config.kb_remote_url and not config.read_only:
        audit_log = AuditLog(
            log_path=audit_log_path or config.audit_log_path,
            hmac_key=config.audit_hmac_key,
            max_bytes=config.audit_max_bytes,
        )
        state.audit_log = audit_log

    if bootstrap_now:
        sha = git.head_sha() if (kb_main_path / ".git").exists() else "no-git"
        idx.build(kb_main_path, source_commit=sha)
        state.record_pull(time.time())

    app: FastMCP = FastMCP(name="data-olympus-mcp")

    @app.tool(title="KB Health", annotations=READ_ONLY_TOOL)
    def kb_health(verbose: VerboseParam = False) -> dict[str, object]:
        """Return service health: kb_commit, index_built_at, staleness, degraded flag,
        and write-side state (pending_count, push_queue_size, last_index_*).

        verbose: False (default) returns a token-compact shape that keeps the core
        snapshot and OMITS diagnostic fields that are null/empty (e.g.
        last_index_error, remote_head_sha when unset). verbose=True returns every
        field including the nulls.

        pending_actions, when present, lists open maintenance items (missing
        `status` fields, recently-expired/expiring-soon docs) computed at the
        last index build; it is omitted when the corpus is clean. Surface it
        to the operator and act on it only with operator confirmation."""
        resp = kb_health_fn(
            idx=state.idx,
            last_git_pull_at=state.last_git_pull_at,
            staleness_degraded_sec=state.config.staleness_degraded_sec,
            last_git_push_at=state.last_git_push_at,
            pending_count=state.pending_count,
            push_queue_size=state.push_queue_size,
            push_queue_frozen=state.push_queue_frozen,
            last_index_build_status=state.last_index_build_status,
            last_index_error=state.last_index_error,
            last_index_error_at=state.last_index_error_at,
            last_index_conflicts=state.last_index_conflicts,
            path_locks_held=state.pending.locks_held() if state.pending else 0,
            last_git_fetch_status=state.last_git_fetch_status,
            last_git_fetch_error=state.last_git_fetch_error,
            last_git_fetch_at=state.last_git_fetch_at,
            last_successful_refresh_at=state.last_successful_refresh_at,
            remote_head_sha=state.remote_head_sha,
            live_sessions=state.live_session_count(),
        )
        return shape_response(resp, verbose=verbose)

    @app.tool(title="KB Outline", annotations=READ_ONLY_TOOL)
    def kb_outline(verbose: VerboseParam = False) -> dict[str, object]:
        """Return the tree of tiers and categories with doc counts.

        verbose: kb_outline is already lean, so compact and full modes return the
        same shape; the parameter exists for interface consistency."""
        resp = kb_outline_fn(idx=state.idx)
        return shape_response(resp, verbose=verbose)

    @app.tool(title="KB Search", annotations=READ_ONLY_TOOL)
    def kb_search(
        query: QueryParam,
        limit: LimitParam = 20,
        tier: TierParam = None,
        category: CategoryParam = None,
        status: StatusParam = None,
        in_force: InForceParam = False,
        doc_type: DocTypeParam = None,
        abstain: AbstainParam = False,
        include_expired: IncludeExpiredParam = False,
        validity_state: ValidityStateParam = None,
        verbose: VerboseParam = False,
    ) -> dict[str, object]:
        """Full-text search across the KB.

        Optional tier/category/status/type filters (status e.g. 'active',
        doc_type e.g. 'decision'). Returns ranked hits with snippets.

        in_force: when true, HARD-filter to the in-force status class
        (active/accepted/approved) AND the validity window (not expired, not
        upcoming) before ranking, EXCLUDING superseded/deprecated/expired/
        upcoming docs rather than only soft-downranking them. Composes with an
        explicit `status` (both must hold). Use this when you want only guidance
        that currently applies.

        A doc past its `valid_until` date is EXCLUDED from every default search
        result (not just `in_force=True`): an expired doc has no named successor
        to outrank it, so left visible it could be the top hit and would govern.
        Set `include_expired=true` to see it anyway; it then carries
        `freshness: "expired"`. A doc with a future `valid_from` ("upcoming")
        stays visible in default search, flagged `freshness: "upcoming"`; only
        `in_force=true` excludes it. `validity_state` is an audit-query facet:
        one of `"expired"`, `"stale"`, or `"expiring_within:N"` (N days) to list
        docs by validity condition; filtering for `"expired"` implies including
        them regardless of `include_expired`.

        abstain: when true, apply the signal gate. If the query matches no
        discriminating column (title/tags/applies_when) it is treated as
        out-of-scope and the search returns NO hits with `abstained: true` and an
        `abstain_reason`, instead of surfacing a weak keyword match. A query with
        a real signal retrieves normally. Distinguish `abstained: true` (no
        governing rule) from an ordinary empty result (`abstained: false`).

        verbose: False (default) returns a token-compact shape. Each hit is
        {id, title, snippet} plus `status` only when a hit is NOT in-force
        (superseded/deprecated), `type` when set, and `freshness` only when a
        hit deviates (`stale`/`expired`/`upcoming`); the `query` echo, per-hit
        `path`, and `score` are dropped (fetch a hit's full metadata with
        kb_get(id); array order conveys rank). A compact hit additionally
        carries `in_force: false` when the computed in-force predicate (the
        single-sourced status + validity-window + not-inbox rule; never
        stored in frontmatter) says the doc does NOT currently govern --
        emitted deviation-only, so an in-force hit's compact shape is
        unchanged. verbose=True restores the full legacy shape with query,
        path, score, status, type, freshness, and the computed
        `in_force: bool` on every hit, so a hit retrieved WITHOUT
        `in_force=true` can still be checked for whether it may govern now.
        """
        resp = kb_search_fn(
            idx=state.idx, query=query, limit=limit, tier=tier, category=category,
            status=status, in_force=in_force, doc_type=doc_type, abstain=abstain,
            include_expired=include_expired, validity_state=validity_state,
        )
        return shape_response(resp, verbose=verbose)

    @app.tool(title="KB Get Document", annotations=READ_ONLY_TOOL)
    def kb_get(id: DocumentIdParam, verbose: VerboseParam = False) -> dict[str, object]:
        """Retrieve a document by id (STD-U-001, ADR-002, T-NNN, etc.).
        Returns the full content markdown plus metadata.

        Always resolves regardless of expiry (ids never dangle): an expired
        document is still returned, with its full `validity` object and a
        computed `freshness` indicator (`stale`/`expired`/`upcoming`).

        verbose: False (default) returns the full `content_markdown` body (kb_get
        exists to read the doc) with a trimmed envelope: `path`,
        `git_remote_url`, and `last_modified_source` are dropped and empty
        status/type/applies_when/description/validity/freshness are omitted;
        `source_commit` and `last_modified` provenance are kept, and
        `in_force: false` is emitted when the computed in-force predicate says
        the doc does NOT currently govern (deviation-only; an in-force doc
        omits the key). verbose=True returns the full legacy envelope with
        every field plus the computed `in_force: bool` (the single-sourced
        status + validity-window + not-inbox predicate; never stored in
        frontmatter)."""
        from data_olympus.tools_read import KbNotFoundError, kb_get_fn
        try:
            resp = kb_get_fn(idx=state.idx, id=id)
        except KbNotFoundError as e:
            return {"error": "not_found", "message": str(e)}
        return shape_response(resp, verbose=verbose)

    @app.tool(title="KB List Documents", annotations=READ_ONLY_TOOL)
    def kb_list(
        tier: RequiredTierParam, category: CategoryParam = None,
        verbose: VerboseParam = False,
    ) -> dict[str, object]:
        """List doc ids in the given tier (and optional category), ordered by id.

        verbose: False (default) drops per-entry `path` (fetch via kb_get(id)) and
        omits a null category. verbose=True restores the full shape with paths."""
        from data_olympus.tools_read import kb_list_fn
        resp = kb_list_fn(idx=state.idx, tier=tier, category=category)
        return shape_response(resp, verbose=verbose)

    @app.tool(title="KB Onboarding Status", annotations=READ_ONLY_TOOL)
    def kb_onboarding_status(
        workspace: WorkspaceParam, component: ComponentParam = None,
        workspace_remote_url: RemoteUrlParam = None,
        component_remote_url: RemoteUrlParam = None,
    ) -> dict[str, object]:
        """Compute onboarding status for a workspace + optional component.
        State is one of: absent, partial, onboarded, rename_candidate."""
        from data_olympus.tools_onboarding import kb_onboarding_status_fn
        resp = kb_onboarding_status_fn(
            idx=state.idx, workspace=workspace, component=component,
            workspace_remote_url=workspace_remote_url,
            component_remote_url=component_remote_url,
        )
        return resp.model_dump()

    def _mcp_rate_limited(*, gate: bool = False) -> dict[str, object] | None:
        """Apply a sliding-window limiter to an MCP enforcement-plane tool
        (WP0b item (b)).

        The REST consult / cleanup-plan routes throttle via ``_rate_limited``; the
        MCP tool paths did not, so an agent could hammer the classifier/ledger
        unbounded. Key on the resolved principal (there is no per-request remote
        addr on the MCP transport, so the limiter's per-agent quota is the
        meaningful dimension). Returns a rejection dict when over quota, else None.

        ``gate=True`` (kb_gate_check) consults ``state.gate_rate_limiter`` instead
        of the write limiter; that limiter is None by default, so the
        high-frequency freshness probe is unthrottled unless explicitly capped,
        matching the REST /api/v1/gate/check route. Skipped when the resolved
        limiter is None (read-only deploy, or gate-check with no ceiling set)."""
        limiter = state.gate_rate_limiter if gate else state.rate_limiter
        if limiter is None:
            return None
        principal_name = _current_principal.get().name
        if not limiter.allow(remote_addr="mcp", agent_identity=principal_name):
            return {"status": "rejected_rate_limited",
                    "error": "too many requests; retry later"}
        return None

    @app.tool(title="KB Cleanup Plan", annotations=READ_ONLY_TOOL)
    def kb_cleanup_plan(
        workspace: WorkspaceParam, local_files: LocalFilesParam,
        component: ComponentParam = None, jaccard_threshold: JaccardParam = 0.6,
    ) -> dict[str, object]:
        """Read-only. Classify local project-repo docs against KB content for this
        workspace/component and return thin-pointer replacements for duplicates.
        The agent applies confirmed edits locally; the server writes nothing."""
        if (throttled := _mcp_rate_limited()) is not None:
            return throttled
        from data_olympus.tools_onboarding import CleanupInputError, kb_cleanup_plan_fn
        try:
            resp = kb_cleanup_plan_fn(
                idx=state.idx, workspace=workspace, component=component,
                local_files=local_files, jaccard_threshold=jaccard_threshold,
                max_files=state.config.max_bootstrap_files,
                max_content_bytes=state.config.max_postimage_bytes,
            )
        except CleanupInputError as e:
            return {"status": "rejected_invalid_input", "error": str(e)}
        return resp.model_dump()

    if not read_only:
        # Write + enforcement-write surface. A read-only replica
        # (issue #44) exposes none of these tools.
        @app.tool(title="KB Propose Memory", annotations=PROPOSAL_WRITE_TOOL)
        def kb_propose_memory(
            text: TextParam, tags: TagsParam, source_session: SourceSessionParam,
            agent_identity: AgentIdentityParam, confidence: ConfidenceParam,
            evidence: EvidenceParam = None,
        ) -> dict[str, object]:
            """Propose a new memory file. High confidence auto-commits and
            enqueues for push; low confidence enters the pending queue for operator
            review.

            evidence: optional supporting-context strings (max 10 items, 500
            chars each), rendered into the memory's frontmatter and surfaced by
            kb_pending."""
            if state.worktrees is None or state.push_queue is None or state.pending is None:
                return {"status": "write_pipeline_disabled"}
            assert state.worktrees is not None
            assert state.push_queue is not None
            assert state.pending is not None
            assert state.rate_limiter is not None
            assert state.blocklist is not None
            from data_olympus.tools_write import kb_propose_memory_fn
            resp = kb_propose_memory_fn(
                text=text, tags=tags, source_session=source_session,
                agent_identity=agent_identity, confidence=confidence,
                confidence_threshold=state.config.confidence_threshold,
                worktrees=state.worktrees, push_queue=state.push_queue,
                pending=state.pending, rate_limiter=state.rate_limiter,
                blocklist=state.blocklist, remote_addr="mcp",
                audit_log=state.audit_log,
                can_auto_commit=_current_principal.get().can_auto_commit,
                max_text_bytes=state.config.max_text_bytes,
                serializer=state.write_serializer, idx=state.idx,
                evidence=evidence,
            )
            return resp.model_dump()

        @app.tool(title="KB Propose Edit", annotations=PROPOSAL_EDIT_TOOL)
        def kb_propose_edit(
            target_path: TargetPathParam, postimage: PostimageParam,
            base_commit: BaseCommitParam, base_blob_sha: BaseBlobShaParam,
            target_file_hash: TargetFileHashParam, reason: ReasonParam,
            source_session: SourceSessionParam, agent_identity: AgentIdentityParam,
            confidence: ConfidenceParam, evidence: EvidenceParam = None,
        ) -> dict[str, object]:
            """Propose an edit to an existing (or new) markdown file under an
            indexed tier. High confidence auto-commits + queues for push; low
            confidence enters the pending queue for operator review.

            evidence: optional supporting-context strings (max 10 items, 500
            chars each), persisted in pending meta / audit events and surfaced
            by kb_pending (not rendered into the postimage: unlike
            kb_propose_memory, the postimage here is caller-supplied verbatim)."""
            if state.worktrees is None or state.push_queue is None or state.pending is None:
                return {"status": "write_pipeline_disabled"}
            assert state.worktrees is not None
            assert state.push_queue is not None
            assert state.pending is not None
            assert state.rate_limiter is not None
            assert state.blocklist is not None
            from data_olympus.tools_write import kb_propose_edit_fn
            resp = kb_propose_edit_fn(
                target_path=target_path, postimage=postimage, base_commit=base_commit,
                base_blob_sha=base_blob_sha, target_file_hash=target_file_hash,
                reason=reason, source_session=source_session, agent_identity=agent_identity,
                confidence=confidence,
                confidence_threshold=state.config.confidence_threshold,
                worktrees=state.worktrees, push_queue=state.push_queue,
                pending=state.pending, rate_limiter=state.rate_limiter,
                blocklist=state.blocklist, remote_addr="mcp",
                audit_log=state.audit_log,
                can_auto_commit=_current_principal.get().can_auto_commit,
                max_postimage_bytes=state.config.max_postimage_bytes,
                serializer=state.write_serializer, idx=state.idx,
                evidence=evidence,
            )
            return resp.model_dump()

        @app.tool(title="KB Resolve Pending", annotations=DESTRUCTIVE_WRITE_TOOL)
        def kb_resolve_pending(
            pending_id: PendingIdParam, decision: DecisionParam,
            edited_text: EditedTextParam = None,
            source_session: SourceSessionParam = "operator-resolve",
            agent_identity: AgentIdentityParam = "operator",
            override_secret_scan: OverrideSecretScanParam = False,
        ) -> dict[str, object]:
            """Resolve a pending proposal: approve (optionally with edited text) or
            reject. Approval commits + enqueues for push.

            ``override_secret_scan``: operator-only override of the secret-
            scanning gate (issue #71). When True and the resolved postimage
            matches a credential pattern, the commit proceeds anyway instead
            of being rejected ``rejected_secret_detected``, and the audit
            event records that the override was used. Use only after
            confirming the flagged content is NOT a real credential."""
            if state.worktrees is None or state.push_queue is None or state.pending is None:
                return {"status": "write_pipeline_disabled"}
            assert state.worktrees is not None
            assert state.push_queue is not None
            assert state.pending is not None
            from data_olympus.tools_write import kb_resolve_pending_fn
            resp = kb_resolve_pending_fn(
                pending_id=pending_id, decision=decision, edited_text=edited_text,
                worktrees=state.worktrees, push_queue=state.push_queue,
                pending=state.pending,
                source_session=source_session, agent_identity=agent_identity,
                audit_log=state.audit_log,
                # WP0b item (a): the REST resolve path caps edited_text at
                # KB_MAX_POSTIMAGE_BYTES; the MCP path was uncapped. Wire the same
                # cap here so the two surfaces match.
                max_postimage_bytes=state.config.max_postimage_bytes,
                serializer=state.write_serializer, idx=state.idx,
                override_secret_scan=override_secret_scan,
            )
            return resp.model_dump()

        @app.tool(title="KB List Pending", annotations=READ_ONLY_TOOL)
        def kb_list_pending() -> dict[str, object]:
            """List currently pending proposals awaiting operator decision."""
            assert state.pending is not None
            from data_olympus.tools_write import kb_list_pending_fn
            resp = kb_list_pending_fn(pending=state.pending)
            return resp.model_dump()

        @app.tool(title="KB Audit", annotations=READ_ONLY_TOOL)
        def kb_audit(
            since: SinceParam = None, agent: AgentParam = None,
            status: EventStatusParam = None, limit: AuditLimitParam = 100,
        ) -> dict[str, object]:
            """Return recent audit events, most-recent first. Optional filters:
            since (unix ts), agent (agent_identity), status (event status)."""
            assert state.audit_log is not None
            from data_olympus.tools_audit import kb_audit_fn
            resp = kb_audit_fn(audit_log=state.audit_log, since=since,
                              agent=agent, status=status, limit=limit)
            return resp.model_dump()

        @app.tool(title="KB Session Recap", annotations=READ_ONLY_TOOL)
        def kb_session_recap(source_session: SourceSessionParam) -> dict[str, object]:
            """Read-only per-session write summary (issue #112 feedback loop):
            N committed, M demoted-to-pending, K rejected for source_session.
            Call this (or `kb pending`) whenever a write response indicated a
            demotion, to confirm the current tally before informing the
            operator."""
            if state.audit_log is None:
                from data_olympus.models import SessionRecapResponse
                return SessionRecapResponse(source_session=source_session).model_dump()
            from data_olympus.tools_recap import kb_session_recap_fn
            resp = kb_session_recap_fn(audit_log=state.audit_log, source_session=source_session)
            return resp.model_dump()

        @app.tool(title="KB Bootstrap Project", annotations=PROPOSAL_WRITE_TOOL)
        def kb_bootstrap_project(
            workspace: WorkspaceParam, files: BootstrapFilesParam,
            source_session: SourceSessionParam, agent_identity: AgentIdentityParam,
            confidence: ConfidenceParam, component: ComponentParam = None,
            workspace_remote_url: RemoteUrlParam = None,
            component_remote_url: RemoteUrlParam = None,
        ) -> dict[str, object]:
            """Bootstrap a new workspace/component. Only valid when status=absent
            or partial. High confidence commits atomically; low confidence
            enqueues pending."""
            if state.worktrees is None or state.push_queue is None or state.pending is None:
                return {"status": "write_pipeline_disabled"}
            assert state.worktrees is not None
            assert state.push_queue is not None
            assert state.pending is not None
            assert state.rate_limiter is not None
            assert state.blocklist is not None
            from data_olympus.tools_onboarding import kb_bootstrap_project_fn
            resp = kb_bootstrap_project_fn(
                idx=state.idx, workspace=workspace, component=component,
                workspace_remote_url=workspace_remote_url,
                component_remote_url=component_remote_url,
                files=files,
                source_session=source_session, agent_identity=agent_identity,
                confidence=confidence,
                confidence_threshold=state.config.confidence_threshold,
                worktrees=state.worktrees, push_queue=state.push_queue,
                pending=state.pending, rate_limiter=state.rate_limiter,
                blocklist=state.blocklist, audit_log=state.audit_log,
                remote_addr="mcp",
                can_auto_commit=_current_principal.get().can_auto_commit,
                max_postimage_bytes=state.config.max_postimage_bytes,
                max_files=state.config.max_bootstrap_files,
                serializer=state.write_serializer,
            )
            return resp.model_dump()

        @app.tool(title="KB Consult", annotations=STATEFUL_NONDESTRUCTIVE_TOOL)
        def kb_consult(
            workspace: WorkspaceParam, intent: QueryParam,
            source_session: SourceSessionParam,
            agent_identity: AgentIdentityParam, trigger: TriggerParam = "explicit",
        ) -> dict[str, object]:
            """Record a consultation for (source_session, workspace) and return the
            governing rules for the intent. Call before code/architectural work.
            trigger is 'explicit' (default: a deliberate consult, clears the gate)
            or 'prompt_hook' (an installer auto-consult: audited, never clears).

            Retrieval is hard-filtered to the in-force class (active/accepted/
            approved, within its validity window, and never a memory-inbox
            doc): an unreviewed proposed memory, a retired/superseded decision,
            an expired doc, or a legacy/forged inbox file is never returned as
            a governing rule.

            pending_actions, when present, lists open maintenance items (missing
            `status` fields, recently-expired/expiring-soon docs); omitted when
            the corpus is clean. Surface it to the operator and act on it only
            with operator confirmation."""
            if (throttled := _mcp_rate_limited()) is not None:
                return throttled
            import time as _time

            from data_olympus.tools_enforce import kb_consult_fn
            resp = kb_consult_fn(
                idx=state.idx, classifier=state.classifier, ledger=state.ledger,
                workspace=workspace, intent=intent, source_session=source_session,
                agent_identity=agent_identity,
                ttl_sec=state.config.consult_ttl_sec, now=_time.time(),
                audit_log=state.audit_log, pending_queue=state.pending,
                trigger=trigger,
            )
            return resp.model_dump(exclude_none=True)

        @app.tool(title="KB Gate Check", annotations=STATEFUL_NONDESTRUCTIVE_TOOL)
        def kb_gate_check(
            workspace: WorkspaceParam, session_id: SessionIdParam,
            tool_name: ToolNameParam, action_path: ActionPathParam = None,
            action_diff: ActionDiffParam = "",
        ) -> dict[str, object]:
            """Return a verdict (allow | consult_required) for a pending code action.
            Governed actions require a fresh consultation on record."""
            if (throttled := _mcp_rate_limited(gate=True)) is not None:
                return throttled
            import time as _time

            from data_olympus.tools_enforce import kb_gate_check_fn
            resp = kb_gate_check_fn(
                classifier=state.classifier, ledger=state.ledger,
                workspace=workspace, session_id=session_id, tool_name=tool_name,
                action_path=action_path, action_diff=action_diff,
                now=_time.time(), ttl_sec=state.config.consult_ttl_sec,
                audit_log=state.audit_log,
            )
            return resp.model_dump()

        @app.tool(title="KB Compliance", annotations=READ_ONLY_TOOL)
        def kb_compliance(
            since: SinceParam = None, agent: AgentParam = None,
        ) -> dict[str, object]:
            """Aggregate enforcement events (consult / gate_*) overall and per agent."""
            if state.audit_log is None:
                return {"counts": {}, "by_agent": {}}
            from data_olympus.tools_enforce import kb_compliance_fn
            resp = kb_compliance_fn(audit_log=state.audit_log, since=since, agent=agent)
            return resp.model_dump()

        @app.tool(title="KB Record Event", annotations=STATEFUL_NONDESTRUCTIVE_TOOL)
        def kb_record_event(
            event_type: EventTypeParam, workspace: WorkspaceParam,
            agent_identity: AgentIdentityParam, source_session: SourceSessionParam,
            reason: ReasonParam = "",
        ) -> dict[str, object]:
            """Record a gate_bypass or gate_degraded enforcement event in the audit."""
            if state.audit_log is None:
                return {"recorded": False, "event_type": event_type}
            import time as _time

            from data_olympus.tools_enforce import kb_record_event_fn
            try:
                resp = kb_record_event_fn(
                    audit_log=state.audit_log, event_type=event_type,
                    workspace=workspace, agent_identity=agent_identity,
                    source_session=source_session, reason=reason, now=_time.time())
            except ValueError as e:
                return {"recorded": False, "error": str(e)}
            return resp.model_dump()

    registry = PrincipalRegistry(auth_token=auth_token, principals=auth_principals)
    # MCP-transport auth: enforce write-tool capabilities (REST is enforced in
    # rest_api.py against the same registry).
    app.add_middleware(MCPAuthMiddleware(registry))

    from data_olympus.rest_api import register_routes
    register_routes(app, state, registry, read_only=read_only)

    from data_olympus.prompts import register_prompts
    register_prompts(app)
    # Attach state for lifespan to discover; not used by tests
    app._dolympus_state = state  # type: ignore[attr-defined]
    return app


def build_app_from_config(config: Config, *, bootstrap_now: bool = True) -> FastMCP:
    """Construct a FastMCP app from a fully-populated Config.

    This is the preferred call path for production (used by main()) and for
    integration tests that need env-driven config to reach the app state.
    Every Config field is threaded through to build_app, so no value is silently
    dropped or overridden by a hardcoded default.
    """
    return build_app(
        kb_main_path=config.kb_main_path,
        kb_index_path=config.kb_index_path,
        sync_interval_sec=config.sync_interval_sec,
        staleness_degraded_sec=config.staleness_degraded_sec,
        bootstrap_now=bootstrap_now,
        kb_remote_url=config.kb_remote_url,
        worktree_root=config.worktree_root,
        pending_root=config.pending_root,
        push_queue_root=config.push_queue_root,
        audit_log_path=config.audit_log_path,
        write_block_tiers=list(config.write_block_tiers),
        write_block_paths=list(config.write_block_paths),
        confidence_threshold=config.confidence_threshold,
        rate_limit_per_hour=config.rate_limit_per_hour,
        rate_limit_per_ip_per_hour=config.rate_limit_per_ip_per_hour,
        gate_check_rate_limit_per_hour=config.gate_check_rate_limit_per_hour,
        max_text_bytes=config.max_text_bytes,
        max_postimage_bytes=config.max_postimage_bytes,
        max_body_bytes=config.max_body_bytes,
        max_bootstrap_files=config.max_bootstrap_files,
        pending_timeout_sec=config.pending_timeout_sec,
        pending_queue_cap=config.pending_queue_cap,
        worktree_idle_sec=config.worktree_idle_sec,
        git_key_path=config.git_key_path,
        auth_token=config.auth_token,
        auth_principals=list(config.auth_principals),
        audit_hmac_key=config.audit_hmac_key,
        audit_max_bytes=config.audit_max_bytes,
        ledger_path=config.ledger_path,
        session_idle_timeout_sec=config.session_idle_timeout_sec,
        session_reap_interval_sec=config.session_reap_interval_sec,
        session_touch_interval_sec=config.session_touch_interval_sec,
        status_weights=config.status_weights,
        read_only=config.read_only,
        embeddings_enabled=config.embeddings_enabled,
        embeddings_weight=config.embeddings_weight,
        embeddings_model=config.embeddings_model,
        maintenance_ledger_path=config.maintenance_ledger_path,
        maintenance_recently_expired_days=config.maintenance_recently_expired_days,
        maintenance_expiring_soon_days=config.maintenance_expiring_soon_days,
    )


def _uvicorn_proxy_kwargs(trusted_proxies: list[str]) -> dict[str, Any]:
    """Build the uvicorn proxy-header kwargs from the trusted-proxy list.

    Empty list -> ``{"proxy_headers": False}``: X-Forwarded-For is ignored and
    ``request.client.host`` is the immediate peer, so a client cannot spoof its
    address to evade the per-IP rate limiter. This is the safe default.

    Non-empty -> enable ``proxy_headers`` and set ``forwarded_allow_ips`` to the
    trusted set so uvicorn rewrites ``remote_addr`` from XFF ONLY when the peer is
    one of those proxies. ``["*"]`` trusts any peer (only safe when nothing
    untrusted can reach the port directly). uvicorn expects a comma-separated
    string for ``forwarded_allow_ips``."""
    if not trusted_proxies:
        return {"proxy_headers": False}
    return {
        "proxy_headers": True,
        "forwarded_allow_ips": ",".join(trusted_proxies),
    }


def _ensure_git_identity() -> None:
    """Give git a default author/committer identity when none is configured
    (scope item 10).

    The Docker entrypoint exports these, but a bare ``main()`` (local run, a
    minimal container, a k8s image that predates the entrypoint change) may not.
    Without an identity every ``git commit`` on the write path fails with "Please
    tell me who you are". We set env defaults (overridable by
    KB_GIT_AUTHOR_NAME / KB_GIT_AUTHOR_EMAIL, or by a pre-existing GIT_* value or
    a real git config) so the shipped artifact commits out of the box. Only fills
    unset vars, so a real operator identity is never clobbered."""
    import os as _os

    name = _os.environ.get("KB_GIT_AUTHOR_NAME", "data-olympus-mcp")
    email = _os.environ.get("KB_GIT_AUTHOR_EMAIL", "data-olympus-mcp@localhost")
    for var, value in (
        ("GIT_AUTHOR_NAME", name), ("GIT_AUTHOR_EMAIL", email),
        ("GIT_COMMITTER_NAME", name), ("GIT_COMMITTER_EMAIL", email),
    ):
        _os.environ.setdefault(var, value)


def main() -> None:
    """Production entry. Loads config from env, bootstraps index, starts HTTP server
    with the git_pull_loop refresh task running in the background."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="data-olympus-mcp",
        description=(
            "Run the data-olympus MCP + REST server. All configuration is via "
            "KB_* environment variables (see docs/serving.md); there are no "
            "positional arguments. Start a local instance with scripts/run-local.sh."
        ),
    )
    # Parsing first means `data-olympus-mcp --help` prints usage and exits 0
    # without loading config (which would otherwise fail with NotADirectoryError
    # when KB_MAIN_PATH does not exist).
    parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    _ensure_git_identity()
    config = load_config()
    app = build_app_from_config(config, bootstrap_now=True)
    # The state lives inside build_app's closure; expose via app attribute for the lifespan task
    state = app._dolympus_state  # type: ignore[attr-defined]  # set in build_app
    log.info(
        "starting streamable HTTP MCP on port %s (mode=%s)",
        config.http_port,
        "read-only replica" if config.read_only else "read-write",
    )

    # Build the streamable-http ASGI app explicitly (rather than app.run_async)
    # so we own the app object: we install the session-activity middleware, wire
    # the live-session count into health, and run the idle-session reaper. See
    # session_metrics for why FastMCP's default wiring never reaps sessions.
    from starlette.middleware import Middleware as StarletteMiddleware

    from data_olympus.session_metrics import (
        SessionActivityMiddleware,
        SessionActivityTracker,
        count_live_sessions,
    )

    tracker = SessionActivityTracker()
    # Re-stamp an in-flight SSE session at least 3x per idle window, so a
    # quiet-but-connected client is never reaped even if an operator sets a short
    # idle timeout. Falls back to the configured interval when the idle window is
    # generous.
    _idle = config.session_idle_timeout_sec
    _touch_interval = float(config.session_touch_interval_sec)
    if _idle > 0:
        # Always strictly below the idle window (>= 3 touches per window) even for
        # a tiny idle value; a small positive floor avoids a zero/negative sleep.
        _touch_interval = max(0.1, min(_touch_interval, _idle / 3))
    http_app = app.http_app(
        transport="streamable-http",
        middleware=[
            StarletteMiddleware(
                SessionActivityMiddleware,
                tracker=tracker,
                touch_interval_sec=_touch_interval,
            )
        ],
    )
    # Health surfaces the live session count; the manager is only reachable once
    # the lifespan has started, so this returns None before then (never raises).
    state.session_count_provider = lambda: count_live_sessions(http_app)

    async def runner() -> None:
        import uvicorn

        from data_olympus.refresh import (
            git_pull_loop,
            pending_gc_loop,
            push_retry_loop,
            worktree_gc_loop,
        )
        from data_olympus.session_metrics import session_reaper_loop

        # Startup recovery: a crash between `git commit` and `push_queue.enqueue`
        # (tools_write) leaves a committed-but-unqueued orphan on a session
        # worktree branch that no loop would ever push. Before the push-retry
        # loop starts, scan every session worktree and re-enqueue any commit
        # reachable from its HEAD but not from origin/main. init_recovery skips
        # shas already queued, so this cannot double-enqueue.
        if state.push_queue is not None and state.worktrees is not None:
            try:
                before = state.push_queue.size()
                state.push_queue.init_recovery(
                    worktree_root=config.worktree_root,
                    list_unpushed_shas=state.git.list_unpushed_shas,
                )
                recovered = state.push_queue.size() - before
                if recovered > 0:
                    log.warning(
                        "startup push recovery re-enqueued %d orphaned "
                        "commit(s) from session worktrees under %s",
                        recovered, config.worktree_root,
                    )
                else:
                    log.info("startup push recovery: no orphaned commits found")
            except Exception:
                log.exception("startup push recovery failed (continuing)")

        # Startup lock recovery: a hard kill while an auto-commit held a per-path
        # lock leaves it on the /state volume with no in-process holder to release
        # it, wedging that path (rejected_path_lock_busy) until manual cleanup. A
        # fresh process provably holds no auto-commit lock, so reclaim every
        # auto-commit lock unconditionally (max_age_sec=0). Pending-proposal locks
        # are untouched: they legitimately outlive a restart.
        if state.pending is not None:
            try:
                reclaimed = state.pending.reclaim_stale_auto_commit_locks(
                    max_age_sec=0,
                )
                if reclaimed > 0:
                    log.warning(
                        "startup reclaimed %d stale auto-commit path lock(s)",
                        reclaimed,
                    )
            except Exception:
                log.exception("startup auto-commit lock reclaim failed (continuing)")

        tasks = [
            asyncio.create_task(
                git_pull_loop(state, config.sync_interval_sec),
                name="git_pull_loop",
            ),
        ]
        if state.push_queue is not None:
            tasks.append(asyncio.create_task(
                push_retry_loop(
                    push_queue=state.push_queue,
                    git=state.git,
                    interval_sec=30,
                    # Non-FF recovery (scope item 2): a rebase conflict demotes the
                    # commit to a pending entry via these; without them the loop
                    # falls back to counting the conflict as a retryable failure.
                    pending=state.pending,
                    audit_log=state.audit_log,
                ),
                name="push_retry_loop",
            ))
        if state.worktrees is not None:
            tasks.append(asyncio.create_task(
                worktree_gc_loop(
                    worktrees=state.worktrees,
                    idle_sec=config.worktree_idle_sec,
                    interval_sec=300,
                ),
                name="worktree_gc_loop",
            ))
        if state.pending is not None:
            tasks.append(asyncio.create_task(
                pending_gc_loop(
                    pending=state.pending,
                    timeout_sec=config.pending_timeout_sec,
                    interval_sec=300,
                    # Crash-orphaned auto-commit path locks older than this are
                    # reclaimed each pass so a hard kill mid-commit does not wedge
                    # the path with rejected_path_lock_busy forever.
                    auto_commit_lock_ttl_sec=config.auto_commit_lock_ttl_sec,
                    # The reclaim runs under the SAME write serializer that
                    # path_lock acquire/release runs under, so a stale holder that
                    # resumes cannot free+let-a-successor-acquire the path mid-scan.
                    write_serializer=state.write_serializer,
                    # Scope item 7: emit an audit event on each expiry instead of
                    # silently rejecting >24h entries.
                    audit_log=state.audit_log,
                ),
                name="pending_gc_loop",
            ))
        if config.session_idle_timeout_sec > 0:
            tasks.append(asyncio.create_task(
                session_reaper_loop(
                    app=http_app,
                    tracker=tracker,
                    idle_after_sec=config.session_idle_timeout_sec,
                    interval_sec=config.session_reap_interval_sec,
                ),
                name="session_reaper_loop",
            ))
        # Proxy-header handling (WP3a item 3). By default uvicorn ignores
        # X-Forwarded-For, so behind an ingress every client collapses to the
        # proxy's address and the per-IP rate limiter throttles all clients as
        # one. When KB_TRUSTED_PROXIES is set we enable proxy_headers and restrict
        # forwarded_allow_ips to those proxies, so uvicorn rewrites remote_addr
        # from XFF ONLY when the immediate peer is trusted (a direct client cannot
        # then spoof its IP). Empty (default) keeps proxy_headers OFF: safe by
        # default, no spoofing surface.
        uvicorn_kwargs = _uvicorn_proxy_kwargs(config.trusted_proxies)
        server = uvicorn.Server(
            uvicorn.Config(
                http_app, host="0.0.0.0", port=config.http_port, log_level="info",
                **uvicorn_kwargs,
            )
        )
        try:
            await server.serve()
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    asyncio.run(runner())


if __name__ == "__main__":
    main()
