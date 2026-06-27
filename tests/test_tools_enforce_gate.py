# tests/test_tools_enforce_gate.py
"""Tests for kb_gate_check_fn."""
from __future__ import annotations

from data_olympus.audit_log import AuditLog
from data_olympus.enforce_policy import ConsultationLedger, IntentClassifier
from data_olympus.tools_enforce import kb_gate_check_fn


def test_non_governed_action_allows_without_consult() -> None:
    resp = kb_gate_check_fn(
        classifier=IntentClassifier(), ledger=ConsultationLedger(),
        workspace="proj", session_id="s1", tool_name="Edit",
        action_path="/p/src/util/strings.py", action_diff="",
        now=1000.0, ttl_sec=300.0,
    )
    assert resp.verdict == "allow"


def test_governed_action_without_consult_requires_consult(tmp_path) -> None:
    al = AuditLog(log_path=str(tmp_path / "events.log"))
    resp = kb_gate_check_fn(
        classifier=IntentClassifier(), ledger=ConsultationLedger(),
        workspace="proj", session_id="s1", tool_name="Edit",
        action_path="/p/pyproject.toml", action_diff="",
        now=1000.0, ttl_sec=300.0, audit_log=al,
    )
    assert resp.verdict == "consult_required"
    events = list(al.iter_filtered())
    assert any(e["event_type"] == "gate_block" for e in events)


def test_governed_action_with_fresh_consult_allows() -> None:
    led = ConsultationLedger()
    led.record(session_id="s1", workspace="proj", rule_ids=[], now=1000.0)
    resp = kb_gate_check_fn(
        classifier=IntentClassifier(), ledger=led,
        workspace="proj", session_id="s1", tool_name="Edit",
        action_path="/p/pyproject.toml", action_diff="",
        now=1100.0, ttl_sec=300.0,
    )
    assert resp.verdict == "allow"
