# src/data_olympus/tools_enforce.py
"""Enforcement tool function implementations: consult, gate-check, compliance.

Decoupled from FastMCP registration, deps passed as kwargs, return pydantic
models — mirrors tools_read.py / tools_write.py."""
from __future__ import annotations

from typing import TYPE_CHECKING

from data_olympus.models import (
    ComplianceResponse,
    ConsultResponse,
    GateCheckResponse,
)
from data_olympus.tools_read import kb_search_fn

if TYPE_CHECKING:
    from data_olympus.audit_log import AuditLog
    from data_olympus.enforce_policy import ConsultationLedger, IntentClassifier
    from data_olympus.index import Index

ENFORCE_EVENT_TYPES = (
    "consult", "gate_allow", "gate_block", "gate_bypass", "gate_degraded",
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
) -> ConsultResponse:
    """Classify the intent, retrieve governing rules when governed, and record a
    consultation in the ledger keyed by (source_session, workspace)."""
    result = classifier.classify(intent=intent)
    rules = []
    rule_ids: list[str] = []
    if result.is_governed_decision:
        search = kb_search_fn(idx=idx, query=intent, limit=limit)
        rules = list(search.hits)
        rule_ids = [h.id for h in search.hits]
    ledger.record(
        session_id=source_session, workspace=workspace, rule_ids=rule_ids, now=now
    )
    if audit_log is not None:
        audit_log.append({
            "ts": now, "event_type": "consult", "status": "recorded",
            "agent_identity": agent_identity, "source_session": source_session,
            "target_path": workspace,
            "reason": ",".join(result.signals) if result.signals else "",
        })
    return ConsultResponse(
        is_governed_decision=result.is_governed_decision,
        rules=rules, consulted_at=now, ttl_seconds=int(ttl_sec),
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
        return GateCheckResponse(verdict="allow", reason="action not governed")
    fresh = ledger.is_fresh(
        session_id=session_id, workspace=workspace, now=now, ttl_sec=ttl_sec
    )
    if fresh:
        if audit_log is not None:
            audit_log.append({
                "ts": now, "event_type": "gate_allow", "status": "allow",
                "source_session": session_id, "target_path": action_path or workspace,
                "reason": ",".join(result.signals),
            })
        return GateCheckResponse(
            verdict="allow", reason="fresh consultation on record"
        )
    if audit_log is not None:
        audit_log.append({
            "ts": now, "event_type": "gate_block", "status": "consult_required",
            "source_session": session_id, "target_path": action_path or workspace,
            "reason": ",".join(result.signals),
        })
    return GateCheckResponse(
        verdict="consult_required",
        reason="governed action without a fresh consultation; call kb_consult first",
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
    for ev in audit_log.iter_filtered(since=since, agent=agent):
        et = ev.get("event_type", "")
        if et not in ENFORCE_EVENT_TYPES:
            continue
        counts[et] = counts.get(et, 0) + 1
        who = ev.get("agent_identity") or "unknown"
        bucket = by_agent.setdefault(who, {})
        bucket[et] = bucket.get(et, 0) + 1
    return ComplianceResponse(counts=counts, by_agent=by_agent)
