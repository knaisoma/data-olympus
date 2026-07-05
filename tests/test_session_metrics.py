"""Tests for streamable-http session observability + idle reaper.

These exercise the leak fix without a live HTTP server: the mcp SDK's
``StreamableHTTPSessionManager`` tracks transports in a plain
``_server_instances`` dict, so a fake manager + fake transports faithfully model
the accumulation the reaper must bound. One test also asserts discovery against
the real FastMCP-built HTTP app.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from data_olympus.server import build_app
from data_olympus.session_metrics import (
    SessionActivityMiddleware,
    SessionActivityTracker,
    count_live_sessions,
    find_session_manager,
    reap_idle_sessions,
)

if TYPE_CHECKING:
    from pathlib import Path


class FakeTransport:
    """Mimics mcp's StreamableHTTPServerTransport public surface."""

    def __init__(self, session_id: str) -> None:
        self.mcp_session_id = session_id
        self._terminated = False

    @property
    def is_terminated(self) -> bool:
        return self._terminated

    async def terminate(self) -> None:
        self._terminated = True


class FakeSessionManager:
    """Mimics the SDK manager: transports live in a _server_instances dict."""

    def __init__(self) -> None:
        self._server_instances: dict[str, FakeTransport] = {}

    def handshake(self, session_id: str) -> FakeTransport:
        t = FakeTransport(session_id)
        self._server_instances[session_id] = t
        return t

    def drop_terminated(self) -> None:
        # Mirror the SDK: a terminated session's task finally-block removes it.
        for sid, t in list(self._server_instances.items()):
            if t.is_terminated:
                del self._server_instances[sid]


def _clock():
    """A controllable monotonic clock."""
    holder = {"t": 1000.0}

    def now() -> float:
        return holder["t"]

    def advance(dt: float) -> None:
        holder["t"] += dt

    now.advance = advance  # type: ignore[attr-defined]
    return now


# ---------------------------------------------------------------------------
# find_session_manager / count_live_sessions
# ---------------------------------------------------------------------------


def test_count_none_when_no_manager() -> None:
    assert count_live_sessions(None) is None
    assert count_live_sessions(object()) is None


def test_find_manager_direct_and_via_holder() -> None:
    mgr = FakeSessionManager()
    assert find_session_manager(mgr) is mgr

    class Holder:
        def __init__(self, m: FakeSessionManager) -> None:
            self.session_manager = m

    assert find_session_manager(Holder(mgr)) is mgr


def test_count_tracks_handshakes() -> None:
    mgr = FakeSessionManager()
    assert count_live_sessions(mgr) == 0
    mgr.handshake("a")
    mgr.handshake("b")
    assert count_live_sessions(mgr) == 2


def test_find_manager_before_lifespan_is_none(tmp_kb: Path, tmp_path: Path) -> None:
    """FastMCP only populates the session manager inside the lifespan. Before
    the app starts serving there is nothing to count, and discovery must return
    None (not crash the health probe)."""
    app = build_app(
        kb_main_path=tmp_kb,
        kb_index_path=tmp_path / "idx.db",
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        bootstrap_now=True,
    )
    http_app = app.http_app(transport="streamable-http")
    assert find_session_manager(http_app) is None
    assert count_live_sessions(http_app) is None


@pytest.mark.asyncio
async def test_find_manager_on_real_fastmcp_app(tmp_kb: Path, tmp_path: Path) -> None:
    """Discovery must locate the manager on a real FastMCP-built streamable-http
    app once its lifespan has started. Guards against FastMCP relocating the
    wrapper across versions."""
    app = build_app(
        kb_main_path=tmp_kb,
        kb_index_path=tmp_path / "idx.db",
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        bootstrap_now=True,
    )
    http_app = app.http_app(transport="streamable-http")
    async with http_app.router.lifespan_context(http_app):
        mgr = find_session_manager(http_app)
        # The wrapper's manager is populated inside the lifespan.
        assert mgr is not None
        assert isinstance(getattr(mgr, "_server_instances", None), dict)
        assert count_live_sessions(http_app) == 0


# ---------------------------------------------------------------------------
# SessionActivityTracker
# ---------------------------------------------------------------------------


def test_tracker_idle_detection() -> None:
    now = _clock()
    tracker = SessionActivityTracker(clock=now)
    tracker.touch("a")
    tracker.touch("b")
    # Nothing idle yet.
    assert tracker.idle_session_ids(live_ids={"a", "b"}, idle_after_sec=30) == []
    now.advance(31)  # type: ignore[attr-defined]
    tracker.touch("b")  # b stays active
    idle = tracker.idle_session_ids(live_ids={"a", "b"}, idle_after_sec=30)
    assert idle == ["a"]


