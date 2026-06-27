"""FastMCP server entry. Streamable HTTP transport at the configured port."""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import time
from typing import TYPE_CHECKING

from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware

if TYPE_CHECKING:
    from pathlib import Path

    from fastmcp.server.middleware import MiddlewareContext

from data_olympus.audit_log import AuditLog
from data_olympus.auth import PathBlocklist
from data_olympus.config import Config, load_config
from data_olympus.enforce_policy import ConsultationLedger, IntentClassifier
from data_olympus.git_ops import GitOps
from data_olympus.index import Index
from data_olympus.pending import PendingQueue
from data_olympus.principals import (
    LOCAL_TRUSTED,
    WRITE_TOOL_CAPABILITY,
    Principal,
    PrincipalRegistry,
)
from data_olympus.push_queue import PushQueue
from data_olympus.rate_limit import SlidingWindowLimiter
from data_olympus.tools_read import kb_health_fn, kb_outline_fn, kb_search_fn
from data_olympus.worktrees import WorktreeRegistry

log = logging.getLogger("data_olympus")

# The principal resolved by the MCP auth middleware for the in-flight tool call.
# Defaults to the fully-trusted local principal so direct (non-HTTP) tool calls
# in tests behave as before. Read by the write-tool closures for the clamp.
_current_principal: contextvars.ContextVar[Principal] = contextvars.ContextVar(
    "current_principal", default=LOCAL_TRUSTED
)


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
        self, context: MiddlewareContext, call_next: object,
    ) -> object:
        from fastmcp.exceptions import ToolError
        from fastmcp.server.dependencies import get_http_headers

        # Authorization is stripped by get_http_headers' default exclude set;
        # include it explicitly so bearer auth reaches the MCP transport.
        headers = get_http_headers(include={"authorization"})
        principal = self._registry.resolve(headers.get("authorization"))
        token = _current_principal.set(principal)
        try:
            cap = WRITE_TOOL_CAPABILITY.get(context.message.name)
            if cap is not None and not principal.has(cap):
                raise ToolError(
                    f"unauthorized: principal '{principal.name}' lacks "
                    f"capability '{cap}' required by '{context.message.name}'"
                )
            return await call_next(context)  # type: ignore[operator]
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
        self.pending_count: int = 0
        self.push_queue_size: int = 0
        self.last_index_build_status: str = "ok"
        self.last_index_error: str | None = None
        self.last_index_error_at: float | None = None
        self.last_index_conflicts: list[dict[str, object]] = []
        self.worktrees: WorktreeRegistry | None = worktrees
        self.push_queue: PushQueue | None = push_queue
        self.pending: PendingQueue | None = pending
        self.rate_limiter: SlidingWindowLimiter | None = rate_limiter
        self.blocklist: PathBlocklist | None = blocklist
        self.audit_log: AuditLog | None = audit_log
        self.classifier: IntentClassifier = classifier or IntentClassifier()
        self.ledger: ConsultationLedger = ledger or ConsultationLedger()

    def record_pull(self, ts: float) -> None:
        self.last_git_pull_at = ts


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
    max_text_bytes: int = 262144,
    max_postimage_bytes: int = 1048576,
    max_body_bytes: int = 2097152,
    pending_timeout_sec: int = 86400,
    pending_queue_cap: int = 100,
    worktree_idle_sec: int = 3600,
    git_key_path: str = "/tmp/git-key",
    auth_token: str = "",
    auth_principals: list[dict] | None = None,
    ledger_path: str | None = None,
) -> FastMCP:
    """Construct a FastMCP app with the read tools registered.

    If `bootstrap_now` is True (used in tests), the index is built synchronously
    before returning. In production main(), bootstrap happens via the lifespan hook.
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
        max_text_bytes=max_text_bytes,
        max_postimage_bytes=max_postimage_bytes,
        max_body_bytes=max_body_bytes,
        pending_timeout_sec=pending_timeout_sec,
        pending_queue_cap=pending_queue_cap,
        worktree_idle_sec=worktree_idle_sec,
        git_key_path=git_key_path,
        auth_token=auth_token,
        auth_principals=auth_principals or [],
    )
    if audit_log_path is not None:
        config_kwargs["audit_log_path"] = audit_log_path
    config = Config(**config_kwargs)  # type: ignore[arg-type]
    idx = Index(kb_index_path)
    git = GitOps(kb_main_path)
    ledger = ConsultationLedger(path=ledger_path)
    state = ServerState(idx=idx, git=git, config=config, ledger=ledger)

    if config.kb_remote_url:
        worktrees = WorktreeRegistry(git=git, worktree_root=config.worktree_root)
        push_queue = PushQueue(queue_root=config.push_queue_root)
        pending = PendingQueue(pending_root=config.pending_root)
        rate_limiter = SlidingWindowLimiter(
            max_per_hour=config.rate_limit_per_hour,
            max_per_ip_per_hour=config.rate_limit_per_ip_per_hour,
        )
        blocklist = PathBlocklist(
            tier_blocks=config.write_block_tiers,
            path_blocks=config.write_block_paths,
        )
        state.worktrees = worktrees
        state.push_queue = push_queue
        state.pending = pending
        state.rate_limiter = rate_limiter
        state.blocklist = blocklist

    if config.kb_remote_url:
        audit_log = AuditLog(log_path=audit_log_path or config.audit_log_path)
        state.audit_log = audit_log

    if bootstrap_now:
        sha = git.head_sha() if (kb_main_path / ".git").exists() else "no-git"
        idx.build(kb_main_path, source_commit=sha)
        state.record_pull(time.time())

    app: FastMCP = FastMCP(name="data-olympus-mcp")

    @app.tool()
    def kb_health() -> dict[str, object]:
        """Return service health: kb_commit, index_built_at, staleness, degraded flag,
        and write-side state (pending_count, push_queue_size, last_index_*)."""
        resp = kb_health_fn(
            idx=state.idx,
            last_git_pull_at=state.last_git_pull_at,
            staleness_degraded_sec=state.config.staleness_degraded_sec,
            last_git_push_at=state.last_git_push_at,
            pending_count=state.pending_count,
            push_queue_size=state.push_queue_size,
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
        )
        return resp.model_dump()

    @app.tool()
    def kb_outline() -> dict[str, object]:
        """Return the tree of tiers and categories with doc counts."""
        resp = kb_outline_fn(idx=state.idx)
        return resp.model_dump()

    @app.tool()
    def kb_search(
        query: str,
        limit: int = 20,
        tier: str | None = None,
        category: str | None = None,
        status: str | None = None,
        doc_type: str | None = None,
    ) -> dict[str, object]:
        """Full-text search across the KB.

        Optional tier/category/status/type filters (status e.g. 'active',
        doc_type e.g. 'decision'). Returns ranked hits with snippets.
        """
        resp = kb_search_fn(
            idx=state.idx, query=query, limit=limit, tier=tier, category=category,
            status=status, doc_type=doc_type,
        )
        return resp.model_dump()

    @app.tool()
    def kb_get(id: str) -> dict[str, object]:
        """Retrieve a document by id (STD-U-001, ADR-002, T-NNN, etc.).
        Returns full content markdown plus metadata."""
        from data_olympus.tools_read import KbNotFoundError, kb_get_fn
        try:
            resp = kb_get_fn(idx=state.idx, id=id)
        except KbNotFoundError as e:
            return {"error": "not_found", "message": str(e)}
        return resp.model_dump()

    @app.tool()
    def kb_list(tier: str, category: str | None = None) -> dict[str, object]:
        """List doc ids in the given tier (and optional category), ordered by id."""
        from data_olympus.tools_read import kb_list_fn
        resp = kb_list_fn(idx=state.idx, tier=tier, category=category)
        return resp.model_dump()

    @app.tool()
    def kb_propose_memory(
        text: str, tags: list[str], source_session: str,
        agent_identity: str, confidence: float,
    ) -> dict[str, object]:
        """Propose a new memory file. High confidence auto-commits and
        enqueues for push; low confidence enters the pending queue for operator
        review."""
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
        )
        return resp.model_dump()

    @app.tool()
    def kb_propose_edit(
        target_path: str, postimage: str, base_commit: str,
        base_blob_sha: str | None, target_file_hash: str | None,
        reason: str, source_session: str, agent_identity: str, confidence: float,
    ) -> dict[str, object]:
        """Propose an edit to an existing (or new) markdown file under an
        indexed tier. High confidence auto-commits + queues for push; low
        confidence enters the pending queue for operator review."""
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
        )
        return resp.model_dump()

    @app.tool()
    def kb_resolve_pending(
        pending_id: str, decision: str, edited_text: str | None = None,
        source_session: str = "operator-resolve", agent_identity: str = "operator",
    ) -> dict[str, object]:
        """Resolve a pending proposal: approve (optionally with edited text) or
        reject. Approval commits + enqueues for push."""
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
        )
        return resp.model_dump()

    @app.tool()
    def kb_list_pending() -> dict[str, object]:
        """List currently pending proposals awaiting operator decision."""
        assert state.pending is not None
        from data_olympus.tools_write import kb_list_pending_fn
        resp = kb_list_pending_fn(pending=state.pending)
        return resp.model_dump()

    @app.tool()
    def kb_audit(
        since: float | None = None, agent: str | None = None,
        status: str | None = None, limit: int = 100,
    ) -> dict[str, object]:
        """Return recent audit events, most-recent first. Optional filters:
        since (unix ts), agent (agent_identity), status (event status)."""
        assert state.audit_log is not None
        from data_olympus.tools_audit import kb_audit_fn
        resp = kb_audit_fn(audit_log=state.audit_log, since=since,
                          agent=agent, status=status, limit=limit)
        return resp.model_dump()

    @app.tool()
    def kb_onboarding_status(
        workspace: str, component: str | None = None,
        workspace_remote_url: str | None = None,
        component_remote_url: str | None = None,
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

    @app.tool()
    def kb_bootstrap_project(
        workspace: str, files: list[dict[str, str]],
        source_session: str, agent_identity: str, confidence: float,
        component: str | None = None,
        workspace_remote_url: str | None = None,
        component_remote_url: str | None = None,
    ) -> dict[str, object]:
        """Bootstrap a new workspace/component. Only valid when status=absent.
        High confidence commits atomically; low confidence enqueues pending."""
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
        )
        return resp.model_dump()

    @app.tool()
    def kb_consult(
        workspace: str, intent: str, source_session: str,
        agent_identity: str,
    ) -> dict[str, object]:
        """Record a consultation for (source_session, workspace) and return the
        governing rules for the intent. Call before code/architectural work."""
        import time as _time

        from data_olympus.tools_enforce import kb_consult_fn
        resp = kb_consult_fn(
            idx=state.idx, classifier=state.classifier, ledger=state.ledger,
            workspace=workspace, intent=intent, source_session=source_session,
            agent_identity=agent_identity,
            ttl_sec=state.config.consult_ttl_sec, now=_time.time(),
            audit_log=state.audit_log,
        )
        return resp.model_dump()

    @app.tool()
    def kb_gate_check(
        workspace: str, session_id: str, tool_name: str,
        action_path: str | None = None, action_diff: str = "",
    ) -> dict[str, object]:
        """Return a verdict (allow | consult_required) for a pending code action.
        Governed actions require a fresh consultation on record."""
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

    @app.tool()
    def kb_compliance(
        since: float | None = None, agent: str | None = None,
    ) -> dict[str, object]:
        """Aggregate enforcement events (consult / gate_*) overall and per agent."""
        if state.audit_log is None:
            return {"counts": {}, "by_agent": {}}
        from data_olympus.tools_enforce import kb_compliance_fn
        resp = kb_compliance_fn(audit_log=state.audit_log, since=since, agent=agent)
        return resp.model_dump()

    @app.tool()
    def kb_record_event(
        event_type: str, workspace: str, agent_identity: str,
        source_session: str, reason: str = "",
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
    register_routes(app, state, registry)
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
        max_text_bytes=config.max_text_bytes,
        max_postimage_bytes=config.max_postimage_bytes,
        max_body_bytes=config.max_body_bytes,
        pending_timeout_sec=config.pending_timeout_sec,
        pending_queue_cap=config.pending_queue_cap,
        worktree_idle_sec=config.worktree_idle_sec,
        git_key_path=config.git_key_path,
        auth_token=config.auth_token,
        auth_principals=list(config.auth_principals),
        ledger_path=config.ledger_path,
    )


def main() -> None:
    """Production entry. Loads config from env, bootstraps index, starts HTTP server
    with the git_pull_loop refresh task running in the background."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    config = load_config()
    app = build_app_from_config(config, bootstrap_now=True)
    # The state lives inside build_app's closure; expose via app attribute for the lifespan task
    state = app._dolympus_state  # type: ignore[attr-defined]  # set in build_app
    log.info("starting streamable HTTP MCP on port %s", config.http_port)

    async def runner() -> None:
        from data_olympus.refresh import (
            git_pull_loop,
            pending_gc_loop,
            push_retry_loop,
        )
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
                ),
                name="push_retry_loop",
            ))
        if state.pending is not None:
            tasks.append(asyncio.create_task(
                pending_gc_loop(
                    pending=state.pending,
                    timeout_sec=config.pending_timeout_sec,
                    interval_sec=300,
                ),
                name="pending_gc_loop",
            ))
        try:
            await app.run_async(
                transport="streamable-http", host="0.0.0.0", port=config.http_port
            )
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    asyncio.run(runner())


if __name__ == "__main__":
    main()
