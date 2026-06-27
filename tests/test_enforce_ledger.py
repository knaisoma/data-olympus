"""Tests for the in-memory consultation ledger."""
from __future__ import annotations

from data_olympus.enforce_policy import ConsultationLedger


def test_record_then_fresh_within_ttl() -> None:
    led = ConsultationLedger()
    led.record(session_id="s1", workspace="proj", rule_ids=["STD-U-002"], now=1000.0)
    assert led.is_fresh(session_id="s1", workspace="proj", now=1100.0, ttl_sec=300.0)


def test_stale_after_ttl() -> None:
    led = ConsultationLedger()
    led.record(session_id="s1", workspace="proj", rule_ids=[], now=1000.0)
    assert not led.is_fresh(session_id="s1", workspace="proj", now=1400.0, ttl_sec=300.0)


def test_unknown_key_is_not_fresh() -> None:
    led = ConsultationLedger()
    assert not led.is_fresh(session_id="nope", workspace="proj", now=1.0, ttl_sec=300.0)


def test_keys_are_isolated_per_session_and_workspace() -> None:
    led = ConsultationLedger()
    led.record(session_id="s1", workspace="projA", rule_ids=[], now=1000.0)
    assert not led.is_fresh(session_id="s1", workspace="projB", now=1000.0, ttl_sec=300.0)
    assert not led.is_fresh(session_id="s2", workspace="projA", now=1000.0, ttl_sec=300.0)
