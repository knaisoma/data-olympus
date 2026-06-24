"""Aggregated health snapshot for the kb_health MCP tool and /healthz endpoint."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from data_olympus.index import Index


@dataclass(frozen=True, slots=True)
class HealthState:
    """Frozen health snapshot returned by kb_health and /healthz.

    Write-side fields default to 0 / None in slice 2A (no write path yet);
    they are filled in by slice 2C when the pending queue + push queue arrive."""

    kb_commit: str
    index_built_at: float | None
    total_rules: int
    last_git_pull_at: float | None
    last_git_push_at: float | None
    staleness_seconds: float | None
    degraded: bool
    db_size_bytes: int
    pending_count: int = 0
    push_queue_size: int = 0
    last_index_build_status: str = "ok"
    last_index_error: str | None = None
    last_index_error_at: float | None = None
    last_index_conflicts: list[dict[str, object]] = field(default_factory=list)


def snapshot(
    *,
    idx: Index,
    last_git_pull_at: float | None,
    staleness_degraded_sec: int,
    last_git_push_at: float | None = None,
    pending_count: int = 0,
    push_queue_size: int = 0,
    last_index_build_status: str = "ok",
    last_index_error: str | None = None,
    last_index_error_at: float | None = None,
    last_index_conflicts: list[dict[str, object]] | None = None,
) -> HealthState:
    """Compose a HealthState from the index and the last-pull/push timestamps."""
    h = idx.health()
    now = time.time()
    staleness = (now - last_git_pull_at) if last_git_pull_at is not None else None
    degraded = (
        last_git_pull_at is None
        or (staleness is not None and staleness > staleness_degraded_sec)
        or h["total_docs"] == 0
        or last_index_build_status != "ok"
    )
    return HealthState(
        kb_commit=str(h["source_commit"]),
        index_built_at=h["index_built_at"] if isinstance(h["index_built_at"], float) else None,
        total_rules=int(h["total_docs"]) if isinstance(h["total_docs"], int) else 0,
        last_git_pull_at=last_git_pull_at,
        last_git_push_at=last_git_push_at,
        staleness_seconds=staleness,
        degraded=degraded,
        db_size_bytes=int(h["db_size_bytes"]) if isinstance(h["db_size_bytes"], int) else 0,
        pending_count=pending_count,
        push_queue_size=push_queue_size,
        last_index_build_status=last_index_build_status,
        last_index_error=last_index_error,
        last_index_error_at=last_index_error_at,
        last_index_conflicts=list(last_index_conflicts or []),
    )
