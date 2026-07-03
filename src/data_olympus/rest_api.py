"""REST mirror at /api/v1/*. Custom routes mounted on the same FastMCP app
so MCP + REST share one Starlette/uvicorn process."""
from __future__ import annotations

import functools
import json
from typing import TYPE_CHECKING, Any

import anyio
from starlette.responses import JSONResponse

from data_olympus.principals import (
    CAP_BOOTSTRAP,
    CAP_PROPOSE,
    CAP_RECORD_EVENT,
    CAP_RESOLVE,
    Principal,
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
    from data_olympus.server import ServerState


async def _offload(fn: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Run a blocking callable in the anyio worker-thread pool.

    Every REST handler's core is synchronous (SQLite reads, audit/ledger file
    I/O, git worktree ops). Running it inline would block the single asyncio
    event loop, and the k8s readiness probe (``GET /api/v1/health``, 1s timeout)
    is served on that same loop: a stalled loop under load drops the probe, the
    only pod is ejected from the Service, and nginx returns 503. Offloading keeps
    the loop free to answer the probe. SQLite is safe here because ``Index`` opens
    a connection per call; the stateful writers (AuditLog, ConsultationLedger)
    are lock-guarded for concurrent thread access.
    """
    return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))


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
        push_queue_frozen=state.push_queue_frozen,
        path_locks_held=state.pending.locks_held() if state.pending else 0,
        last_index_build_status=state.last_index_build_status,
        last_index_error=state.last_index_error,
        last_index_error_at=state.last_index_error_at,
        last_index_conflicts=state.last_index_conflicts,
        last_git_fetch_status=state.last_git_fetch_status,
        last_git_fetch_error=state.last_git_fetch_error,
        last_git_fetch_at=state.last_git_fetch_at,
        last_successful_refresh_at=state.last_successful_refresh_at,
        remote_head_sha=state.remote_head_sha,
        live_sessions=state.live_session_count(),
    )


def _degraded_response(health: HealthResponse) -> JSONResponse:
    """503 + degraded:true body. Used on all read endpoints when the index is
    not healthy, so bin/kb --no-stale rejects stale reads across all subcommands."""
    body = health.model_dump()
    body["degraded"] = True
    body["error"] = "degraded_index"
    return JSONResponse(body, status_code=503)


def _query_bool(raw: str | None) -> bool:
    """Parse a boolean query-string flag. True for 1/true/yes/on (case-insensitive);
    everything else (incl. absent/empty) is False, so a missing flag defaults off."""
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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


def _parse_numeric_qp(
    qp: Any, name: str, kind: type, *, default: Any = None,
) -> tuple[Any, JSONResponse | None]:
    """Coerce query param ``name`` to ``kind`` (float/int), returning a 400
    JSONResponse instead of letting a non-numeric value raise ValueError -> HTTP
    500 (item 9). The file's own 400-vs-500 rationale (see _missing_fields_response)
    demands that malformed client input surface as an actionable 400, not an
    opaque crash. A missing/empty param yields ``default``."""
    raw = qp.get(name)
    if raw is None or raw == "":
        return default, None
    try:
        return kind(raw), None
    except (TypeError, ValueError):
        return default, JSONResponse(
            {"error": "bad_request",
             "message": f"query param '{name}' must be a {kind.__name__}"},
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
    if status in ("rejected_stale_base", "rejected_path_lock_busy"):
        # Optimistic-concurrency / lock contention: the caller's base moved or a
        # concurrent write holds the path. 409 Conflict.
        return 409
    if status == "rejected_invalid_document":
        # The postimage failed the content-validation gate. 422 Unprocessable.
        return 422
    return 400


def _resolve_status(status: str) -> int:
    """Map a resolve response status to an HTTP status code (item 5, item 3/4)."""
    if status == "committed":
        return 200
    if status == "rejected_edited_text_too_large":
        return 413
    if status == "already_resolved":
        # Lost the double-resolve race: the id was already decided. 409 Conflict.
        return 409
    if status == "rejected_stale_base":
        return 409
    if status == "rejected_invalid_document":
        return 422
    if status in ("rejected", "rejected_symlink_escape"):
        return 200
    return 200


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


def _rate_limited(
    state: ServerState, request: Request, principal: Principal,
) -> JSONResponse | None:
    """Apply the shared sliding-window limiter to an enforcement-plane route.

    The consult / gate / cleanup-plan routes were previously unthrottled (item 6),
    so an anonymous or authenticated caller could hammer the classifier and ledger
    without bound. Reuse the same limiter the write routes use, keyed by
    (remote_addr, principal name) so a per-principal quota applies. Returns a 429
    JSONResponse when over quota, else None.

    When the limiter is absent (a read-only deployment with no write pipeline
    configured) throttling is skipped: there is no shared limiter object to
    consult and these routes are read-mostly there.
    """
    limiter = state.rate_limiter
    if limiter is None:
        return None
    remote_addr = request.client.host if request.client else "unknown"
    if not limiter.allow(remote_addr=remote_addr, agent_identity=principal.name):
        return JSONResponse(
            {"error": "rate_limited",
             "message": "too many requests; retry later"},
            status_code=429,
        )
    return None


def register_routes(
    app: FastMCP,
    state: ServerState,
    registry: PrincipalRegistry,
    *,
    read_only: bool = False,
) -> None:
    """Mount REST routes under /api/v1/ on the FastMCP app.

    Write and enforcement-plane routes are authorized against ``registry``
    (see ``_authorize``). When no auth is configured every caller is trusted and
    behavior matches the pre-auth product. Read routes are always open.

    When ``read_only`` is True (a scaling replica, issue #44) the write and
    enforcement-write routes (propose / resolve / bootstrap / consult / gate /
    record-event and the observability mirrors) are not registered at all, so
    they return 404. Only the pure-read routes are mounted.
    """

    @app.custom_route("/api/v1/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        # Served INLINE, not via _offload(): the readiness probe must never queue
        # behind the shared anyio worker pool it exists to outlive. _build_health
        # reads the cached Index.health() snapshot (memory in steady state; the
        # rare cache-miss SQLite read is sub-millisecond on the loop), so keeping
        # it off the limiter is the right trade for probe responsiveness.
        resp = _build_health(state)
        # Degraded health responses MUST return 503 so the
        # CLI's --no-stale contract (exit 2 on HTTP 200 or 503 degraded) is meaningful.
        status = 503 if resp.degraded else 200
        return JSONResponse(resp.model_dump(), status_code=status)

    @app.custom_route("/api/v1/outline", methods=["GET"])
    async def outline(_request: Request) -> JSONResponse:
        h = await _offload(_build_health, state)
        if h.degraded:
            return _degraded_response(h)
        resp = await _offload(kb_outline_fn, idx=state.idx)
        return JSONResponse(resp.model_dump())

    @app.custom_route("/api/v1/search", methods=["GET"])
    async def search(request: Request) -> JSONResponse:
        h = await _offload(_build_health, state)
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
        in_force = _query_bool(request.query_params.get("in_force"))
        abstain = _query_bool(request.query_params.get("abstain"))
        resp = await _offload(
            kb_search_fn, idx=state.idx, query=q, limit=limit, tier=tier,
            category=category, in_force=in_force, abstain=abstain,
        )
        return JSONResponse(resp.model_dump())

    @app.custom_route("/api/v1/get/{id}", methods=["GET"])
    async def get(request: Request) -> JSONResponse:
        h = await _offload(_build_health, state)
        if h.degraded:
            return _degraded_response(h)
        id_ = request.path_params["id"]
        try:
            resp = await _offload(kb_get_fn, idx=state.idx, id=id_)
        except KbNotFoundError as e:
            return JSONResponse({"error": "not_found", "message": str(e)}, status_code=404)
        return JSONResponse(resp.model_dump())

    @app.custom_route("/api/v1/list", methods=["GET"])
    async def list_(request: Request) -> JSONResponse:
        h = await _offload(_build_health, state)
        if h.degraded:
            return _degraded_response(h)
        tier = request.query_params.get("tier")
        if not tier:
            return JSONResponse({"error": "missing_tier"}, status_code=400)
        category = request.query_params.get("category") or None
        resp = await _offload(kb_list_fn, idx=state.idx, tier=tier, category=category)
        return JSONResponse(resp.model_dump())

    if not read_only:
        # Write + enforcement-write REST surface. A read-only replica
        # (issue #44) mounts none of these, so they return 404.
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
            resp = await _offload(
                kb_propose_memory_fn,
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
                serializer=state.write_serializer, idx=state.idx,
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
            resp = await _offload(
                kb_propose_edit_fn,
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
                serializer=state.write_serializer, idx=state.idx,
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
            body, big = await _read_json_capped(request, state.config.max_body_bytes)
            if big is not None:
                return big
            if (bad := _missing_fields_response(body, ["decision"])) is not None:
                return bad
            assert state.worktrees is not None
            assert state.push_queue is not None
            assert state.pending is not None
            from data_olympus.pending import PendingNotFoundError
            from data_olympus.tools_write import kb_resolve_pending_fn
            try:
                resp = await _offload(
                    kb_resolve_pending_fn,
                    pending_id=pid, decision=body["decision"],
                    edited_text=body.get("edited_text"),
                    worktrees=state.worktrees, push_queue=state.push_queue,
                    pending=state.pending,
                    source_session=body.get("source_session", "operator"),
                    agent_identity=body.get("agent_identity", "operator"),
                    audit_log=state.audit_log,
                    max_postimage_bytes=state.config.max_postimage_bytes,
                    serializer=state.write_serializer, idx=state.idx,
                )
            except PendingNotFoundError:
                # Unknown or already-resolved/expired pending_id: a client-side
                # mistake, not a server fault. 404, not the opaque 500 the raw
                # FileNotFoundError produced (item 9).
                return JSONResponse(
                    {"error": "not_found",
                     "message": f"no pending proposal with id '{pid}'"},
                    status_code=404,
                )
            status = _resolve_status(resp.status)
            return JSONResponse(resp.model_dump(), status_code=status)

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
            resp = await _offload(kb_list_pending_fn, pending=state.pending)
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
            since, bad = _parse_numeric_qp(qp, "since", float)
            if bad is not None:
                return bad
            agent = qp.get("agent")
            status_filter = qp.get("status")
            limit, bad = _parse_numeric_qp(qp, "limit", int, default=100)
            if bad is not None:
                return bad
            resp = await _offload(kb_audit_fn, audit_log=state.audit_log, since=since,
                                  agent=agent, status=status_filter, limit=limit)
            return JSONResponse(resp.model_dump())

        @app.custom_route("/api/v1/audit/verify", methods=["GET"])
        async def audit_verify(request: Request) -> JSONResponse:
            _principal, denied = _authorize(request, registry)
            if denied is not None:
                return denied
            if state.audit_log is None:
                return JSONResponse({"ok": True, "first_broken_index": -1})
            ok, idx = await _offload(state.audit_log.verify)
            return JSONResponse({"ok": ok, "first_broken_index": idx})

        @app.custom_route("/api/v1/consult", methods=["POST"])
        async def consult(request: Request) -> JSONResponse:
            principal, denied = _authorize(request, registry)
            if denied is not None:
                return denied
            if (throttled := _rate_limited(state, request, principal)) is not None:
                return throttled
            import time as _time

            from data_olympus.tools_enforce import kb_consult_fn
            body, big = await _read_json_capped(request, state.config.max_body_bytes)
            if big is not None:
                return big
            if (bad := _missing_fields_response(
                body, ["workspace", "source_session"],
            )) is not None:
                return bad
            resp = await _offload(
                kb_consult_fn,
                idx=state.idx, classifier=state.classifier, ledger=state.ledger,
                workspace=body["workspace"], intent=body.get("intent", ""),
                source_session=body["source_session"],
                agent_identity=body.get("agent_identity", "unknown"),
                ttl_sec=state.config.consult_ttl_sec, now=_time.time(),
                audit_log=state.audit_log,
                # Optional: installers mark prompt-hook auto-consults so they are
                # audited but never clear the gate. Omitted -> explicit (old
                # clients are real agent calls).
                trigger=body.get("trigger", "explicit"),
            )
            return JSONResponse(resp.model_dump())

        @app.custom_route("/api/v1/gate/check", methods=["POST"])
        async def gate_check(request: Request) -> JSONResponse:
            principal, denied = _authorize(request, registry)
            if denied is not None:
                return denied
            if (throttled := _rate_limited(state, request, principal)) is not None:
                return throttled
            import time as _time

            from data_olympus.tools_enforce import kb_gate_check_fn
            body, big = await _read_json_capped(request, state.config.max_body_bytes)
            if big is not None:
                return big
            if (bad := _missing_fields_response(
                body, ["workspace", "session_id"],
            )) is not None:
                return bad
            resp = await _offload(
                kb_gate_check_fn,
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
            since, bad = _parse_numeric_qp(qp, "since", float)
            if bad is not None:
                return bad
            agent = qp.get("agent")
            resp = await _offload(
                kb_compliance_fn, audit_log=state.audit_log, since=since, agent=agent)
            return JSONResponse(resp.model_dump())

        @app.custom_route("/api/v1/audit/event", methods=["POST"])
        async def record_event(request: Request) -> JSONResponse:
            _principal, denied = _authorize(request, registry, CAP_RECORD_EVENT)
            if denied is not None:
                return denied
            if state.audit_log is None:
                return JSONResponse({"recorded": False}, status_code=503)
            import time as _time

            body, big = await _read_json_capped(request, state.config.max_body_bytes)
            if big is not None:
                return big
            if (bad := _missing_fields_response(body, ["event_type", "workspace"])) is not None:
                return bad
            from data_olympus.tools_enforce import kb_record_event_fn
            try:
                resp = await _offload(
                    kb_record_event_fn,
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
        resp = await _offload(
            kb_onboarding_status_fn,
            idx=state.idx,
            workspace=qp.get("workspace", ""),
            component=qp.get("component") or None,
            workspace_remote_url=qp.get("workspace_remote_url") or None,
            component_remote_url=qp.get("component_remote_url") or None,
        )
        return JSONResponse(resp.model_dump())

    if not read_only:
        # Write route: bootstrapping a workspace is a commit. Absent on a
        # read-only replica (issue #44).
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
            resp = await _offload(
                kb_bootstrap_project_fn,
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
        # Was anonymous-allowed even with auth configured, and unthrottled (item 6).
        # Close it to anonymous callers when auth is on (no-auth deployments still
        # resolve LOCAL_TRUSTED and pass) and apply the shared limiter.
        principal, denied = _authorize(request, registry)
        if denied is not None:
            return denied
        if (throttled := _rate_limited(state, request, principal)) is not None:
            return throttled
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
