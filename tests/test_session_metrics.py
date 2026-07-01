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
