# src/data_olympus/tools_enforce.py
"""Enforcement tool function implementations: consult, gate-check, compliance.

Decoupled from FastMCP registration, deps passed as kwargs, return pydantic
models — mirrors tools_read.py / tools_write.py."""
from __future__ import annotations

from typing import TYPE_CHECKING

from data_olympus.enforce_policy import EXPLICIT_TRIGGER, PROMPT_HOOK_TRIGGER
from data_olympus.maintenance import pending_actions_for
from data_olympus.models import (
    ComplianceResponse,
    ConsultResponse,
    GateCheckResponse,
    RecordEventResponse,
)
from data_olympus.tools_read import kb_search_fn

if TYPE_CHECKING:
    from data_olympus.audit_log import AuditLog
    from data_olympus.enforce_policy import ConsultationLedger, IntentClassifier
    from data_olympus.index import Index

ENFORCE_EVENT_TYPES = (
    "consult", "gate_allow", "gate_block", "gate_bypass", "gate_degraded",
)

# Accepted consult triggers; anything else is coerced to explicit (fail-safe:
# an unknown trigger is treated as a real agent consult, never silently dropped
# to a non-clearing prompt-hook consult).
_TRIGGERS = (EXPLICIT_TRIGGER, PROMPT_HOOK_TRIGGER)


def _deny_instruction(*, workspace: str, session_id: str) -> str:
    """The copy-pasteable remediation an agent must run to clear the gate. Echoes
    the exact workspace key and session id (the one parameter an agent cannot
    guess) so the fix does not require the agent to invent either value."""
    return (
        f"Call kb_consult(workspace='{workspace}', source_session='{session_id}', "
        f"intent='<what you are doing>') then retry."
    )


def kb_consult_fn(
    *,
    idx: Index,
    classifier: IntentClassifier,
    ledger: ConsultationLedger,
    workspace: str,
    intent: str,
    source_session: str,
    agent_identity: str,
    ttl_sec: float,
    now: float,
    audit_log: AuditLog | None = None,
    limit: int = 5,
    trigger: str = EXPLICIT_TRIGGER,
) -> ConsultResponse:
    """Classify the intent, retrieve governing rules when governed, and record a
    consultation in the ledger keyed by (source_session, workspace).

    ``trigger`` distinguishes a deliberate agent consult (EXPLICIT_TRIGGER, the
    default, which clears the gate) from an installer prompt-hook auto-consult
    (PROMPT_HOOK_TRIGGER, recorded for audit/compliance but never gate-clearing).
    Old clients that omit the field are treated as explicit, since a bare consult
    call is always a real agent action.

    Retrieval is HARD-filtered to the in-force class (issue #109:
    ``in_force=True`` on the internal search), so this enforcement surface can
    never present an unreviewed, proposed, retired, expired, upcoming, or
    memory-inbox document as a governing rule. Previously this ran an
    unfiltered search, so e.g. a server-rendered agent memory (before it is
    reviewed) or a superseded decision could be handed back as "the" rule for
    an intent.

    The response's ``pending_actions`` (issue #113) surfaces open maintenance
    items (missing ``status`` fields, recently-expired/expiring-soon docs)
    ONLY when the computed corpus state is dirty; it is omitted entirely on a
    clean corpus. When present, surface it to the operator and act on it only
    with operator confirmation -- do not silently start remediating the
    corpus.
    """
    trigger = trigger if trigger in _TRIGGERS else EXPLICIT_TRIGGER
    result = classifier.classify(intent=intent)
    rules = []
    rule_ids: list[str] = []
    if result.is_governed_decision:
        search = kb_search_fn(idx=idx, query=intent, limit=limit, in_force=True)
        rules = list(search.hits)
        rule_ids = [h.id for h in search.hits]
    ledger.record(
        session_id=source_session, workspace=workspace, rule_ids=rule_ids, now=now,
        trigger=trigger,
    )
    if audit_log is not None:
        audit_log.append({
            "ts": now, "event_type": "consult", "status": "recorded",
            "agent_identity": agent_identity, "source_session": source_session,
            "target_path": workspace, "trigger": trigger,
            "reason": ",".join(result.signals) if result.signals else "",
        })
    return ConsultResponse(
        is_governed_decision=result.is_governed_decision,
        rules=rules, consulted_at=now, ttl_seconds=int(ttl_sec),
        pending_actions=pending_actions_for(getattr(idx, "maintenance_state", None)),
    )