def test_tracker_unseen_live_session_gets_full_window() -> None:
    now = _clock()
    tracker = SessionActivityTracker(clock=now)
    # "c" is live but never touched: first pass stamps it, does not reap it.
    assert tracker.idle_session_ids(live_ids={"c"}, idle_after_sec=30) == []
    now.advance(31)  # type: ignore[attr-defined]
    assert tracker.idle_session_ids(live_ids={"c"}, idle_after_sec=30) == ["c"]


def test_tracker_forgets_vanished_sessions() -> None:
    now = _clock()
    tracker = SessionActivityTracker(clock=now)
    tracker.touch("a")
    tracker.idle_session_ids(live_ids=set(), idle_after_sec=30)
    assert tracker.last_seen("a") is None


# ---------------------------------------------------------------------------
# SessionActivityMiddleware
# ---------------------------------------------------------------------------


class _RecordingApp:
    """A downstream ASGI app that records whether it was called."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, scope, _receive, _send) -> None:
        self.calls.append(scope)


async def _noop_receive():  # pragma: no cover - never awaited in these tests
    return {"type": "http.request"}


async def _noop_send(_message) -> None:  # pragma: no cover - never awaited
    return None


def _http_scope(headers: list[tuple[bytes, bytes]]) -> dict:
    return {"type": "http", "headers": headers}


@pytest.mark.asyncio
async def test_middleware_stamps_activity_when_header_present() -> None:
    now = _clock()
    tracker = SessionActivityTracker(clock=now)
    downstream = _RecordingApp()
    mw = SessionActivityMiddleware(downstream, tracker)

    scope = _http_scope([(b"mcp-session-id", b"sess-1")])
    await mw(scope, _noop_receive, _noop_send)

    # Activity was stamped for the header's session id...
    assert tracker.last_seen("sess-1") == 1000.0
    # ...and the request was passed through unchanged.
    assert downstream.calls == [scope]


@pytest.mark.asyncio
async def test_middleware_header_casing_and_latin1_decode() -> None:
    """ASGI header names arrive lowercased by spec, but the middleware lowercases
    defensively; the value is decoded latin-1 (the SDK emits ascii uuids, but a
    non-ascii byte must not raise)."""
    now = _clock()
    tracker = SessionActivityTracker(clock=now)
    mw = SessionActivityMiddleware(_RecordingApp(), tracker)

    # Mixed-case header key is still matched.
    await mw(_http_scope([(b"Mcp-Session-Id", b"sess-cased")]), _noop_receive, _noop_send)
    assert tracker.last_seen("sess-cased") == 1000.0

    # A 0xff byte decodes cleanly under latin-1 (would raise under utf-8).
    await mw(_http_scope([(b"mcp-session-id", b"sess-\xff")]), _noop_receive, _noop_send)
    assert tracker.last_seen("sess-\xff".encode("latin-1").decode("latin-1")) == 1000.0


@pytest.mark.asyncio
async def test_middleware_noop_and_passthrough_when_header_absent() -> None:
    tracker = SessionActivityTracker()
    downstream = _RecordingApp()
    mw = SessionActivityMiddleware(downstream, tracker)

    scope = _http_scope([(b"content-type", b"application/json")])
    await mw(scope, _noop_receive, _noop_send)

    # No session id stamped, nothing tracked, and the request still passed through.
    assert tracker.last_seen("content-type") is None
    assert downstream.calls == [scope]


@pytest.mark.asyncio
async def test_middleware_empty_header_value_is_ignored() -> None:
    tracker = SessionActivityTracker()
    downstream = _RecordingApp()
    mw = SessionActivityMiddleware(downstream, tracker)

    # An empty value must not stamp (and must not crash).
    await mw(_http_scope([(b"mcp-session-id", b"")]), _noop_receive, _noop_send)
    assert tracker.last_seen("") is None
    assert len(downstream.calls) == 1


@pytest.mark.asyncio
async def test_middleware_non_http_scope_passes_through() -> None:
    tracker = SessionActivityTracker()
    downstream = _RecordingApp()
    mw = SessionActivityMiddleware(downstream, tracker)

    # A lifespan/websocket scope has no headers to scan; must not crash.
    scope = {"type": "lifespan"}
    await mw(scope, _noop_receive, _noop_send)
    assert downstream.calls == [scope]


@pytest.mark.asyncio
async def test_middleware_early_break_stamps_once_on_first_match() -> None:
    """The header scan breaks on the first matching id, so a (malformed) request
    with two session-id headers stamps only the first and does not double-scan."""
    now = _clock()
    tracker = SessionActivityTracker(clock=now)
    mw = SessionActivityMiddleware(_RecordingApp(), tracker)

    scope = _http_scope(
        [(b"mcp-session-id", b"first"), (b"mcp-session-id", b"second")]
    )
    await mw(scope, _noop_receive, _noop_send)

    assert tracker.last_seen("first") == 1000.0
    assert tracker.last_seen("second") is None


# ---------------------------------------------------------------------------
# reap_idle_sessions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reaper_terminates_only_idle_sessions() -> None:
    now = _clock()
    mgr = FakeSessionManager()
    tracker = SessionActivityTracker(clock=now)

    ta = mgr.handshake("a")
    tb = mgr.handshake("b")
    tracker.touch("a")
    tracker.touch("b")

    now.advance(31)  # type: ignore[attr-defined]
    tracker.touch("b")  # keep b active

    reaped = await reap_idle_sessions(app=mgr, tracker=tracker, idle_after_sec=30)
    assert reaped == 1
    assert ta.is_terminated is True
    assert tb.is_terminated is False


@pytest.mark.asyncio
async def test_reaper_bounds_growth_across_repeated_handshakes() -> None:
    """Regression for #43: repeated handshakes that never DELETE would grow
    _server_instances without bound. With activity stamping + reaper, the live
    count stays bounded once sessions go idle."""
    now = _clock()
    mgr = FakeSessionManager()
    tracker = SessionActivityTracker(clock=now)

    # Simulate 50 handshakes over time; each stamps activity then goes silent.
    for i in range(50):
        mgr.handshake(f"s{i}")
        tracker.touch(f"s{i}")
        now.advance(1)  # type: ignore[attr-defined]

    # Without a reaper the SDK dict would hold all 50 forever.
    assert count_live_sessions(mgr) == 50

    # Advance past the idle window and reap; the SDK finally-block equivalent
    # removes terminated transports from the dict.
    now.advance(60)  # type: ignore[attr-defined]
    reaped = await reap_idle_sessions(app=mgr, tracker=tracker, idle_after_sec=30)
    mgr.drop_terminated()

    assert reaped == 50
    assert count_live_sessions(mgr) == 0


@pytest.mark.asyncio
async def test_reaper_no_manager_is_noop() -> None:
    tracker = SessionActivityTracker()
    assert await reap_idle_sessions(app=None, tracker=tracker, idle_after_sec=30) == 0
    assert await reap_idle_sessions(app=object(), tracker=tracker, idle_after_sec=30) == 0


# ---------------------------------------------------------------------------
# In-flight SSE keep-alive: a quiet-but-connected GET stream must not be reaped
# ---------------------------------------------------------------------------


class _CountingTracker(SessionActivityTracker):
    """Counts touch() calls so we can assert re-stamping without timing math."""

    def __init__(self) -> None:
        super().__init__()
        self.touch_count = 0

    def touch(self, session_id: str, *, now: float | None = None) -> None:
        self.touch_count += 1
        super().touch(session_id, now=now)


@pytest.mark.asyncio
async def test_middleware_restamps_in_flight_get_sse_stream() -> None:
    """A long-lived GET /mcp SSE request is re-stamped every touch interval while
    open, so the idle reaper never evicts a still-connected client. Once the
    stream closes, re-stamping stops."""
    import asyncio

    released = asyncio.Event()

    class _HoldingApp:
        async def __call__(self, _scope, _receive, _send) -> None:
            await released.wait()  # simulate a held-open SSE stream

    tracker = _CountingTracker()
    mw = SessionActivityMiddleware(_HoldingApp(), tracker, touch_interval_sec=0.02)
    scope = {
        "type": "http",
        "method": "GET",
        "headers": [(b"mcp-session-id", b"sse-1")],
    }
    task = asyncio.create_task(mw(scope, _noop_receive, _noop_send))
    await asyncio.sleep(0.12)  # ~6 touch intervals

    # Initial stamp plus several periodic re-stamps while the stream is open.
    assert tracker.touch_count >= 3

    released.set()
    await task
    settled = tracker.touch_count
    await asyncio.sleep(0.06)
    # No further stamps after the stream closed: the keep-alive task was cancelled.
    assert tracker.touch_count == settled


@pytest.mark.asyncio
async def test_middleware_short_post_stamps_once_not_periodically() -> None:
    """A short POST request finishes before the first tick, so it is stamped once
    and does not spin up a keep-alive loop."""
    tracker = _CountingTracker()
    mw = SessionActivityMiddleware(_RecordingApp(), tracker, touch_interval_sec=0.02)
    scope = {
        "type": "http",
        "method": "POST",
        "headers": [(b"mcp-session-id", b"post-1")],
    }
    await mw(scope, _noop_receive, _noop_send)
    assert tracker.touch_count == 1
