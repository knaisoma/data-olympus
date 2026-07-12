"""Prometheus /metrics registry with a no-op fallback (issue #69 / KNA-71).

``prometheus-client`` is an OPTIONAL dependency (the ``metrics`` extra). The
counters/gauges below are updated from inside core background loops (refresh,
push-retry, pending-GC) and the MCP tool-call middleware, so a bare import or
attribute access on a standard deployment WITHOUT the extra must never crash the
server. This module therefore exposes a single ``Metrics`` object whose
``inc()`` / ``set()`` / ``observe()`` calls are cheap no-ops when
prometheus-client is absent, and the ``/metrics`` route is registered
conditionally (or returns 501) based on ``Metrics.available``.

Usage from a hot path::

    from data_olympus.metrics import get_metrics
    get_metrics().pending_queue_depth.set(state.pending_count)
    get_metrics().tool_calls.labels(tool="kb_search").inc()

Every handle returned is safe to call unconditionally; the no-op shim mirrors
the prometheus-client metric surface used here (``.set``, ``.inc``,
``.observe``, ``.labels(...)``).
"""
from __future__ import annotations

import logging
from typing import Protocol, cast, runtime_checkable

log = logging.getLogger("data_olympus.metrics")


@runtime_checkable
class MetricHandle(Protocol):
    """The subset of the prometheus-client metric API this project calls. Both a
    real prometheus metric and the ``_NoopMetric`` shim satisfy it, so hot-path
    call sites type-check without per-call ``type: ignore`` noise."""

    def inc(self, amount: float = ...) -> None: ...
    def set(self, value: float) -> None: ...
    def observe(self, amount: float) -> None: ...
    def labels(self, *args: object, **kwargs: object) -> MetricHandle: ...

# Public content type for the exposition response. Imported lazily-safe: a fixed
# string so the route can set it without importing prometheus-client.
CONTENT_TYPE_FALLBACK = "text/plain; version=0.0.4; charset=utf-8"


class _NoopMetric:
    """Stand-in for a prometheus metric when the extra is not installed.

    Supports the subset of the metric API this project calls: ``inc``, ``set``,
    ``observe`` and ``labels`` (which returns another no-op so a labelled chain
    like ``m.labels(tool="x").inc()`` is a no-op end to end)."""

    __slots__ = ()

    def inc(self, amount: float = 1.0) -> None:  # noqa: ARG002
        return None

    def set(self, value: float) -> None:  # noqa: ARG002
        return None

    def observe(self, amount: float) -> None:  # noqa: ARG002
        return None

    def labels(self, *args: object, **kwargs: object) -> _NoopMetric:  # noqa: ARG002
        return self


_NOOP = _NoopMetric()


