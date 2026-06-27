"""kb_record_event_fn allowlist + append."""
from __future__ import annotations

import pytest

from data_olympus.audit_log import AuditLog
from data_olympus.tools_enforce import kb_record_event_fn


def test_record_allowed_event(tmp_path) -> None:
    al = AuditLog(log_path=str(tmp_path / "events.log"))
    resp = kb_record_event_fn(
        audit_log=al, event_type="gate_bypass", workspace="proj",
        agent_identity="codex", source_session="s", reason="sha123", now=1.0)
    assert resp.recorded is True
    assert resp.event_type == "gate_bypass"
    events = list(al.iter_filtered())
    assert events and events[0]["event_type"] == "gate_bypass"
    assert events[0]["target_path"] == "proj"


def test_record_disallowed_event_raises(tmp_path) -> None:
    al = AuditLog(log_path=str(tmp_path / "events.log"))
    with pytest.raises(ValueError):
        kb_record_event_fn(
            audit_log=al, event_type="consult", workspace="proj",
            agent_identity="x", source_session="s", reason="", now=1.0)
