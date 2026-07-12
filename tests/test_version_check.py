"""Tests for the periodic version-check background task (issue #146 / KNA-68).

Covers the four guarantees in the spec:
  1. The /api/v1/health field is populated FROM THE CACHE when a newer version
     has been detected.
  2. There is NO outbound network call on the request path (the health route
     reads the cached ServerState fields only).
  3. The offline gate (KB_DISABLE_VERSION_CHECK) suppresses the check so an
     air-gapped deployment makes zero outbound calls.
  4. The cache prevents a per-request lookup (repeated health hits never call
     latest_version()).
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx
import pytest

from data_olympus import setup_wizard
from data_olympus.config import load_config
from data_olympus.server import build_app
from data_olympus.setup_wizard import VersionInfo
from data_olympus.version_check import _compute_once, version_check_loop

if TYPE_CHECKING:
    from pathlib import Path


# --------------------------------------------------------------------------- #
# _compute_once: the pure, thread-run lookup + comparison
# --------------------------------------------------------------------------- #
def test_compute_once_detects_newer_version(monkeypatch) -> None:
    """A published version strictly newer than the installed one -> update_available.
    The installed version is read from VersionInfo.installed (what
    latest_version() returns), so mocking latest_version() fully controls it."""
    monkeypatch.setattr(
        setup_wizard,
        "latest_version",
        lambda: VersionInfo("0.4.2", "9.9.9", "pypi"),
    )
    latest, installed, newer = _compute_once()
    assert latest == "9.9.9"
    assert installed == "0.4.2"
    assert newer is True


def test_compute_once_no_update_when_installed_is_current(monkeypatch) -> None:
    monkeypatch.setattr(
        setup_wizard,
        "latest_version",
        lambda: VersionInfo("0.4.2", "0.4.2", "pypi"),
    )
    latest, _installed, newer = _compute_once()
    assert latest == "0.4.2"
    assert newer is False


def test_compute_once_falls_back_when_version_tuple_rejects(monkeypatch) -> None:
    monkeypatch.setattr(
        setup_wizard,
        "latest_version",
        lambda: VersionInfo("0.4.2-dev", "0.4.2", "pypi"),
    )

    def bad_tuple(_version: str) -> tuple[int, ...]:
        raise ValueError("bad version")

    monkeypatch.setattr(setup_wizard, "_version_tuple", bad_tuple)
    latest, installed, newer = _compute_once()
    assert latest == "0.4.2"
    assert installed == "0.4.2-dev"
    assert newer is True


def test_compute_once_offline_returns_none(monkeypatch) -> None:
    """An offline lookup (latest is None) degrades to (None, False), no crash."""
    monkeypatch.setattr(
        setup_wizard,
        "latest_version",
        lambda: VersionInfo("0.4.2", None, "offline"),
    )
    latest, _installed, newer = _compute_once()
    assert latest is None
    assert newer is False


# --------------------------------------------------------------------------- #
# version_check_loop: refreshes the cache on ServerState
# --------------------------------------------------------------------------- #
class _FakeState:
    latest_version: str | None = None
    update_available: bool = False


@pytest.mark.asyncio
async def test_loop_refreshes_cache_then_can_be_cancelled(monkeypatch) -> None:
    monkeypatch.setattr(
        setup_wizard,
        "latest_version",
        lambda: VersionInfo("0.4.2", "9.9.9", "pypi"),
    )
    state = _FakeState()
    task = asyncio.create_task(
        version_check_loop(state, interval_sec=3600),  # type: ignore[arg-type]
    )
    # Yield until the first (immediate) refresh has populated the cache.
    for _ in range(200):
        await asyncio.sleep(0)
        if state.latest_version is not None:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state.latest_version == "9.9.9"
    assert state.update_available is True


@pytest.mark.asyncio
async def test_loop_can_skip_the_immediate_first_check(monkeypatch) -> None:
    calls = {"latest": 0}

    def _counting() -> VersionInfo:
        calls["latest"] += 1
        return VersionInfo("0.4.2", "9.9.9", "pypi")

    async def _cancel_sleep(_interval: int) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(setup_wizard, "latest_version", _counting)
    monkeypatch.setattr("data_olympus.version_check.asyncio.sleep", _cancel_sleep)

    state = _FakeState()
    with pytest.raises(asyncio.CancelledError):
        await version_check_loop(
            state,
            interval_sec=3600,  # type: ignore[arg-type]
            run_once_immediately=False,
        )
    assert calls["latest"] == 0
    assert state.latest_version is None


@pytest.mark.asyncio
async def test_loop_propagates_cancelled_during_lookup(monkeypatch) -> None:
    async def _cancel_to_thread(_fn):
        raise asyncio.CancelledError

    monkeypatch.setattr("data_olympus.version_check.asyncio.to_thread", _cancel_to_thread)

    with pytest.raises(asyncio.CancelledError):
        await version_check_loop(_FakeState(), interval_sec=3600)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_server_starts_version_check_task_unless_disabled(monkeypatch) -> None:
    from types import SimpleNamespace

    import data_olympus.version_check as version_check
    from data_olympus.server import _maybe_start_version_check_task

    started: list[int] = []

    async def _fake_loop(_state, *, interval_sec: int) -> None:
        started.append(interval_sec)
        await asyncio.Event().wait()

    monkeypatch.setattr(version_check, "version_check_loop", _fake_loop)

    tasks: list[asyncio.Task[object]] = []
    _maybe_start_version_check_task(
        tasks,
        _FakeState(),  # type: ignore[arg-type]
        SimpleNamespace(disable_version_check=False, version_check_interval_sec=123),  # type: ignore[arg-type]
    )
    assert len(tasks) == 1
    assert tasks[0].get_name() == "version_check_loop"
    tasks[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await tasks[0]

    tasks = []
    _maybe_start_version_check_task(
        tasks,
        _FakeState(),  # type: ignore[arg-type]
        SimpleNamespace(disable_version_check=True, version_check_interval_sec=123),  # type: ignore[arg-type]
    )
    assert tasks == []


# --------------------------------------------------------------------------- #
# /api/v1/health reads the cache, never the network (request path)
# --------------------------------------------------------------------------- #
@pytest.fixture
def app(tmp_kb: Path, tmp_path: Path):
    return build_app(
        kb_main_path=tmp_kb,
        kb_index_path=tmp_path / "idx.db",
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        bootstrap_now=True,
    )


@pytest.mark.asyncio
async def test_health_reports_cached_version_fields(app, monkeypatch) -> None:
    """When the background task has cached a newer version, the health payload
    surfaces it. The request path must NOT call latest_version()."""
    def _boom() -> VersionInfo:  # pragma: no cover - must never run
        raise AssertionError("latest_version() called on the request path")

    monkeypatch.setattr(setup_wizard, "latest_version", _boom)

    state = app._dolympus_state  # type: ignore[attr-defined]
    # Simulate what the background task would have cached.
    state.latest_version = "9.9.9"
    state.update_available = True

    http_app = app.http_app()
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["latest_version"] == "9.9.9"
    assert body["update_available"] is True


@pytest.mark.asyncio
async def test_health_no_outbound_and_cache_prevents_per_request_lookup(
    app, monkeypatch,
) -> None:
    """Repeated health requests never call latest_version() (no per-request
    network lookup); the cache is the only source."""
    calls = {"n": 0}

    def _counting() -> VersionInfo:
        calls["n"] += 1
        return VersionInfo("0.4.2", "9.9.9", "pypi")

    monkeypatch.setattr(setup_wizard, "latest_version", _counting)

    http_app = app.http_app()
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(3):
            resp = await client.get("/api/v1/health")
            assert resp.status_code == 200
    assert calls["n"] == 0  # zero outbound lookups from the request path


# --------------------------------------------------------------------------- #
# Config gate: KB_DISABLE_VERSION_CHECK
# --------------------------------------------------------------------------- #
def test_config_gate_defaults_off(monkeypatch) -> None:
    monkeypatch.delenv("KB_DISABLE_VERSION_CHECK", raising=False)
    monkeypatch.delenv("KB_VERSION_CHECK_INTERVAL_SEC", raising=False)
    cfg = load_config()
    assert cfg.disable_version_check is False
    assert cfg.version_check_interval_sec == 86400


def test_config_gate_can_disable(monkeypatch) -> None:
    monkeypatch.setenv("KB_DISABLE_VERSION_CHECK", "1")
    cfg = load_config()
    assert cfg.disable_version_check is True


def test_config_interval_override(monkeypatch) -> None:
    monkeypatch.setenv("KB_VERSION_CHECK_INTERVAL_SEC", "60")
    cfg = load_config()
    assert cfg.version_check_interval_sec == 60


@pytest.mark.asyncio
async def test_health_omits_version_fields_before_first_check(app) -> None:
    """Before any check has run (fresh state), the DEFAULT compact health payload
    omits latest_version (None -> dropped by compact_dump). update_available is a
    bool that a monitor branches on, so like other always-present booleans it
    stays present and False. Verbose mode carries both."""
    http_app = app.http_app()
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/health")
        verbose = await client.get("/api/v1/health", params={"verbose": "true"})
    body = resp.json()
    assert "latest_version" not in body  # None -> omitted in compact mode
    assert body["update_available"] is False  # bool stays present, defaults False
    # verbose=true restores every field, including the null latest_version.
    vbody = verbose.json()
    assert vbody["latest_version"] is None
    assert vbody["update_available"] is False
