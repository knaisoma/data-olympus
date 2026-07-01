"""Observability and a bound for FastMCP streamable-http transport sessions.

Why this module exists
----------------------
Each session-less ``POST /mcp`` handshake makes the mcp SDK's
``StreamableHTTPSessionManager`` create a ``StreamableHTTPServerTransport``,
register it in ``_server_instances[session_id]``, and start a long-lived task
whose ``app.run()`` blocks on the transport read stream (see the installed
``mcp/server/streamable_http_manager.py`` ``_handle_stateful_request`` /
``run_server``). That entry is removed on exactly three events: an explicit
client ``DELETE`` (``streamable_http.py`` ``_handle_delete_request`` ->
``terminate``), the session task crashing, or the manager shutting down
(``run`` finally -> ``_server_instances.clear()``).

The SDK *does* support idle reaping, but only when its manager is built with a
positive ``session_idle_timeout`` (``streamable_http_manager.py`` lines ~293-311
arm an ``anyio.CancelScope`` deadline). FastMCP 3.4.2 constructs that manager
with no ``session_idle_timeout`` argument and exposes no setting or ``run_async``
knob to inject one (``fastmcp/server/http.py`` ``create_streamable_http_app``
lifespan). So under FastMCP's default wiring the timeout is ``None`` and there is
no automatic reaping.

Consequence: a client that handshakes and drops the connection without sending
``DELETE`` (the exact symptom behind the repeated "Created new transport with
session ID" logs) leaves its transport resident. Over long uptime
``_server_instances`` grows without bound and leaks memory + tasks.

This module adds, entirely inside our own code and using only the public
transport surface (``mcp_session_id``, ``terminate``, ``is_terminated``) plus the
``_server_instances`` mapping that FastMCP itself iterates on shutdown:

- :func:`find_session_manager` / :func:`count_live_sessions` to *observe* the
  live session count (surfaced via ``kb_health`` / ``/api/v1/health`` and logged
  periodically).
- :class:`SessionActivityTracker` to stamp last-activity per session id from an
  ASGI middleware, since the SDK does not stamp activity when idle-timeout is
  off.
- :func:`reap_idle_sessions` and :func:`session_reaper_loop` to terminate
  sessions idle beyond a configured bound, closing the leak.

Everything degrades safely: if a future FastMCP/mcp version relocates or renames
these internals, discovery returns ``None`` and the count is reported as
``None`` rather than crashing the health probe.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger("data_olympus.session_metrics")

# Header the mcp SDK uses to carry the streamable-http session id on both the
# handshake response and subsequent requests. Mirrored here (rather than
# imported) so a rename upstream degrades to "no activity stamp" instead of an
# ImportError at module load.
_MCP_SESSION_ID_HEADER = b"mcp-session-id"


class _Transport(Protocol):
    """The subset of mcp's StreamableHTTPServerTransport we rely on. Public
    attributes only; see mcp/server/streamable_http.py."""

    mcp_session_id: str | None

    @property
    def is_terminated(self) -> bool: ...

    async def terminate(self) -> None: ...


def find_session_manager(app: Any) -> Any | None:
    """Locate the live ``StreamableHTTPSessionManager`` for a FastMCP app, or None.

    FastMCP stores the manager on the ``StreamableHTTPASGIApp`` wrapper, set
    fresh on each lifespan cycle, not on the FastMCP object. We walk the built
    Starlette app's routes to find the wrapper's ``session_manager`` that carries
    a ``_server_instances`` dict. Returns None (never raises) when the app has
    not been built into an HTTP app yet or the internals moved.
    """
    if app is None:
        return None
    # Direct: some callers pass the manager (or its holder) straight in.
    if _has_instances(app):
        return app
    holder = getattr(app, "session_manager", None)
    if _has_instances(holder):
        return holder

    starlette_app = _resolve_starlette_app(app)
    if starlette_app is None:
        return None
    routes = getattr(getattr(starlette_app, "router", None), "routes", None) or []
    for route in routes:
        endpoint = getattr(route, "endpoint", None) or getattr(route, "app", None)
        mgr = getattr(endpoint, "session_manager", None)
        if _has_instances(mgr):
            return mgr
    return None


def _has_instances(obj: Any) -> bool:
    return obj is not None and isinstance(
        getattr(obj, "_server_instances", None), dict
    )


def _resolve_starlette_app(app: Any) -> Any | None:
    """Best-effort resolve a Starlette app from a FastMCP instance or a Starlette
    app. Prefers a cached HTTP app the server already built; never builds a new
    one (that would create a second, unrelated session manager)."""
    # Already a Starlette-like app with a router.
    if getattr(getattr(app, "router", None), "routes", None) is not None:
        return app
    # FastMCP caches the built http app under private attrs across versions.
    for attr in ("_http_app", "_cached_http_app", "_streamable_http_app"):
        candidate = getattr(app, attr, None)
        if getattr(getattr(candidate, "router", None), "routes", None) is not None:
            return candidate
    return None


def count_live_sessions(app: Any) -> int | None:
    """Return the number of live streamable-http sessions, or None if the session
    manager cannot be located (e.g. app not yet serving). Never raises."""
    try:
        mgr = find_session_manager(app)
        if mgr is None:
            return None
        instances = getattr(mgr, "_server_instances", None)
        if not isinstance(instances, dict):
            return None
        return len(instances)
    except Exception:  # pragma: no cover - defensive; observability must not crash callers
        log.debug("count_live_sessions failed", exc_info=True)
        return None


class SessionActivityTracker:
    """Last-activity timestamps per streamable-http session id.

    The mcp SDK only stamps activity (via ``idle_scope.deadline``) when the
    session manager is built with an idle timeout, which FastMCP does not do. We
    therefore keep our own map, updated from the ASGI middleware on every request
    that carries an ``mcp-session-id`` header (both the handshake response and
    subsequent client requests carry it). ``touch`` is also called at session
    creation so a brand-new session is not reaped before its first follow-up.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last_seen: dict[str, float] = {}

    def touch(self, session_id: str, *, now: float | None = None) -> None:
        if not session_id:
            return
        self._last_seen[session_id] = self._clock() if now is None else now

    def last_seen(self, session_id: str) -> float | None:
        return self._last_seen.get(session_id)

    def forget(self, session_id: str) -> None:
        self._last_seen.pop(session_id, None)

    def idle_session_ids(
        self, *, live_ids: set[str], idle_after_sec: float, now: float | None = None
    ) -> list[str]:
        """Return live session ids whose last activity is older than
        ``idle_after_sec``. A live session we have never seen is treated as
        active-now and stamped, so it gets a full idle window before eviction
        (avoids reaping a session created between middleware touches)."""
        current = self._clock() if now is None else now
        # Drop bookkeeping for sessions the manager no longer tracks.
        for stale_id in [sid for sid in self._last_seen if sid not in live_ids]:
            self._last_seen.pop(stale_id, None)
        idle: list[str] = []
        for sid in live_ids:
            seen = self._last_seen.get(sid)
            if seen is None:
                self._last_seen[sid] = current
                continue
            if current - seen >= idle_after_sec:
                idle.append(sid)
        return idle


