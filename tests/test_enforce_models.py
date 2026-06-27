"""Tests for enforcement response models."""
from __future__ import annotations

from data_olympus.models import (
    ComplianceResponse,
    ConsultResponse,
    GateCheckResponse,
    SearchHitModel,
)


def test_consult_response_round_trips() -> None:
    hit = SearchHitModel(id="STD-U-002", path="universal/foundation/STD-U-002.md",
                         title="Writing style", snippet="...", score=1.0)
    resp = ConsultResponse(is_governed_decision=True, rules=[hit],
                           consulted_at=100.0, ttl_seconds=300)
    dumped = resp.model_dump()
    assert dumped["is_governed_decision"] is True
    assert dumped["rules"][0]["id"] == "STD-U-002"
    assert dumped["ttl_seconds"] == 300


def test_gate_check_response_defaults() -> None:
    resp = GateCheckResponse(verdict="allow")
    assert resp.verdict == "allow"
    assert resp.reason == ""
    assert resp.rules == []


def test_compliance_response_defaults() -> None:
    resp = ComplianceResponse()
    assert resp.counts == {}
    assert resp.by_agent == {}
