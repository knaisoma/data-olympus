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
    push_queue_frozen: int = 0
    last_index_build_status: str = "ok"
    last_index_error: str | None = None
    last_index_error_at: float | None = None
    last_index_conflicts: list[dict[str, object]] = field(default_factory=list)
    last_git_fetch_status: str = "no_change"
    last_git_fetch_error: str | None = None
    last_git_fetch_at: float | None = None
    last_successful_refresh_at: float | None = None
    remote_head_sha: str | None = None
    live_sessions: int | None = None
    # Count of docs whose front-matter was present but malformed at the last index
    # build (WP2b finding (j)). A non-zero value means a doc silently lost its
    # governance metadata (type/status/tier), so it will not be governed or
    # filtered correctly. Surfaced as a WARNING signal; it does NOT flip
    # ``degraded`` (see snapshot()).
    malformed_frontmatter: int = 0


def snapshot(
    *,
    idx: Index,
    last_git_pull_at: float | None,
    staleness_degraded_sec: int,
    last_git_push_at: float | None = None,
    pending_count: int = 0,
    push_queue_size: int = 0,
    push_queue_frozen: int = 0,
    last_index_build_status: str = "ok",
    last_index_error: str | None = None,
    last_index_error_at: float | None = None,
    last_index_conflicts: list[dict[str, object]] | None = None,
    last_git_fetch_status: str = "no_change",
    last_git_fetch_error: str | None = None,
    last_git_fetch_at: float | None = None,
    last_successful_refresh_at: float | None = None,
    remote_head_sha: str | None = None,
    live_sessions: int | None = None,
) -> HealthState:
    """Compose a HealthState from the index and the last-pull/push timestamps.

    A fetch/ff failure freezes ``last_git_pull_at`` (see refresh.git_pull_loop),
    so staleness climbs and ``degraded`` flips through the existing staleness
    path; the ``last_git_fetch_*`` fields surface the immediate cause.
    """
    h = idx.health()
    now = time.time()
    staleness = (now - last_git_pull_at) if last_git_pull_at is not None else None
    # malformed_frontmatter deliberately does NOT contribute to ``degraded``: a
    # doc losing its governance metadata is a data-quality WARNING, not a
    # service-health failure. Flipping degraded would (a) 503 every read via the
    # CLI --no-stale contract and (b) tie the readiness/health signal to author
    # error rather than serviceability. Alert on a non-zero count instead (see
    # docs/operations.md).
    degraded = (
        last_git_pull_at is None
        or (staleness is not None and staleness > staleness_degraded_sec)
        or h["total_docs"] == 0
        or last_index_build_status != "ok"
    )
    _mf = h.get("malformed_frontmatter")
    malformed_frontmatter = _mf if isinstance(_mf, int) else 0
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
        push_queue_frozen=push_queue_frozen,
        last_index_build_status=last_index_build_status,
        last_index_error=last_index_error,
        last_index_error_at=last_index_error_at,
        last_index_conflicts=list(last_index_conflicts or []),
        last_git_fetch_status=last_git_fetch_status,
        last_git_fetch_error=last_git_fetch_error,
        last_git_fetch_at=last_git_fetch_at,
        last_successful_refresh_at=last_successful_refresh_at,
        remote_head_sha=remote_head_sha,
        live_sessions=live_sessions,
        malformed_frontmatter=malformed_frontmatter,
    )