class SessionActivityMiddleware:
    """ASGI middleware that stamps :class:`SessionActivityTracker` on each request
    carrying an ``mcp-session-id`` header. Pure pass-through otherwise; adds no
    latency beyond a header scan."""

    def __init__(self, app: ASGIApp, tracker: SessionActivityTracker) -> None:
        self.app = app
        self._tracker = tracker

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http":
            for key, value in scope.get("headers", []):
                if key.lower() == _MCP_SESSION_ID_HEADER and value:
                    self._tracker.touch(value.decode("latin-1"))
                    break
        await self.app(scope, receive, send)


async def reap_idle_sessions(
    *,
    app: Any,
    tracker: SessionActivityTracker,
    idle_after_sec: float,
    now: float | None = None,
) -> int:
    """Terminate live sessions idle beyond ``idle_after_sec``. Returns the count
    reaped. Uses only the public transport surface (``mcp_session_id``,
    ``is_terminated``, ``terminate``); the SDK's own crash-cleanup ``finally``
    then removes the entry from ``_server_instances``. Safe to call when no
    session manager exists yet (returns 0)."""
    mgr = find_session_manager(app)
    if mgr is None:
        return 0
    instances = getattr(mgr, "_server_instances", None)
    if not isinstance(instances, dict):
        return 0
    live: dict[str, _Transport] = dict(instances)
    idle_ids = tracker.idle_session_ids(
        live_ids=set(live.keys()), idle_after_sec=idle_after_sec, now=now
    )
    reaped = 0
    for sid in idle_ids:
        transport = live.get(sid)
        if transport is None or transport.is_terminated:
            tracker.forget(sid)
            continue
        try:
            await transport.terminate()
            reaped += 1
            log.info("reaped idle streamable-http session %s", sid)
        except Exception:  # pragma: no cover - defensive
            log.warning("failed to terminate idle session %s", sid, exc_info=True)
        finally:
            tracker.forget(sid)
    return reaped


async def session_reaper_loop(
    *,
    app: Any,
    tracker: SessionActivityTracker,
    idle_after_sec: float,
    interval_sec: float,
    log_count: bool = True,
) -> None:
    """Background asyncio task: periodically reap idle sessions and log the live
    session count for production observability. Cancellable via asyncio."""
    log.info(
        "session_reaper_loop started (idle_after=%ss interval=%ss)",
        idle_after_sec,
        interval_sec,
    )
    while True:
        try:
            reaped = await reap_idle_sessions(
                app=app, tracker=tracker, idle_after_sec=idle_after_sec
            )
            if log_count:
                live = count_live_sessions(app)
                if live is not None:
                    log.info("live streamable-http sessions: %d (reaped %d)", live, reaped)
        except asyncio.CancelledError:
            log.info("session_reaper_loop cancelled")
            raise
        except Exception:  # pragma: no cover - defensive
            log.warning("session_reaper_loop iteration failed", exc_info=True)
        await asyncio.sleep(interval_sec)


__all__ = [
    "SessionActivityMiddleware",
    "SessionActivityTracker",
    "count_live_sessions",
    "find_session_manager",
    "reap_idle_sessions",
    "session_reaper_loop",
]
