"""kb_audit MCP tool function."""
from __future__ import annotations

from typing import TYPE_CHECKING

from data_olympus.models import AuditEvent, AuditResponse

if TYPE_CHECKING:
    from data_olympus.audit_log import AuditLog


def kb_audit_fn(
    *,
    audit_log: AuditLog,
    since: float | None = None,
    agent: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> AuditResponse:
    events: list[AuditEvent] = []
    for ev in audit_log.iter_filtered(since=since, agent=agent, status=status):
        events.append(AuditEvent(**ev))
        if len(events) >= limit:
            break
    return AuditResponse(events=events, returned=len(events), limit_hit=len(events) >= limit)
