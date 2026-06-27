"""REST mirror at /api/v1/*. Custom routes mounted on the same FastMCP app
so MCP + REST share one Starlette/uvicorn process."""
from __future__ import annotations

import hmac
from typing import TYPE_CHECKING

from starlette.responses import JSONResponse

from data_olympus.tools_read import (
    KbNotFoundError,
    kb_get_fn,
    kb_health_fn,
    kb_list_fn,
    kb_outline_fn,
    kb_search_fn,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from starlette.requests import Request

    from data_olympus.models import HealthResponse
    from data_olympus.server import ServerState


def _build_health(state: ServerState) -> HealthResponse:
    """Compose the HealthResponse from current ServerState. Shared by /health and
    by the degraded-precheck on all other read endpoints."""
    return kb_health_fn(
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
    )


def _degraded_response(health: HealthResponse) -> JSONResponse:
    """503 + degraded:true body. Used on all read endpoints when the index is
    not healthy, so bin/kb --no-stale rejects stale reads across all subcommands."""
    body = health.model_dump()
    body["degraded"] = True
    body["error"] = "degraded_index"
    return JSONResponse(body, status_code=503)


def _check_auth(request: Request, auth_token: str) -> JSONResponse | None:
    """Return a 401 JSONResponse if auth_token is set and the request does not
    supply a matching ``Authorization: Bearer <token>`` header. Uses
    hmac.compare_digest for constant-time comparison.

    Returns None when auth is satisfied (token empty, or header matches).
    """
    if not auth_token:
        return None
    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not header.startswith(prefix):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    supplied = header[len(prefix):]
    if not hmac.compare_digest(supplied.encode(), auth_token.encode()):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return None


def _missing_fields_response(body: object, required: list[str]) -> JSONResponse | None:
    """Return a 400 JSONResponse naming the missing/null required field(s), or
    None when all are present and non-null.

    Without this guard a missing field raised KeyError inside the handler,
    surfacing as a plain-text HTTP 500 "Internal Server Error". The kb CLI fed
    that non-JSON body to jq and aborted with a parse error, so a client that
    forgot a field got an opaque crash instead of an actionable 400. An explicit
    JSON null is treated the same as absent, since the handlers would otherwise
    pass None straight into the write functions.
    """
    if not isinstance(body, dict):
        return JSONResponse(
            {"error": "bad_request", "message": "request body must be a JSON object"},
            status_code=400,
        )
    missing = [f for f in required if body.get(f) is None]
    if missing:
        return JSONResponse(
            {"error": "missing_field",
             "message": f"missing or null required field(s): {', '.join(missing)}"},
            status_code=400,
        )
    return None


def _parse_confidence(body: dict) -> tuple[float, JSONResponse | None]:
    """Coerce body['confidence'] to float, returning a 400 JSONResponse instead
    of letting a non-numeric value raise ValueError/TypeError -> HTTP 500 (the
    same opaque-crash class as a missing field). Presence/non-null is enforced
    separately by _missing_fields_response, so the key is assumed present here.
    """
    try:
        return float(body["confidence"]), None
    except (TypeError, ValueError):
        return 0.0, JSONResponse(
            {"error": "bad_request", "message": "confidence must be a number"},
            status_code=400,
        )


def register_routes(app: FastMCP, state: ServerState, auth_token: str = "") -> None:
    """Mount REST routes under /api/v1/ on the FastMCP app.

    Write routes require a valid ``Authorization: Bearer <token>`` header when
    ``auth_token`` is non-empty. Read routes are always open.
    """

    @app.custom_route("/api/v1/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        resp = _build_health(state)
        # Degraded health responses MUST return 503 so the
        # CLI's --no-stale contract (exit 2 on HTTP 200 or 503 degraded) is meaningful.
        status = 503 if resp.degraded else 200
        return JSONResponse(resp.model_dump(), status_code=status)

    @app.custom_route("/api/v1/outline", methods=["GET"])
    async def outline(_request: Request) -> JSONResponse:
        h = _build_health(state)
        if h.degraded:
            return _degraded_response(h)
        resp = kb_outline_fn(idx=state.idx)
        return JSONResponse(resp.model_dump())

    @app.custom_route("/api/v1/search", methods=["GET"])
    async def search(request: Request) -> JSONResponse:
        h = _build_health(state)
        if h.degraded:
            return _degraded_response(h)
        q = request.query_params.get("q", "")
        if not q:
            return JSONResponse({"error": "missing_q"}, status_code=400)
        try:
            limit = int(request.query_params.get("limit", "20"))
        except ValueError:
            return JSONResponse({"error": "bad_limit"}, status_code=400)
        tier = request.query_params.get("tier") or None
        category = request.query_params.get("category") or None
        resp = kb_search_fn(idx=state.idx, query=q, limit=limit, tier=tier, category=category)
        return JSONResponse(resp.model_dump())

    @app.custom_route("/api/v1/get/{id}", methods=["GET"])
    async def get(request: Request) -> JSONResponse:
        h = _build_health(state)
        if h.degraded:
            return _degraded_response(h)
        id_ = request.path_params["id"]
        try:
            resp = kb_get_fn(idx=state.idx, id=id_)
        except KbNotFoundError as e:
            return JSONResponse({"error": "not_found", "message": str(e)}, status_code=404)
        return JSONResponse(resp.model_dump())

    @app.custom_route("/api/v1/list", methods=["GET"])
    async def list_(request: Request) -> JSONResponse:
        h = _build_health(state)
        if h.degraded:
            return _degraded_response(h)
        tier = request.query_params.get("tier")
        if not tier:
            return JSONResponse({"error": "missing_tier"}, status_code=400)
        category = request.query_params.get("category") or None
        resp = kb_list_fn(idx=state.idx, tier=tier, category=category)
        return JSONResponse(resp.model_dump())

    @app.custom_route("/api/v1/propose/memory", methods=["POST"])
    async def propose_memory(request: Request) -> JSONResponse:
        if (denied := _check_auth(request, auth_token)) is not None:
            return denied
        body = await request.json()
        if (bad := _missing_fields_response(
            body, ["text", "source_session", "agent_identity", "confidence"],
        )) is not None:
            return bad
        confidence, bad = _parse_confidence(body)
        if bad is not None:
            return bad
        assert state.worktrees is not None
        assert state.push_queue is not None
        assert state.pending is not None
        assert state.rate_limiter is not None
        assert state.blocklist is not None
        from data_olympus.tools_write import kb_propose_memory_fn
        resp = kb_propose_memory_fn(
            text=body["text"], tags=body.get("tags", []),
            source_session=body["source_session"],
            agent_identity=body["agent_identity"],
            confidence=confidence,
            confidence_threshold=state.config.confidence_threshold,
            worktrees=state.worktrees, push_queue=state.push_queue,
            pending=state.pending, rate_limiter=state.rate_limiter,
            blocklist=state.blocklist,
            remote_addr=request.client.host if request.client else "unknown",
            audit_log=state.audit_log,
        )
        status = 201 if resp.status == "committed" else (
            202 if resp.status == "pending_confirmation" else 400
        )
        return JSONResponse(resp.model_dump(), status_code=status)

    @app.custom_route("/api/v1/propose/edit", methods=["POST"])
    async def propose_edit(request: Request) -> JSONResponse:
        if (denied := _check_auth(request, auth_token)) is not None:
            return denied
        body = await request.json()
        if (bad := _missing_fields_response(
            body,
            ["target_path", "postimage", "base_commit",
             "source_session", "agent_identity", "confidence"],
        )) is not None:
            return bad
        confidence, bad = _parse_confidence(body)
        if bad is not None:
            return bad
        assert state.worktrees is not None
        assert state.push_queue is not None
        assert state.pending is not None
        assert state.rate_limiter is not None
        assert state.blocklist is not None
        from data_olympus.tools_write import kb_propose_edit_fn
        resp = kb_propose_edit_fn(
            target_path=body["target_path"], postimage=body["postimage"],
            base_commit=body["base_commit"],
            base_blob_sha=body.get("base_blob_sha"),
            target_file_hash=body.get("target_file_hash"),
            reason=body.get("reason", ""),
            source_session=body["source_session"],
            agent_identity=body["agent_identity"],
            confidence=confidence,
            confidence_threshold=state.config.confidence_threshold,
            worktrees=state.worktrees, push_queue=state.push_queue,
            pending=state.pending, rate_limiter=state.rate_limiter,
            blocklist=state.blocklist,
            remote_addr=request.client.host if request.client else "unknown",
            audit_log=state.audit_log,
        )
        status = 201 if resp.status == "committed" else (
            202 if resp.status == "pending_confirmation" else 400
        )
        return JSONResponse(resp.model_dump(), status_code=status)

    @app.custom_route("/api/v1/resolve/{pending_id}", methods=["POST"])
    async def resolve_pending(request: Request) -> JSONResponse:
        if (denied := _check_auth(request, auth_token)) is not None:
            return denied
        pid = request.path_params["pending_id"]
        body = await request.json()
        assert state.worktrees is not None
        assert state.push_queue is not None
        assert state.pending is not None
        from data_olympus.tools_write import kb_resolve_pending_fn
        resp = kb_resolve_pending_fn(
            pending_id=pid, decision=body["decision"],
            edited_text=body.get("edited_text"),
            worktrees=state.worktrees, push_queue=state.push_queue,
            pending=state.pending,
            source_session=body.get("source_session", "operator"),
            agent_identity=body.get("agent_identity", "operator"),
            audit_log=state.audit_log,
        )
        return JSONResponse(resp.model_dump())

    @app.custom_route("/api/v1/pending", methods=["GET"])
    async def list_pending(_request: Request) -> JSONResponse:
        assert state.pending is not None
        from data_olympus.tools_write import kb_list_pending_fn
        resp = kb_list_pending_fn(pending=state.pending)
        return JSONResponse(resp.model_dump())

    @app.custom_route("/api/v1/audit", methods=["GET"])
    async def audit(request: Request) -> JSONResponse:
        assert state.audit_log is not None
        from data_olympus.tools_audit import kb_audit_fn
        qp = request.query_params
        since = float(qp["since"]) if qp.get("since") else None
        agent = qp.get("agent")
        status_filter = qp.get("status")
        limit = int(qp.get("limit", "100"))
        resp = kb_audit_fn(audit_log=state.audit_log, since=since,
                          agent=agent, status=status_filter, limit=limit)
        return JSONResponse(resp.model_dump())

    @app.custom_route("/api/v1/consult", methods=["POST"])
    async def consult(request: Request) -> JSONResponse:
        if (denied := _check_auth(request, auth_token)) is not None:
            return denied
        import time as _time

        from data_olympus.tools_enforce import kb_consult_fn
        body = await request.json()
        resp = kb_consult_fn(
            idx=state.idx, classifier=state.classifier, ledger=state.ledger,
            workspace=body["workspace"], intent=body.get("intent", ""),
            source_session=body["source_session"],
            agent_identity=body.get("agent_identity", "unknown"),
            ttl_sec=state.config.consult_ttl_sec, now=_time.time(),
            audit_log=state.audit_log,
        )
        return JSONResponse(resp.model_dump())

    @app.custom_route("/api/v1/gate/check", methods=["POST"])
    async def gate_check(request: Request) -> JSONResponse:
        if (denied := _check_auth(request, auth_token)) is not None:
            return denied
        import time as _time

        from data_olympus.tools_enforce import kb_gate_check_fn
        body = await request.json()
        resp = kb_gate_check_fn(
            classifier=state.classifier, ledger=state.ledger,
            workspace=body["workspace"], session_id=body["session_id"],
            tool_name=body.get("tool_name", ""),
            action_path=body.get("action_path"),
            action_diff=body.get("action_diff", ""),
            now=_time.time(), ttl_sec=state.config.consult_ttl_sec,
            audit_log=state.audit_log,
        )
        return JSONResponse(resp.model_dump())

    @app.custom_route("/api/v1/compliance", methods=["GET"])
    async def compliance(request: Request) -> JSONResponse:
        if state.audit_log is None:
            return JSONResponse({"counts": {}, "by_agent": {}})
        from data_olympus.tools_enforce import kb_compliance_fn
        qp = request.query_params
        since = float(qp["since"]) if qp.get("since") else None
        agent = qp.get("agent")
        resp = kb_compliance_fn(audit_log=state.audit_log, since=since, agent=agent)
        return JSONResponse(resp.model_dump())

    @app.custom_route("/api/v1/audit/event", methods=["POST"])
    async def record_event(request: Request) -> JSONResponse:
        if (denied := _check_auth(request, auth_token)) is not None:
            return denied
        if state.audit_log is None:
            return JSONResponse({"recorded": False}, status_code=503)
        import time as _time

        body = await request.json()
        if (bad := _missing_fields_response(body, ["event_type", "workspace"])) is not None:
            return bad
        from data_olympus.tools_enforce import kb_record_event_fn
        try:
            resp = kb_record_event_fn(
                audit_log=state.audit_log, event_type=body["event_type"],
                workspace=body["workspace"],
                agent_identity=body.get("agent_identity", "unknown"),
                source_session=body.get("source_session", ""),
                reason=body.get("reason", ""), now=_time.time())
        except ValueError as e:
            return JSONResponse({"recorded": False, "error": str(e)}, status_code=400)
        return JSONResponse(resp.model_dump())

    @app.custom_route("/api/v1/onboarding/status", methods=["GET"])
    async def onboarding_status(request: Request) -> JSONResponse:
        from data_olympus.tools_onboarding import kb_onboarding_status_fn
        qp = request.query_params
        resp = kb_onboarding_status_fn(
            idx=state.idx,
            workspace=qp.get("workspace", ""),
            component=qp.get("component") or None,
            workspace_remote_url=qp.get("workspace_remote_url") or None,
            component_remote_url=qp.get("component_remote_url") or None,
        )
        return JSONResponse(resp.model_dump())

    @app.custom_route("/api/v1/onboarding/bootstrap", methods=["POST"])
    async def onboarding_bootstrap(request: Request) -> JSONResponse:
        if (denied := _check_auth(request, auth_token)) is not None:
            return denied
        body = await request.json()
        assert state.worktrees is not None
        assert state.push_queue is not None
        assert state.pending is not None
        assert state.rate_limiter is not None
        assert state.blocklist is not None
        from data_olympus.tools_onboarding import kb_bootstrap_project_fn
        resp = kb_bootstrap_project_fn(
            idx=state.idx,
            workspace=body["workspace"],
            component=body.get("component"),
            workspace_remote_url=body.get("workspace_remote_url"),
            component_remote_url=body.get("component_remote_url"),
            files=body["files"],
            source_session=body["source_session"],
            agent_identity=body["agent_identity"],
            confidence=float(body["confidence"]),
            confidence_threshold=state.config.confidence_threshold,
            worktrees=state.worktrees, push_queue=state.push_queue,
            pending=state.pending, rate_limiter=state.rate_limiter,
            blocklist=state.blocklist, audit_log=state.audit_log,
            remote_addr=request.client.host if request.client else "unknown",
        )
        status = 201 if resp.status == "committed" else (
            202 if resp.status == "pending_confirmation" else 400
        )
        return JSONResponse(resp.model_dump(), status_code=status)
