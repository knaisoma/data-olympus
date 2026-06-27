# tests/test_tools_enforce_compliance.py
"""Tests for kb_compliance_fn."""
from __future__ import annotations

from data_olympus.audit_log import AuditLog
from data_olympus.tools_enforce import kb_compliance_fn


def test_compliance_counts_enforce_events_only(tmp_path) -> None:
    al = AuditLog(log_path=str(tmp_path / "events.log"))
    al.append({"ts": 1.0, "event_type": "consult", "status": "recorded",
               "agent_identity": "claude"})
    al.append({"ts": 2.0, "event_type": "gate_block", "status": "consult_required",
               "agent_identity": "claude"})
    al.append({"ts": 3.0, "event_type": "propose_memory", "status": "committed",
               "agent_identity": "claude"})  # not an enforce event
    resp = kb_compliance_fn(audit_log=al)
    assert resp.counts.get("consult") == 1
    assert resp.counts.get("gate_block") == 1
    assert "propose_memory" not in resp.counts
    assert resp.by_agent["claude"]["gate_block"] == 1
