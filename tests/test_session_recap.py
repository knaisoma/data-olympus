"""Tests for kb_session_recap (issue #112 feedback loop) and its surfacing in
kb_consult's pending_actions envelope."""
from __future__ import annotations

import time

from data_olympus.audit_log import AuditLog
from data_olympus.enforce_policy import ConsultationLedger, IntentClassifier
from data_olympus.pending import PendingQueue
from data_olympus.tools_enforce import kb_consult_fn
from data_olympus.tools_recap import kb_session_recap_fn


class _FakeIndex:
    """Minimal Index double for kb_consult_fn: no governed decision, no rules."""

    maintenance_state = None

    def search(self, **kwargs):  # noqa: ANN003, ARG002
        class _R:
            hits: list = []
        return _R()


def _audit(tmp_path):
    return AuditLog(log_path=str(tmp_path / "audit.log"), hmac_key="")


def test_recap_counts_committed_demoted_rejected_for_one_session(tmp_path) -> None:
    audit = _audit(tmp_path)
    audit.append({"ts": 1.0, "event_type": "propose_edit", "status": "committed",
                  "source_session": "s1"})
    audit.append({"ts": 2.0, "event_type": "propose_edit", "status": "pending_confirmation",
                  "source_session": "s1", "demotion_reason": "governed_target"})
    audit.append({"ts": 3.0, "event_type": "propose_edit", "status": "pending_confirmation",
                  "source_session": "s1", "demotion_reason": "status_promotion"})
    audit.append({"ts": 4.0, "event_type": "propose_edit", "status": "rejected_secret_detected",
                  "source_session": "s1"})
    # A different session's events must not leak into s1's tally.
    audit.append({"ts": 5.0, "event_type": "propose_edit", "status": "committed",
                  "source_session": "s2"})

    recap = kb_session_recap_fn(audit_log=audit, source_session="s1")
    assert recap.source_session == "s1"
    assert recap.committed == 1
    assert recap.demoted_to_pending == 2
    assert recap.rejected == 1


def test_recap_empty_session_is_all_zero(tmp_path) -> None:
    audit = _audit(tmp_path)
    audit.append({"ts": 1.0, "event_type": "propose_edit", "status": "committed",
                  "source_session": "other"})
    recap = kb_session_recap_fn(audit_log=audit, source_session="nonexistent")
    assert recap.committed == 0
    assert recap.demoted_to_pending == 0
    assert recap.rejected == 0


def test_consult_pending_actions_includes_demoted_writes_item(tmp_path) -> None:
    audit = _audit(tmp_path)
    audit.append({"ts": 1.0, "event_type": "propose_edit", "status": "pending_confirmation",
                  "source_session": "sess-demoted", "demotion_reason": "governed_target"})
    audit.append({"ts": 2.0, "event_type": "propose_edit", "status": "pending_confirmation",
                  "source_session": "sess-demoted", "demotion_reason": "status_promotion"})

    idx = _FakeIndex()
    classifier = IntentClassifier()
    ledger = ConsultationLedger(path=str(tmp_path / "ledger.json"))
    resp = kb_consult_fn(
        idx=idx, classifier=classifier, ledger=ledger,
        workspace="ws", intent="just checking in", source_session="sess-demoted",
        agent_identity="claude", ttl_sec=300, now=time.time(), audit_log=audit,
    )
    assert resp.pending_actions is not None
    demoted_items = [i for i in resp.pending_actions if i["kind"] == "demoted_writes"]
    assert len(demoted_items) == 1
    assert demoted_items[0]["count"] == 2


def test_consult_pending_actions_omits_demoted_writes_item_for_clean_session(
    tmp_path,
) -> None:
    audit = _audit(tmp_path)
    audit.append({"ts": 1.0, "event_type": "propose_edit", "status": "committed",
                  "source_session": "sess-clean"})

    idx = _FakeIndex()
    classifier = IntentClassifier()
    ledger = ConsultationLedger(path=str(tmp_path / "ledger.json"))
    resp = kb_consult_fn(
        idx=idx, classifier=classifier, ledger=ledger,
        workspace="ws", intent="just checking in", source_session="sess-clean",
        agent_identity="claude", ttl_sec=300, now=time.time(), audit_log=audit,
    )
    assert resp.pending_actions is None