class Metrics:
    """Metric registry. Real prometheus metrics when the extra is installed;
    otherwise every handle is a shared no-op so hot-path updates never fail.

    Instantiated once per process (see ``get_metrics``). Registering into a
    private ``CollectorRegistry`` (not the global default) keeps a second
    instance (tests) from raising "Duplicated timeseries"."""

    def __init__(self) -> None:
        self.available = False
        self._registry: object | None = None
        # Default every handle to the no-op so attribute access is always safe,
        # even before/without prometheus-client.
        self.pending_queue_depth: MetricHandle = _NOOP
        self.push_queue_depth: MetricHandle = _NOOP
        self.push_queue_frozen: MetricHandle = _NOOP
        self.push_failures: MetricHandle = _NOOP
        self.staleness_seconds: MetricHandle = _NOOP
        self.live_sessions: MetricHandle = _NOOP
        self.tool_calls: MetricHandle = _NOOP
        self.index_build_duration: MetricHandle = _NOOP
        self.index_last_build_timestamp: MetricHandle = _NOOP

        try:
            from prometheus_client import (
                CollectorRegistry,
                Counter,
                Gauge,
                Histogram,
            )
        except Exception:  # pragma: no cover - exercised via the no-extra test path
            log.info(
                "prometheus-client not installed; /metrics disabled and metric "
                "updates are no-ops (install the 'metrics' extra to enable)"
            )
            return

        reg = CollectorRegistry()
        self._registry = reg
        self.available = True

        def _g(name: str, doc: str) -> MetricHandle:
            return cast("MetricHandle", Gauge(name, doc, registry=reg))

        self.pending_queue_depth = _g(
            "data_olympus_pending_queue_depth",
            "Number of entries in the pending-proposal queue.",
        )
        self.push_queue_depth = _g(
            "data_olympus_push_queue_depth",
            "Number of entries in the push queue.",
        )
        self.push_queue_frozen = _g(
            "data_olympus_push_queue_frozen",
            "Number of frozen push-queue entries (hit max_attempts; stuck).",
        )
        self.push_failures = cast(
            "MetricHandle",
            Counter(
                "data_olympus_push_failures_total",
                "Count of push attempts that failed (retryable or frozen).",
                registry=reg,
            ),
        )
        self.staleness_seconds = _g(
            "data_olympus_staleness_seconds",
            "Seconds since the last successful git pull (KB freshness).",
        )
        self.live_sessions = _g(
            "data_olympus_live_sessions",
            "Live streamable-http MCP transport sessions.",
        )
        self.tool_calls = cast(
            "MetricHandle",
            Counter(
                "data_olympus_tool_calls_total",
                "MCP tool calls, labelled by tool name.",
                ["tool"],
                registry=reg,
            ),
        )
        self.index_build_duration = cast(
            "MetricHandle",
            Histogram(
                "data_olympus_index_build_duration_seconds",
                "Wall-clock duration of an index rebuild.",
                registry=reg,
            ),
        )
        self.index_last_build_timestamp = _g(
            "data_olympus_index_last_build_timestamp_seconds",
            "Unix timestamp of the last successful index build.",
        )

    def render(self) -> tuple[bytes, str]:
        """Return ``(body, content_type)`` for the /metrics response.

        Only called when ``available`` is True (the route is 501 otherwise), so
        prometheus-client is guaranteed importable here."""
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

        assert self._registry is not None
        return generate_latest(self._registry), CONTENT_TYPE_LATEST  # type: ignore[arg-type]

    def sync_from_state(
        self,
        *,
        pending_count: int,
        push_queue_size: int,
        push_queue_frozen: int,
        staleness_seconds: float | None,
        live_sessions: int | None,
    ) -> None:
        """Refresh the gauge-style metrics from a health snapshot. Safe no-op
        without the extra. Called from the refresh loop each tick so the gauges
        track the live queue depths and freshness even between scrapes."""
        self.pending_queue_depth.set(pending_count)
        self.push_queue_depth.set(push_queue_size)
        self.push_queue_frozen.set(push_queue_frozen)
        if staleness_seconds is not None:
            self.staleness_seconds.set(staleness_seconds)
        if live_sessions is not None:
            self.live_sessions.set(live_sessions)

    def record_index_build(self, *, duration_seconds: float, ts: float) -> None:
        """Record one index-build's duration and last-build timestamp. No-op
        without the extra."""
        self.index_build_duration.observe(duration_seconds)
        self.index_last_build_timestamp.set(ts)


_METRICS: Metrics | None = None


def get_metrics() -> Metrics:
    """Return the process-wide Metrics singleton, constructing it on first use."""
    global _METRICS
    if _METRICS is None:
        _METRICS = Metrics()
    return _METRICS


def reset_metrics_for_test() -> Metrics:
    """Rebuild the singleton. Tests only: gives each test a fresh registry so
    counter assertions do not see values bled in from another test."""
    global _METRICS
    _METRICS = Metrics()
    return _METRICS


def prometheus_available() -> bool:
    """True when prometheus-client is importable (the /metrics route is live)."""
    return get_metrics().available
