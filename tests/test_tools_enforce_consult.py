# tests/test_tools_enforce_consult.py
"""Tests for kb_consult_fn."""
from __future__ import annotations

from data_olympus.audit_log import AuditLog
from data_olympus.enforce_policy import ConsultationLedger, IntentClassifier
from data_olympus.tools_enforce import kb_consult_fn


class _FakeIndex:
    """Minimal stand-in exposing the .search and .health surface kb_search_fn uses."""

    def search(self, query, limit=20, tier=None, category=None, status=None,  # noqa: ARG002
               in_force=False, doc_type=None, **kwargs):  # noqa: ARG002
        return []

    def health(self):
        return {"source_commit": "deadbeef"}


def test_consult_records_and_flags_governed(tmp_path) -> None:
    led = ConsultationLedger()
    al = AuditLog(log_path=str(tmp_path / "events.log"))
    resp = kb_consult_fn(
        idx=_FakeIndex(), classifier=IntentClassifier(), ledger=led,
        workspace="proj", intent="add a new caching library",
        source_session="s1", agent_identity="claude",
        ttl_sec=300.0, now=1000.0, audit_log=al,
    )
    assert resp.is_governed_decision is True
    assert resp.ttl_seconds == 300
    assert led.is_fresh(session_id="s1", workspace="proj", now=1000.0, ttl_sec=300.0)


def test_consult_records_even_when_not_governed() -> None:
    led = ConsultationLedger()
    resp = kb_consult_fn(
        idx=_FakeIndex(), classifier=IntentClassifier(), ledger=led,
        workspace="proj", intent="say hello",
        source_session="s1", agent_identity="claude",
        ttl_sec=300.0, now=1000.0, audit_log=None,
    )
    assert resp.is_governed_decision is False
    assert led.is_fresh(session_id="s1", workspace="proj", now=1000.0, ttl_sec=300.0)
