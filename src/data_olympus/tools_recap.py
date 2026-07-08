"""kb_session_recap: per-session write summary over the audit log (issue #112).

Part of the governed-lane feedback loop: "agents can propose, only humans can
promote" must never demote a write silently. Alongside the per-write
``operator_prompt`` instruction (see ``tools_write.py`` /
``tools_onboarding.py``), a per-session recap answers "what happened this
session" -- N committed, M demoted-to-pending, K rejected -- so an agent (or
the operator) can check the tally at any point, and so ``kb_consult``'s
``pending_actions`` envelope can surface it proactively when a session has
open demotions (see ``tools_enforce.kb_consult_fn``).

"Demoted to pending" here is any ``pending_confirmation`` audit event for the
session, whether it parked for a plain low-confidence reason or a governed-
lane demotion (status clamp / governed target) -- from the operator's
perspective both mean "this write awaits review before it takes effect".
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from data_olympus.models import SessionRecapResponse

if TYPE_CHECKING:
    from data_olympus.audit_log import AuditLog

# Generous default scan bound: a session recap wants the full session
# history, but an unbounded scan of a huge rotated archive would be an
# expensive read-tool call. 20000 covers a very long-running session's audit
# footprint comfortably; callers with a specific need can override it.
_DEFAULT_MAX_SCAN_EVENTS = 20000


def kb_session_recap_fn(
    *,
    audit_log: AuditLog,
    source_session: str,
    max_scan_events: int | None = _DEFAULT_MAX_SCAN_EVENTS,
) -> SessionRecapResponse:
    """Tally committed / demoted-to-pending / rejected audit events for
    ``source_session``, most-recent history first (bounded by
    ``max_scan_events``, walking rotated segments too so a long-running
    session's earlier events are not silently dropped once the live log
    rotates).
    """
    committed = 0
    demoted = 0
    rejected = 0
    for ev in audit_log.iter_filtered(
        include_rotated=True, max_scan_events=max_scan_events,
    ):
        if ev.get("source_session") != source_session:
            continue
        status = ev.get("status", "")
        if status == "committed":
            committed += 1
        elif status == "pending_confirmation":
            demoted += 1
        elif isinstance(status, str) and status.startswith("rejected"):
            rejected += 1
    return SessionRecapResponse(
        source_session=source_session,
        committed=committed,
        demoted_to_pending=demoted,
        rejected=rejected,
    )
