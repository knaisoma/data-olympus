# tests/test_enforce_ledger_trigger.py
"""ConsultationLedger trigger semantics (WP0c): explicit_at tracking, the
require_explicit freshness switch, no-downgrade, and persistence round-trip."""
from __future__ import annotations

import json

from data_olympus.enforce_policy import (
    EXPLICIT_TRIGGER,
    PROMPT_HOOK_TRIGGER,
    ConsultationLedger,
)


def test_explicit_record_sets_explicit_at() -> None:
    led = ConsultationLedger()
    led.record(session_id="s", workspace="w", rule_ids=[], now=100.0,
               trigger=EXPLICIT_TRIGGER)
    e = led.get(session_id="s", workspace="w")
    assert e is not None
    assert e.explicit_at == 100.0
    assert led.is_fresh(session_id="s", workspace="w", now=200.0, ttl_sec=300.0)


def test_prompt_hook_record_leaves_explicit_at_none() -> None:
    led = ConsultationLedger()
    led.record(session_id="s", workspace="w", rule_ids=[], now=100.0,
               trigger=PROMPT_HOOK_TRIGGER)
    e = led.get(session_id="s", workspace="w")
    assert e is not None
    assert e.explicit_at is None
    # require_explicit (gate default) -> not fresh; require_explicit=False -> fresh.
    assert not led.is_fresh(session_id="s", workspace="w", now=200.0, ttl_sec=300.0)
    assert led.is_fresh(session_id="s", workspace="w", now=200.0, ttl_sec=300.0,
                        require_explicit=False)


def test_prompt_hook_does_not_downgrade_explicit_at() -> None:
    led = ConsultationLedger()
    led.record(session_id="s", workspace="w", rule_ids=[], now=100.0,
               trigger=EXPLICIT_TRIGGER)
    led.record(session_id="s", workspace="w", rule_ids=[], now=110.0,
               trigger=PROMPT_HOOK_TRIGGER)
    e = led.get(session_id="s", workspace="w")
    assert e is not None
    assert e.explicit_at == 100.0  # preserved, not overwritten
    assert e.consulted_at == 110.0  # row liveness refreshed
    assert led.is_fresh(session_id="s", workspace="w", now=200.0, ttl_sec=300.0)


def test_persistence_round_trips_explicit_at(tmp_path) -> None:
    p = tmp_path / "ledger.json"
    led = ConsultationLedger(str(p))
    led.record(session_id="s", workspace="w", rule_ids=["R1"], now=100.0,
               trigger=EXPLICIT_TRIGGER)
    led2 = ConsultationLedger(str(p))
    assert led2.is_fresh(session_id="s", workspace="w", now=200.0, ttl_sec=300.0)


def test_legacy_persisted_row_without_explicit_at_counts_as_explicit(tmp_path) -> None:
    """A ledger written before the trigger split (no explicit_at key) must load
    its rows as explicit so a server upgrade does not spuriously re-block."""
    p = tmp_path / "ledger.json"
    p.write_text(json.dumps([
        {"session_id": "s", "workspace": "w", "consulted_at": 100.0,
         "rule_ids": []},
    ]))
    led = ConsultationLedger(str(p))
    assert led.is_fresh(session_id="s", workspace="w", now=200.0, ttl_sec=300.0)
