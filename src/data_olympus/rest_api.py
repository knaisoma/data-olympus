"""REST mirror at /api/v1/*. Custom routes mounted on the same FastMCP app
so MCP + REST share one Starlette/uvicorn process."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse

from data_olympus.principals import (
    CAP_BOOTSTRAP,
    CAP_PROPOSE,
    CAP_RECORD_EVENT,
    CAP_RESOLVE,
    PrincipalRegistry,
)
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
    from data_olympus.principals import Principal
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
        last_git_fetch_status=state.last_git_fetch_status,
        last_git_fetch_error=state.last_git_fetch_error,
        last_git_fetch_at=state.last_git_fetch_at,
        last_successful_refresh_at=state.last_successful_refresh_at,
        remote_head_sha=state.remote_head_sha,
    )


def _degraded_response(health: HealthResponse) -> JSONResponse:
    """503 + degraded:true body. Used on all read endpoints when the index is
    not healthy, so bin/kb --no-stale rejects stale reads across all subcommands."""
    body = health.model_dump()
    body["degraded"] = True
    body["error"] = "degraded_index"
    return JSONResponse(body, status_code=503)


def _authorize(
    request: Request,
    registry: PrincipalRegistry,
    capability: str | None = None,
) -> tuple[Principal, JSONResponse | None]:
    """Resolve the request's principal and check it against ``capability``.

    Returns ``(principal, None)`` when allowed, or ``(principal, denial)`` where
    ``denial`` is a 401 (unauthenticated) or 403 (authenticated but missing the
    capability) JSONResponse. ``capability=None`` means "any authenticated
    principal" — used for the enforcement-plane routes (consult / gate) which do
    not map to a KB write capability but must still be closed to anonymous
    callers when auth is configured.

    With no auth configured the resolver returns the fully-trusted LOCAL_TRUSTED
    principal, so every check passes and behavior matches the pre-auth product.
    """
    principal = registry.resolve(request.headers.get("Authorization"))
    if capability is None:
        allowed = (not registry.auth_configured) or principal.authenticated
    else:
        allowed = principal.has(capability)
    if allowed:
        return principal, None
    if not principal.authenticated:
        return principal, JSONResponse({"error": "unauthorized"}, status_code=401)
    return principal, JSONResponse(
        {"error": "forbidden",
         "message": f"principal '{principal.name}' lacks capability "
                    f"'{capability}'"},
        status_code=403,
    )


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


def _parse_confidence(body: dict[str, Any]) -> tuple[float, JSONResponse | None]:
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


def _write_pipeline_ready(state: ServerState) -> JSONResponse | None:
    """Return a structured 503 when the write pipeline is disabled (the server
    runs read-only because ``KB_REMOTE_URL`` is unset), else None.

    Previously the write handlers asserted ``state.worktrees is not None`` and a
    read-only deployment turned every write call into an opaque plain-text HTTP
    500. This returns an actionable JSON body and the correct 503 status instead.
    """
    if state.worktrees is None or state.push_queue is None or state.pending is None:
        return JSONResponse(
            {"error": "write_pipeline_disabled",
             "message": "server is read-only (KB_REMOTE_URL is not set)"},
            status_code=503,
        )
    return None


def _propose_status(status: str) -> int:
    """Map a propose/bootstrap response status to an HTTP status code."""
    if status == "committed":
        return 201
    if status == "pending_confirmation":
        return 202
    if status in ("rejected_payload_too_large", "rejected_too_many_files"):
        return 413
    if status in ("rejected_rate_limited", "rejected_pending_queue_full"):
        return 429
    return 400


async def _read_json_capped(
    request: Request, max_body_bytes: int,
) -> tuple[Any, JSONResponse | None]:
    """Read and JSON-parse the request body, enforcing a hard byte cap.

    Reads the body stream incrementally and returns a 413 the moment the running
    byte count exceeds ``max_body_bytes`` (0 = unlimited). Unlike a Content-Length
    precheck this also bounds chunked or Content-Length-omitting clients, so the
    cap is real rather than advisory. Returns ``(data, None)`` on success or
    ``(None, response)`` for a 413 (too large) or 400 (invalid JSON)."""
    if max_body_bytes <= 0:
        try:
            return await request.json(), None
        except (json.JSONDecodeError, ValueError):
            return None, JSONResponse(
                {"error": "bad_request", "message": "invalid JSON body"},
                status_code=400,
            )
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > max_body_bytes:
            return None, JSONResponse(
                {"error": "payload_too_large",
                 "message": f"request body exceeds {max_body_bytes} bytes"},
                status_code=413,
            )
    try:
        return json.loads(buf), None
    except (json.JSONDecodeError, ValueError):
        return None, JSONResponse(
            {"error": "bad_request", "message": "invalid JSON body"},
            status_code=400,
        )


def register_routes(
    app: FastMCP, state: ServerState, registry: PrincipalRegistry
) -> None:
    """Mount REST routes under /api/v1/ on the FastMCP app.

    Write and enforcement-plane routes are authorized against ``registry``
    (see ``_authorize``). When no auth is configured every caller is trusted and
    behavior matches the pre-auth product. Read routes are always open.
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
        principal, denied = _authorize(request, registry, CAP_PROPOSE)
        if denied is not None:
            return denied
        if (off := _write_pipeline_ready(state)) is not None:
            return off
        body, big = await _read_json_capped(request, state.config.max_body_bytes)
        if big is not None:
            return big
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
            can_auto_commit=principal.can_auto_commit,
            max_text_bytes=state.config.max_text_bytes,
        )
        status = _propose_status(resp.status)
        return JSONResponse(resp.model_dump(), status_code=status)

    @app.custom_route("/api/v1/propose/edit", methods=["POST"])
    async def propose_edit(request: Request) -> JSONResponse:
        principal, denied = _authorize(request, registry, CAP_PROPOSE)
        if denied is not None:
            return denied
        if (off := _write_pipeline_ready(state)) is not None:
            return off
        body, big = await _read_json_capped(request, state.config.max_body_bytes)
        if big is not None:
            return big
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
            can_auto_commit=principal.can_auto_commit,
            max_postimage_bytes=state.config.max_postimage_bytes,
        )
        status = _propose_status(resp.status)
        return JSONResponse(resp.model_dump(), status_code=status)

    @app.custom_route("/api/v1/resolve/{pending_id}", methods=["POST"])
    async def resolve_pending(request: Request) -> JSONResponse:
        _principal, denied = _authorize(request, registry, CAP_RESOLVE)
        if denied is not None:
            return denied
        if (off := _write_pipeline_ready(state)) is not None:
            return off
        pid = request.path_params["pending_id"]
        body = await request.json()
        if (bad := _missing_fields_response(body, ["decision"])) is not None:
            return bad
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
    async def list_pending(request: Request) -> JSONResponse:
        # Observability routes leak target paths / agent identities, so when auth
        # is configured they require an authenticated principal (open otherwise).
        _principal, denied = _authorize(request, registry)
        if denied is not None:
            return denied
        if state.pending is None:
            return JSONResponse({"pending": []})
        from data_olympus.tools_write import kb_list_pending_fn
        resp = kb_list_pending_fn(pending=state.pending)
        return JSONResponse(resp.model_dump())

    @app.custom_route("/api/v1/audit", methods=["GET"])
    async def audit(request: Request) -> JSONResponse:
        _principal, denied = _authorize(request, registry)
        if denied is not None:
            return denied
        if state.audit_log is None:
            return JSONResponse({"events": [], "returned": 0, "limit_hit": False})
        from data_olympus.tools_audit import kb_audit_fn
        qp = request.query_params
        since = float(qp["since"]) if qp.get("since") else None
        agent = qp.get("agent")
        status_filter = qp.get("status")
        limit = int(qp.get("limit", "100"))
        resp = kb_audit_fn(audit_log=state.audit_log, since=since,
                          agent=agent, status=status_filter, limit=limit)
        return JSONResponse(resp.model_dump())

    @app.custom_route("/api/v1/audit/verify", methods=["GET"])
    async def audit_verify(request: Request) -> JSONResponse:
        _principal, denied = _authorize(request, registry)
        if denied is not None:
            return denied
        if state.audit_log is None:
            return JSONResponse({"ok": True, "first_broken_index": -1})
        ok, idx = state.audit_log.verify()
        return JSONResponse({"ok": ok, "first_broken_index": idx})

    @app.custom_route("/api/v1/consult", methods=["POST"])
    async def consult(request: Request) -> JSONResponse:
        _principal, denied = _authorize(request, registry)
        if denied is not None:
            return denied
        import time as _time

        from data_olympus.tools_enforce import kb_consult_fn
        body = await request.json()
        if (bad := _missing_fields_response(
            body, ["workspace", "source_session"],
        )) is not None:
            return bad
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
        _principal, denied = _authorize(request, registry)
        if denied is not None:
            return denied
        import time as _time

        from data_olympus.tools_enforce import kb_gate_check_fn
        body = await request.json()
        if (bad := _missing_fields_response(
            body, ["workspace", "session_id"],
        )) is not None:
            return bad
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
        # Enforcement aggregates are observability data; gate like /pending,
        # /audit, /consult, /gate when auth is configured.
        _principal, denied = _authorize(request, registry)
        if denied is not None:
            return denied
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
        _principal, denied = _authorize(request, registry, CAP_RECORD_EVENT)
        if denied is not None:
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
        principal, denied = _authorize(request, registry, CAP_BOOTSTRAP)
        if denied is not None:
            return denied
        if (off := _write_pipeline_ready(state)) is not None:
            return off
        body, big = await _read_json_capped(request, state.config.max_body_bytes)
        if big is not None:
            return big
        if (bad := _missing_fields_response(
            body, ["workspace", "files", "source_session",
                   "agent_identity", "confidence"],
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
            confidence=confidence,
            confidence_threshold=state.config.confidence_threshold,
            worktrees=state.worktrees, push_queue=state.push_queue,
            pending=state.pending, rate_limiter=state.rate_limiter,
            blocklist=state.blocklist, audit_log=state.audit_log,
            remote_addr=request.client.host if request.client else "unknown",
            can_auto_commit=principal.can_auto_commit,
            max_postimage_bytes=state.config.max_postimage_bytes,
            max_files=state.config.max_bootstrap_files,
        )
        status = _propose_status(resp.status)
        return JSONResponse(resp.model_dump(), status_code=status)

    @app.custom_route("/api/v1/onboarding/playbook", methods=["GET"])
    async def onboarding_playbook(request: Request) -> JSONResponse:
        from data_olympus.onboarding_playbook import render_playbook
        qp = request.query_params
        kind = qp.get("kind", "dispatch")
        try:
            text = render_playbook(
                kind,
                workspace=qp.get("workspace") or None,
                component=qp.get("component") or None,
                workspace_remote_url=qp.get("workspace_remote_url") or None,
                component_remote_url=qp.get("component_remote_url") or None,
            )
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"kind": kind, "text": text})

    @app.custom_route("/api/v1/onboarding/cleanup-plan", methods=["POST"])
    async def onboarding_cleanup_plan(request: Request) -> JSONResponse:
        body, big = await _read_json_capped(request, state.config.max_body_bytes)
        if big is not None:
            return big
        if (bad := _missing_fields_response(body, ["workspace", "local_files"])) is not None:
            return bad
        from data_olympus.tools_onboarding import CleanupInputError, kb_cleanup_plan_fn
        try:
            resp = kb_cleanup_plan_fn(
                idx=state.idx,
                workspace=body["workspace"],
                component=body.get("component"),
                local_files=body["local_files"],
                jaccard_threshold=body.get("jaccard_threshold", 0.6),
                max_files=state.config.max_bootstrap_files,
                max_content_bytes=state.config.max_postimage_bytes,
            )
        except CleanupInputError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(resp.model_dump())
