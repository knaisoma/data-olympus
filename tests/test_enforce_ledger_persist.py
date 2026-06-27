"""ConsultationLedger optional JSON-file persistence."""
from __future__ import annotations

import json

from data_olympus.enforce_policy import ConsultationLedger


def test_in_memory_when_no_path() -> None:
    led = ConsultationLedger()  # no path -> pure in-memory (unchanged behavior)
    led.record(session_id="s", workspace="w", rule_ids=[], now=100.0)
    assert led.is_fresh(session_id="s", workspace="w", now=100.0, ttl_sec=300)


def test_record_persists_and_reloads(tmp_path) -> None:
    path = str(tmp_path / "ledger.json")
    led = ConsultationLedger(path=path)
    led.record(session_id="s1", workspace="proj", rule_ids=["STD-U-002"], now=1000.0)
    # a fresh ledger pointed at the same file sees the recorded consult
    led2 = ConsultationLedger(path=path)
    assert led2.is_fresh(session_id="s1", workspace="proj", now=1100.0, ttl_sec=300)


def test_corrupt_file_degrades_to_empty(tmp_path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text("{ not json")
    led = ConsultationLedger(path=str(path))  # must not crash
    assert led.is_fresh(session_id="s", workspace="w", now=1.0, ttl_sec=300) is False


def test_writes_are_atomic_and_leave_no_temp(tmp_path) -> None:
    path = tmp_path / "ledger.json"
    led = ConsultationLedger(path=str(path))
    for i in range(5):
        led.record(session_id=f"s{i}", workspace="proj", rule_ids=[f"R{i}"], now=float(i))
    # File is valid JSON and reloads correctly via a fresh ledger.
    json.loads(path.read_text())
    led2 = ConsultationLedger(path=str(path))
    for i in range(5):
        assert led2.is_fresh(session_id=f"s{i}", workspace="proj", now=float(i), ttl_sec=300)
    # No leftover temp file from the atomic write dance.
    assert not list(tmp_path.glob(".ledger-*.tmp"))
