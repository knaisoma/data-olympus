# tests/test_enforce_gate_policy.py
"""Gate policy tests for the explicit-vs-prompt_hook consult distinction (WP0c).

The gate must mean "the agent explicitly consulted", not "an HTTP call happened
this session". These tests pin that policy: an explicit consult clears the gate,
a prompt_hook consult does not, a non-governed explicit consult still clears, TTL
expiry re-blocks, and a prompt_hook consult never downgrades a fresh explicit one.
"""
from __future__ import annotations

from data_olympus.enforce_policy import (
    EXPLICIT_TRIGGER,
    PROMPT_HOOK_TRIGGER,
    ConsultationLedger,
    IntentClassifier,
)
from data_olympus.tools_enforce import kb_consult_fn, kb_gate_check_fn


class _FakeIndex:
    def search(self, query, limit=20, tier=None, category=None, status=None,  # noqa: ARG002
               in_force=False, doc_type=None, **kwargs):  # noqa: ARG002
        return []

    def health(self):
        return {"source_commit": "deadbeef"}


def _gate(led: ConsultationLedger, *, now: float, path="/p/pyproject.toml"):
    return kb_gate_check_fn(
        classifier=IntentClassifier(), ledger=led,
        workspace="proj", session_id="s1", tool_name="Edit",
        action_path=path, action_diff="", now=now, ttl_sec=300.0,
    )


def test_explicit_consult_clears_the_gate() -> None:
    led = ConsultationLedger()
    kb_consult_fn(
        idx=_FakeIndex(), classifier=IntentClassifier(), ledger=led,
        workspace="proj", intent="choose a dependency", source_session="s1",
        agent_identity="claude", ttl_sec=300.0, now=1000.0,
        trigger=EXPLICIT_TRIGGER,
    )
    assert _gate(led, now=1100.0).verdict == "allow"


def test_prompt_hook_consult_does_not_clear_the_gate() -> None:
    led = ConsultationLedger()
    # A prompt-hook auto-consult is recorded but must NOT satisfy the gate.
    kb_consult_fn(
        idx=_FakeIndex(), classifier=IntentClassifier(), ledger=led,
        workspace="proj", intent="choose a dependency", source_session="s1",
        agent_identity="claude", ttl_sec=300.0, now=1000.0,
        trigger=PROMPT_HOOK_TRIGGER,
    )
    resp = _gate(led, now=1100.0)
    assert resp.verdict == "consult_required"


def test_non_governed_explicit_consult_still_clears_the_gate() -> None:
    """No deadlock: an intent with zero governing rules still records a fresh
    explicit consult, so the agent can always clear the gate by consulting."""
    led = ConsultationLedger()
    resp = kb_consult_fn(
        idx=_FakeIndex(), classifier=IntentClassifier(), ledger=led,
        workspace="proj", intent="say hello",  # not governed -> no rules
        source_session="s1", agent_identity="claude", ttl_sec=300.0, now=1000.0,
        trigger=EXPLICIT_TRIGGER,
    )
    assert resp.is_governed_decision is False
    assert _gate(led, now=1100.0).verdict == "allow"


def test_explicit_consult_expires_after_ttl() -> None:
    led = ConsultationLedger()
    kb_consult_fn(
        idx=_FakeIndex(), classifier=IntentClassifier(), ledger=led,
        workspace="proj", intent="choose a dependency", source_session="s1",
        agent_identity="claude", ttl_sec=300.0, now=1000.0,
        trigger=EXPLICIT_TRIGGER,
    )
    # 301s later the explicit consult is stale; the gate re-blocks.
    assert _gate(led, now=1301.0).verdict == "consult_required"


def test_prompt_hook_does_not_downgrade_a_fresh_explicit_consult() -> None:
    """An explicit consult clears the gate; a later prompt-hook consult on the
    same (session, workspace) must not un-clear it."""
    led = ConsultationLedger()
    kb_consult_fn(
        idx=_FakeIndex(), classifier=IntentClassifier(), ledger=led,
        workspace="proj", intent="choose a dependency", source_session="s1",
        agent_identity="claude", ttl_sec=300.0, now=1000.0,
        trigger=EXPLICIT_TRIGGER,
    )
    # A per-turn prompt-hook consult fires shortly after.
    kb_consult_fn(
        idx=_FakeIndex(), classifier=IntentClassifier(), ledger=led,
        workspace="proj", intent="do the thing", source_session="s1",
        agent_identity="claude", ttl_sec=300.0, now=1010.0,
        trigger=PROMPT_HOOK_TRIGGER,
    )
    # The explicit consult (t=1000) is still fresh at t=1100; gate stays clear.
    assert _gate(led, now=1100.0).verdict == "allow"


def test_missing_trigger_is_treated_as_explicit() -> None:
    """Backward compatibility: an old client that omits trigger is a real agent
    call and must clear the gate (default explicit)."""
    led = ConsultationLedger()
    kb_consult_fn(
        idx=_FakeIndex(), classifier=IntentClassifier(), ledger=led,
        workspace="proj", intent="choose a dependency", source_session="s1",
        agent_identity="claude", ttl_sec=300.0, now=1000.0,
        # no trigger kwarg
    )
    assert _gate(led, now=1100.0).verdict == "allow"


def test_unknown_trigger_coerced_to_explicit() -> None:
    led = ConsultationLedger()
    kb_consult_fn(
        idx=_FakeIndex(), classifier=IntentClassifier(), ledger=led,
        workspace="proj", intent="choose a dependency", source_session="s1",
        agent_identity="claude", ttl_sec=300.0, now=1000.0,
        trigger="garbage",
    )
    assert _gate(led, now=1100.0).verdict == "allow"


def test_deny_reason_contains_session_and_workspace_and_instruction() -> None:
    """The blocked verdict's reason must be actionable: it names the workspace,
    the session id, and a copy-pasteable kb_consult call."""
    led = ConsultationLedger()
    resp = kb_gate_check_fn(
        classifier=IntentClassifier(), ledger=led,
        workspace="my-proj", session_id="sess-xyz", tool_name="Edit",
        action_path="/p/pyproject.toml", action_diff="", now=1000.0, ttl_sec=300.0,
    )
    assert resp.verdict == "consult_required"
    assert "my-proj" in resp.reason
    assert "sess-xyz" in resp.reason
    assert "kb_consult(" in resp.reason
    # The echo fields must carry the exact gate key back to MCP callers.
    assert resp.session_id == "sess-xyz"
    assert resp.workspace == "my-proj"


def test_allow_response_also_echoes_session_and_workspace() -> None:
    led = ConsultationLedger()
    led.record(session_id="s1", workspace="proj", rule_ids=[], now=1000.0)
    resp = _gate(led, now=1100.0)
    assert resp.verdict == "allow"
    assert resp.session_id == "s1"
    assert resp.workspace == "proj"
