"""KNA-71 (gh #69): optional Prometheus /metrics endpoint.

Covers:
  - /metrics returns valid exposition format when prometheus-client is installed
  - a counter moves when its loop / dispatch chokepoint runs
  - the no-op shim: metric updates and the background-loop wiring never crash
    when prometheus-client is absent, and /metrics returns 501
"""
from __future__ import annotations

import builtins
import importlib
from typing import TYPE_CHECKING

import httpx
import pytest

import data_olympus.metrics as metrics_mod
import data_olympus.server as server
from data_olympus.config import load_config

if TYPE_CHECKING:
    from pathlib import Path

prometheus_client = pytest.importorskip("prometheus_client")


@pytest.fixture()
def _fresh_metrics() -> object:
    """Give each test a fresh registry so counters do not bleed across tests."""
    return metrics_mod.reset_metrics_for_test()


def _build_http_app(tmp_git_kb: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KB_MAIN_PATH", str(tmp_git_kb))
    monkeypatch.setenv("KB_INDEX_PATH", str(tmp_path / "kb.db"))
    cfg = load_config()
    app = server.build_app_from_config(cfg, bootstrap_now=True)
    return app.http_app()


@pytest.mark.asyncio
async def test_metrics_endpoint_returns_exposition_format(
    tmp_git_kb: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    _fresh_metrics: object,
) -> None:
    http_app = _build_http_app(tmp_git_kb, tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=http_app)
    async with (
        http_app.router.lifespan_context({}),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        resp = await client.get("/metrics")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    # Valid exposition format: HELP/TYPE preamble + our metric names present.
    assert "# HELP data_olympus_pending_queue_depth" in body
    assert "# TYPE data_olympus_tool_calls_total counter" in body
    assert "data_olympus_index_last_build_timestamp_seconds" in body


@pytest.mark.asyncio
async def test_tool_call_counter_moves_on_dispatch(
    tmp_git_kb: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    _fresh_metrics: object,
) -> None:
    """The per-tool counter increments when the MCP tool-dispatch chokepoint runs."""
    from fastmcp import Client

    monkeypatch.setenv("KB_MAIN_PATH", str(tmp_git_kb))
    monkeypatch.setenv("KB_INDEX_PATH", str(tmp_path / "kb.db"))
    cfg = load_config()
    app = server.build_app_from_config(cfg, bootstrap_now=True)

    m = metrics_mod.get_metrics()
    before = m.tool_calls.labels(tool="kb_search")._value.get()  # type: ignore[union-attr]
    async with Client(app) as client:
        await client.call_tool("kb_search", {"query": "worktree", "limit": 5})
    after = m.tool_calls.labels(tool="kb_search")._value.get()  # type: ignore[union-attr]
    assert after == before + 1


def test_index_build_metric_records(_fresh_metrics: object) -> None:
    """record_index_build moves the histogram + last-build gauge."""
    m = metrics_mod.get_metrics()
    m.record_index_build(duration_seconds=0.42, ts=1700000000.0)
    # Histogram sample count went up; last-build gauge set.
    samples = {
        s.name: s.value
        for metric in m._registry.collect()  # type: ignore[union-attr]
        for s in metric.samples
    }
    assert samples.get("data_olympus_index_build_duration_seconds_count") == 1.0
    assert samples.get("data_olympus_index_last_build_timestamp_seconds") == 1700000000.0


# ---------------------------------------------------------------------------
# No-extra path: prometheus-client absent -> no-op shim + 501 route
# ---------------------------------------------------------------------------
def test_noop_metrics_when_prometheus_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Metric updates must be silent no-ops when prometheus-client is missing,
    because they run inside core background loops and tool wrappers."""
    real_import = builtins.__import__

    def _blocked_import(name: str, *args: object, **kwargs: object):
        if name == "prometheus_client" or name.startswith("prometheus_client."):
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", _blocked_import)
    reloaded = importlib.reload(metrics_mod)
    try:
        m = reloaded.Metrics()
        assert m.available is False
        # Every hot-path update must not raise.
        m.pending_queue_depth.set(3)  # type: ignore[union-attr]
        m.push_failures.inc()  # type: ignore[union-attr]
        m.tool_calls.labels(tool="kb_search").inc()  # type: ignore[union-attr]
        m.sync_from_state(
            pending_count=1, push_queue_size=2, push_queue_frozen=0,
            staleness_seconds=5.0, live_sessions=1,
        )
        m.record_index_build(duration_seconds=0.1, ts=1.0)
        assert reloaded.prometheus_available.__module__  # callable exists
    finally:
        # Restore the real module so later tests see a working prometheus_client.
        monkeypatch.undo()
        importlib.reload(metrics_mod)


@pytest.mark.asyncio
async def test_metrics_route_501_when_absent(
    tmp_git_kb: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The /metrics route answers 501 (not 404) when the extra is not installed."""
    # Force the singleton into the unavailable state without touching the real
    # module import machinery for the whole app.
    fake = metrics_mod.Metrics.__new__(metrics_mod.Metrics)
    fake.available = False
    fake._registry = None  # type: ignore[attr-defined]
    monkeypatch.setattr(metrics_mod, "_METRICS", fake, raising=False)
    monkeypatch.setattr(metrics_mod, "get_metrics", lambda: fake)

    http_app = _build_http_app(tmp_git_kb, tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=http_app)
    async with (
        http_app.router.lifespan_context({}),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        resp = await client.get("/metrics")
    assert resp.status_code == 501
    assert "metrics" in resp.text.lower()
