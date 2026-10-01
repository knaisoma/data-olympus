# src/data_olympus/tools_enforce.py
"""Enforcement tool function implementations: consult, gate-check, compliance.

Decoupled from FastMCP registration, deps passed as kwargs, return pydantic
models. This module mirrors tools_read.py / tools_write.py."""
from __future__ import annotations

from typing import TYPE_CHECKING

from data_olympus.enforce_policy import (
    EXPLICIT_TRIGGER,
    GATE_CLEARANCE_INTENT,
    GATE_CLEARANCE_PAIR,
    PROMPT_HOOK_TRIGGER,
    uncovered_signals,
)
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
    from data_olympus.pending import PendingQueue

ENFORCE_EVENT_TYPES = (
    "consult", "gate_allow", "gate_block", "gate_bypass", "gate_degraded",
)

# Accepted consult triggers; anything else is coerced to explicit (fail-safe:
# an unknown trigger is treated as a real agent consult, never silently dropped
# to a non-clearing prompt-hook consult).
_TRIGGERS = (EXPLICIT_TRIGGER, PROMPT_HOOK_TRIGGER)


def _deny_instruction(
    *, workspace: str, session_id: str, intent: str = "<what you are doing>",
) -> str:
    """The copy-pasteable remediation an agent must run to clear the gate. Echoes
    the exact workspace key and session id (the one parameter an agent cannot
    guess) so the fix does not require the agent to invent either value. Each
    argument is a Python string literal (``repr``), so the call still parses
    when a value holds a quote character (issue #309)."""
    return (
        f"Call kb_consult(workspace={workspace!r}, source_session={session_id!r}, "
        f"intent={intent!r}) then retry."
    )


def _quote(value: str) -> str:
    """``value`` wrapped in the first quote character it does not contain, so
    the consult classifier reads it back as one quoted span."""
    q = next((c for c in "\"'`" if c not in value), '"')
    return f"{q}{value}{q}"


