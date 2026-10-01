"""Tests for kb_audit_fn."""
from __future__ import annotations

from data_olympus.audit_log import AuditLog
from data_olympus.tools_audit import kb_audit_fn


def test_kb_audit_fn_returns_filtered_events(tmp_path) -> None:
    al = AuditLog(log_path=str(tmp_path / "events.log"))
    al.append({
        "ts": 100.0, "event_type": "propose_memory",
        "status": "committed", "agent_identity": "claude",
    })
    al.append({
        "ts": 200.0, "event_type": "propose_edit",
        "status": "rejected_rate_limited", "agent_identity": "codex",
    })
    resp = kb_audit_fn(audit_log=al, status="committed", limit=100)
    assert resp.returned == 1
    assert resp.events[0].agent_identity == "claude"


def test_kb_audit_fn_limit_hit_flag(tmp_path) -> None:
    al = AuditLog(log_path=str(tmp_path / "events.log"))
    for i in range(5):
        al.append({"ts": float(i), "event_type": "propose_memory", "status": "committed"})
    resp = kb_audit_fn(audit_log=al, limit=3)
    assert resp.returned == 3
    assert resp.limit_hit is True


def test_kb_audit_fn_returns_consult_coverage_and_trigger(tmp_path) -> None:
    """Issue #309: the report reads consults through this surface, so the
    coverage set and trigger must survive it; a legacy row has no coverage."""
    al = AuditLog(log_path=str(tmp_path / "events.log"))
    al.append({
        "ts": 100.0, "event_type": "consult", "status": "recorded",
        "target_path": "proj", "trigger": "explicit",
        "coverage": ["keyword:dependency"],
    })
    al.append({"ts": 200.0, "event_type": "consult", "status": "recorded",
               "target_path": "proj"})
    by_ts = {e.ts: e for e in kb_audit_fn(audit_log=al, limit=100).events}
    assert by_ts[100.0].coverage == ["keyword:dependency"]
    assert by_ts[100.0].trigger == "explicit"
    assert by_ts[200.0].coverage is None