def _enqueue_demotion(pending: PendingQueue, *, source_session: str, rel: str) -> str:
    """Enqueue a governed-lane demotion the way tools_write does: an edit whose
    meta carries the demoting session. Returns the pending_id."""
    return pending.enqueue(
        proposal_type="edit",
        target_path=rel,
        postimage="post\n",
        base_commit="deadbeef",
        base_blob_sha=None,
        target_file_hash=None,
        meta={
            "source_session": source_session,
            "confidence": 0.95,
            "agent_identity": "claude",
            "demotion_reason": "governed_target",
        },
    )


def test_consult_omits_demoted_writes_after_pending_resolved(tmp_path) -> None:
    """KNA-72 / gh #137: a high-confidence write demoted to pending (governed
    lane) then REJECTED must not keep surfacing the demoted_writes CTA. The
    lifetime recap still counts the demotion, but with no live pending entry
    for the session the actionable CTA is omitted."""
    audit = _audit(tmp_path)
    # The demotion is on the lifetime audit record forever.
    audit.append({"ts": 1.0, "event_type": "propose_edit",
                  "status": "pending_confirmation", "source_session": "sess-demoted",
                  "demotion_reason": "governed_target"})
    pending = PendingQueue(pending_root=str(tmp_path / "pending"))
    pid = _enqueue_demotion(pending, source_session="sess-demoted",
                            rel="universal/foundation/x.md")
    # Operator rejects the demoted write: the live pending entry is gone.
    pending.reject(pid)
    assert pending.size() == 0

    idx = _FakeIndex()
    classifier = IntentClassifier()
    ledger = ConsultationLedger(path=str(tmp_path / "ledger.json"))
    # Sanity: the lifetime recap still reports the demotion (intended surface).
    assert kb_session_recap_fn(
        audit_log=audit, source_session="sess-demoted",
    ).demoted_to_pending == 1

    resp = kb_consult_fn(
        idx=idx, classifier=classifier, ledger=ledger,
        workspace="ws", intent="just checking in", source_session="sess-demoted",
        agent_identity="claude", ttl_sec=300, now=time.time(), audit_log=audit,
        pending_queue=pending,
    )
    demoted_items = [
        i for i in (resp.pending_actions or []) if i["kind"] == "demoted_writes"
    ]
    assert demoted_items == []


def test_consult_keeps_demoted_writes_with_live_pending(tmp_path) -> None:
    """The CTA stays (and its count reflects the LIVE pending, not the lifetime
    audit count) while a real unresolved pending entry remains for the session."""
    audit = _audit(tmp_path)
    # Two lifetime demotions, but only one still live below.
    audit.append({"ts": 1.0, "event_type": "propose_edit",
                  "status": "pending_confirmation", "source_session": "sess-live",
                  "demotion_reason": "governed_target"})
    audit.append({"ts": 2.0, "event_type": "propose_edit",
                  "status": "pending_confirmation", "source_session": "sess-live",
                  "demotion_reason": "status_promotion"})
    pending = PendingQueue(pending_root=str(tmp_path / "pending"))
    pid = _enqueue_demotion(pending, source_session="sess-live",
                            rel="universal/foundation/a.md")
    still = _enqueue_demotion(pending, source_session="sess-live",
                              rel="universal/foundation/b.md")
    # A different session's live pending must not inflate this session's count.
    _enqueue_demotion(pending, source_session="other-sess",
                      rel="universal/foundation/c.md")
    pending.reject(pid)  # one resolved, one (`still`) remains
    assert still  # keep the id referenced

    idx = _FakeIndex()
    classifier = IntentClassifier()
    ledger = ConsultationLedger(path=str(tmp_path / "ledger.json"))
    resp = kb_consult_fn(
        idx=idx, classifier=classifier, ledger=ledger,
        workspace="ws", intent="just checking in", source_session="sess-live",
        agent_identity="claude", ttl_sec=300, now=time.time(), audit_log=audit,
        pending_queue=pending,
    )
    demoted_items = [
        i for i in (resp.pending_actions or []) if i["kind"] == "demoted_writes"
    ]
    assert len(demoted_items) == 1
    # Reconciled against LIVE pending (1), not the lifetime audit count (2).
    assert demoted_items[0]["count"] == 1
