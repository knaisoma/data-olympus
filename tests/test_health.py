"""Tests for health aggregation."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from data_olympus.health import HealthState, snapshot
from data_olympus.index import Index

if TYPE_CHECKING:
    from pathlib import Path


def test_snapshot_when_index_unbuilt(tmp_path: Path) -> None:
    idx = Index(tmp_path / "idx.db")
    state = snapshot(idx=idx, last_git_pull_at=None, staleness_degraded_sec=600)
    assert isinstance(state, HealthState)
    assert state.kb_commit == ""
    assert state.total_rules == 0
    assert state.staleness_seconds is None
    assert state.degraded is True  # no successful pull = degraded


def test_snapshot_with_built_index_and_recent_pull(tmp_kb: Path, tmp_path: Path) -> None:
    idx = Index(tmp_path / "idx.db")
    idx.build(tmp_kb, source_commit="abc123")
    now = time.time()
    state = snapshot(idx=idx, last_git_pull_at=now, staleness_degraded_sec=600)
    assert state.kb_commit == "abc123"
    assert state.total_rules == 10
    assert state.index_built_at is not None
    assert state.staleness_seconds is not None
    assert state.staleness_seconds < 5
    assert state.degraded is False


def test_snapshot_degraded_when_stale(tmp_kb: Path, tmp_path: Path) -> None:
    idx = Index(tmp_path / "idx.db")
    idx.build(tmp_kb, source_commit="abc")
    old = time.time() - 1000  # 1000s ago, > 600s default
    state = snapshot(idx=idx, last_git_pull_at=old, staleness_degraded_sec=600)
    assert state.degraded is True
    assert state.staleness_seconds is not None and state.staleness_seconds > 600


def test_snapshot_includes_index_build_fields_defaults(tmp_kb: Path, tmp_path: Path) -> None:
    """Default snapshot reports last_index_build_status='ok' and no error."""
    idx = Index(tmp_path / "idx.db")
    idx.build(tmp_kb, source_commit="x")
    state = snapshot(idx=idx, last_git_pull_at=time.time(), staleness_degraded_sec=600)
    assert state.last_index_build_status == "ok"
    assert state.last_index_error is None
    assert state.last_index_error_at is None
    assert state.last_index_conflicts == []


def test_snapshot_propagates_index_build_failure(tmp_kb: Path, tmp_path: Path) -> None:
    """If we pass failure kwargs, they appear in the HealthState."""
    idx = Index(tmp_path / "idx.db")
    idx.build(tmp_kb, source_commit="x")
    state = snapshot(
        idx=idx,
        last_git_pull_at=time.time(),
        staleness_degraded_sec=600,
        last_index_build_status="failed",
        last_index_error="duplicate id STD-U-007 in files X and Y",
        last_index_error_at=time.time(),
        last_index_conflicts=[{"id": "STD-U-007", "paths": ["X", "Y"]}],
    )
    assert state.last_index_build_status == "failed"
    assert "STD-U-007" in (state.last_index_error or "")
    assert state.last_index_error_at is not None
    assert state.last_index_conflicts == [{"id": "STD-U-007", "paths": ["X", "Y"]}]


def test_health_response_includes_path_locks_held(tmp_kb, tmp_index_path):
    from data_olympus.index import Index
    from data_olympus.tools_read import kb_health_fn

    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    resp = kb_health_fn(
        idx=idx,
        last_git_pull_at=None,
        staleness_degraded_sec=600,
        last_git_push_at=None,
        pending_count=0,
        push_queue_size=0,
        last_index_build_status="ok",
        last_index_error=None,
        last_index_error_at=None,
        last_index_conflicts=[],
        path_locks_held=2,
    )
    assert resp.path_locks_held == 2


def test_health_response_surfaces_live_sessions(tmp_kb, tmp_index_path):
    """live_sessions defaults to None (unobservable) and is threaded when set."""
    from data_olympus.index import Index
    from data_olympus.tools_read import kb_health_fn

    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    default = kb_health_fn(idx=idx, last_git_pull_at=None, staleness_degraded_sec=600)
    assert default.live_sessions is None

    with_count = kb_health_fn(
        idx=idx, last_git_pull_at=None, staleness_degraded_sec=600, live_sessions=3
    )
    assert with_count.live_sessions == 3


def test_server_state_live_session_count(tmp_kb, tmp_index_path):
    """ServerState.live_session_count() returns None with no provider, the
    provider's value when wired, and None (not a crash) when the provider raises."""
    from data_olympus.git_ops import GitOps
    from data_olympus.index import Index
    from data_olympus.server import ServerState

    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    cfg = _config_stub()
    state = ServerState(idx=idx, git=GitOps(tmp_kb), config=cfg)
    assert state.live_session_count() is None

    state.session_count_provider = lambda: 5
    assert state.live_session_count() == 5

    def boom() -> int:
        raise RuntimeError("manager gone")

    state.session_count_provider = boom
    assert state.live_session_count() is None


def _config_stub():
    from pathlib import Path

    from data_olympus.config import Config

    return Config(
        kb_main_path=Path("/kb"),
        kb_index_path=Path("/kb.db"),
        kb_remote_url="",
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        confidence_threshold=0.85,
        http_port=8080,
    )