def _suggest_intent(
    uncovered: list[str], *, classifier: IntentClassifier, action_path: str | None,
) -> str:
    """ONE intent whose coverage set clears every uncovered signal: the exact
    path for a path signal (the only place a path signal can come from), the
    quoted command fragment for a command signal, the keyword itself for a
    keyword signal."""
    parts: list[str] = []
    for sig in uncovered:
        kind, _, value = sig.partition(":")
        if kind == "path" and action_path:
            part = f"edit {_quote(action_path)}"
        elif kind == "command":
            part = f"run {_quote(classifier.command_pattern(sig) or value)}"
        else:
            part = value
        if part not in parts:
            parts.append(part)
    # A path signal leads, so the agent sees first the one thing it must name.
    parts.sort(key=lambda p: not p.startswith("edit "))
    return "; ".join(parts)


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
    pending_queue: PendingQueue | None = None,
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
    # Issue #296: what this consult covers, which the gate compares an action's
    # signals against under intent clearance. Wider than ``result.signals``: it
    # also holds the command fragments and paths the intent names.
    coverage = classifier.coverage(intent)
    rules = []
    rule_ids: list[str] = []
    if result.is_governed_decision:
        # in_force=True (issues #109 + #110 slice 2): a consult must surface only
        # CURRENTLY GOVERNING rules. Without this, a draft/superseded/
        # deprecated/rejected doc -- or one graph-excluded via a supersedes
        # edge from an in-force source -- could still rank well enough on bm25
        # to reach the agent as "the rule to follow"; the status reranker only
        # soft-downranks those, it does not exclude them. The unconditional
        # not-expired filter already gave this invariant for free ("kb_consult
        # never returns an expired document"); the graph-exclusion rule is
        # scoped to in_force=True only (see format.validate.
        # graph_excluded_ids_sql), so consult must opt in explicitly to get
        # the same guarantee for a graph-excluded doc. The same flag also
        # applies the issue #109 memory-inbox floor, so an unreviewed
        # agent-written memory (or forged inbox frontmatter) is excluded too.
        search = kb_search_fn(idx=idx, query=intent, limit=limit, in_force=True)
        rules = list(search.hits)
        rule_ids = [h.id for h in search.hits]
    ledger.record(
        session_id=source_session, workspace=workspace, rule_ids=rule_ids, now=now,
        trigger=trigger, signals=coverage,
    )
    if audit_log is not None:
        audit_log.append({
            "ts": now, "event_type": "consult", "status": "recorded",
            "agent_identity": agent_identity, "source_session": source_session,
            "target_path": workspace, "trigger": trigger,
            "reason": ",".join(result.signals) if result.signals else "",
            # Issue #309: what this consult covered, so `kb enforce report`
            # can judge a commit by the same coverage rule as the live gate.
            "coverage": coverage,
        })
    pending_actions = pending_actions_for(getattr(idx, "maintenance_state", None))
    # Governed-lane feedback loop (issue #112): when THIS session has demoted
    # writes on record, surface a CTA item alongside the maintenance-ledger
    # items above so a demotion is never silent even if the agent never
    # thinks to run kb_session_recap on its own. Omitted (no item added) when
    # the session has no demotions, or there is no audit log to query.
    if audit_log is not None:
        from data_olympus.tools_recap import kb_session_recap_fn
        recap = kb_session_recap_fn(audit_log=audit_log, source_session=source_session)
        if recap.demoted_to_pending > 0:
            # KNA-72 / gh #137: reconcile the CTA against the LIVE pending queue.
            # ``recap.demoted_to_pending`` is the session-LIFETIME demotion count
            # from the audit log, which never decreases when a demoted entry is
            # later approved or rejected. Using it verbatim made the CTA claim
            # "N write(s) ... are awaiting operator review" long after every
            # demoted entry was resolved (kb_health pending_count 0,
            # kb_list_pending empty), so the count was neither accurate nor
            # actionable. When a pending queue is available, count only the
            # entries STILL on disk for this source_session and OMIT the item
            # entirely once none remain; the recap's lifetime tally is left as-is
            # (that surface is intended). Without a pending queue (old callers /
            # unit tests that do not wire one) fall back to the lifetime count.
            if pending_queue is not None:
                live_count = sum(
                    1 for e in pending_queue.list()
                    if e.get("source_session") == source_session
                )
            else:
                live_count = recap.demoted_to_pending
            if live_count > 0:
                item = {
                    "kind": "demoted_writes",
                    "message": (
                        f"{live_count} write(s) in this session are "
                        f"awaiting operator review (governed-lane write "
                        f"protection or low confidence). Surface this to the "
                        f"operator; run `kb pending` / kb_session_recap for "
                        f"details. Act only on operator confirmation."
                    ),
                    "count": live_count,
                }
                pending_actions = [*(pending_actions or []), item]
    return ConsultResponse(
        is_governed_decision=result.is_governed_decision,
        rules=rules, consulted_at=now, ttl_seconds=int(ttl_sec),
        pending_actions=pending_actions,
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
    clearance: str = GATE_CLEARANCE_INTENT,
) -> GateCheckResponse:
    """Decide whether a pending code action may proceed. Governed actions require
    a fresh explicit consultation on record for (session_id, workspace).

    Under ``clearance="intent"`` (KB_GATE_CLEARANCE, issue #296) that
    consultation must also cover every signal of the action, directly or
    through the family mapping in ``enforce_policy``; a denial names the
    uncovered signals and suggests one intent that covers them all. Under
    ``"pair"`` any fresh explicit consult clears, the pre-#296 rule."""
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
    if clearance == GATE_CLEARANCE_PAIR:
        fresh = ledger.is_fresh(
            session_id=session_id, workspace=workspace, now=now, ttl_sec=ttl_sec,
            require_explicit=True,
        )
        uncovered = [] if fresh else list(result.signals)
    else:
        uncovered = uncovered_signals(result.signals, ledger.fresh_signals(
            session_id=session_id, workspace=workspace, now=now, ttl_sec=ttl_sec,
        ))
    if not uncovered:
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
            "reason": ",".join(result.signals), "uncovered": uncovered,
        })
    if clearance == GATE_CLEARANCE_PAIR:
        reason = (
            "governed action without a fresh explicit consultation. "
            + _deny_instruction(workspace=workspace, session_id=session_id)
        )
    else:
        reason = (
            "governed action not covered by a fresh explicit consultation; "
            f"uncovered signals: {', '.join(uncovered)}. "
            + _deny_instruction(
                workspace=workspace, session_id=session_id,
                intent=_suggest_intent(
                    uncovered, classifier=classifier, action_path=action_path,
                ),
            )
        )
    return GateCheckResponse(
        verdict="consult_required", reason=reason,
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
    skipped = 0
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
        who = ev.get("agent_identity") or "unknown"
        if not isinstance(who, str):
            # A malformed line (issue #310) cannot be attributed to an agent.
            # Skip and count it rather than failing the whole aggregate.
            skipped += 1
            continue
        counts[et] = counts.get(et, 0) + 1
        bucket = by_agent.setdefault(who, {})
        bucket[et] = bucket.get(et, 0) + 1
    return ComplianceResponse(counts=counts, by_agent=by_agent, skipped=skipped)


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
