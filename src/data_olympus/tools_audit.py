"""kb_audit MCP tool function."""
from __future__ import annotations

from typing import TYPE_CHECKING

from data_olympus.models import AuditEvent, AuditResponse

if TYPE_CHECKING:
    from data_olympus.audit_log import AuditLog


# Cap the reverse scan at a generous multiple of the requested limit so a
# heavily-filtered query (e.g. one rare agent) still walks enough history to fill
# the page without slurping an entire rotated archive when few rows match.
_SCAN_MULTIPLE = 50


def kb_audit_fn(
    *,
    audit_log: AuditLog,
    since: float | None = None,
    agent: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> AuditResponse:
    # A ``since`` filter is a request for history over a window, which may reach
    # back past the live file into rotated segments, so include them. Without a
    # ``since`` the query is "most recent N", answerable from the live file alone
    # (cheaper, and matches the pre-rotation behaviour). The scan is bounded so a
    # large rotated archive cannot turn one query into an unbounded read.
    include_rotated = since is not None
    max_scan = max(1, limit) * _SCAN_MULTIPLE
    events: list[AuditEvent] = []
    for ev in audit_log.iter_filtered(
        since=since, agent=agent, status=status,
        include_rotated=include_rotated, max_scan_events=max_scan,
    ):
        events.append(AuditEvent(**ev))
        if len(events) >= limit:
            break
    return AuditResponse(events=events, returned=len(events), limit_hit=len(events) >= limit)