def kb_gate_check_fn(
    *,
    classifier: IntentClassifier,
    ledger: ConsultationLedger,
    workspace: str,
    session_id: str,
    tool_name: str,  # noqa: ARG001  part of the gate-check contract; reserved for richer policy
    action_path: str | None,
    action_diff: str,
    now: float,
    ttl_sec: float,
    audit_log: AuditLog | None = None,
) -> GateCheckResponse:
    """Decide whether a pending code action may proceed. Governed actions require
    a fresh consultation on record for (session_id, workspace)."""
    result = classifier.classify(action_path=action_path, action_diff=action_diff)
    if not result.is_governed_decision:
        return GateCheckResponse(
            verdict="allow", reason="action not governed",
            session_id=session_id, workspace=workspace,
        )
    # Gate policy: only a fresh EXPLICIT consult clears the gate. A prompt-hook
    # auto-consult is recorded (audit/compliance) but never satisfies this check,
    # so the gate means "the agent explicitly consulted", not "an HTTP call
    # happened this session".
    fresh = ledger.is_fresh(
        session_id=session_id, workspace=workspace, now=now, ttl_sec=ttl_sec,
        require_explicit=True,
    )
    if fresh:
        if audit_log is not None:
            audit_log.append({
                "ts": now, "event_type": "gate_allow", "status": "allow",
                "source_session": session_id, "target_path": action_path or workspace,
                "reason": ",".join(result.signals),
            })
        return GateCheckResponse(
            verdict="allow", reason="fresh explicit consultation on record",
            session_id=session_id, workspace=workspace,
        )
    if audit_log is not None:
        audit_log.append({
            "ts": now, "event_type": "gate_block", "status": "consult_required",
            "source_session": session_id, "target_path": action_path or workspace,
            "reason": ",".join(result.signals),
        })
    return GateCheckResponse(
        verdict="consult_required",
        reason=(
            "governed action without a fresh explicit consultation. "
            + _deny_instruction(workspace=workspace, session_id=session_id)
        ),
        session_id=session_id, workspace=workspace,
    )


def kb_compliance_fn(
    *,
    audit_log: AuditLog,
    since: float | None = None,
    agent: str | None = None,
) -> ComplianceResponse:
    """Aggregate enforcement events (consult / gate_*) into overall and per-agent
    counts. Ignores non-enforcement audit events."""
    counts: dict[str, int] = {}
    by_agent: dict[str, dict[str, int]] = {}
    # A ``since`` window may reach into rotated segments; include them so the
    # aggregate is complete over the requested window (the ``since`` floor bounds
    # the scan). Without ``since`` the aggregate is over the live file only,
    # matching the pre-rotation behaviour.
    for ev in audit_log.iter_filtered(
        since=since, agent=agent, include_rotated=since is not None,
    ):
        et = ev.get("event_type", "")
        if et not in ENFORCE_EVENT_TYPES:
            continue
        counts[et] = counts.get(et, 0) + 1
        who = ev.get("agent_identity") or "unknown"
        bucket = by_agent.setdefault(who, {})
        bucket[et] = bucket.get(et, 0) + 1
    return ComplianceResponse(counts=counts, by_agent=by_agent)


RECORDABLE_EVENT_TYPES = ("gate_bypass", "gate_degraded")


def kb_record_event_fn(
    *,
    audit_log: AuditLog,
    event_type: str,
    workspace: str,
    agent_identity: str,
    source_session: str,
    reason: str,
    now: float,
) -> RecordEventResponse:
    """Append a client-reported enforcement event (gate_bypass / gate_degraded)
    to the audit log. Rejects any other event type so clients cannot forge
    consult/gate_allow/gate_block rows."""
    if event_type not in RECORDABLE_EVENT_TYPES:
        raise ValueError(f"event_type must be one of {RECORDABLE_EVENT_TYPES}")
    audit_log.append({
        "ts": now, "event_type": event_type, "status": event_type,
        "agent_identity": agent_identity, "source_session": source_session,
        "target_path": workspace, "reason": reason,
    })
    return RecordEventResponse(recorded=True, event_type=event_type)
