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


# ---------------------------------------------------------------------------
# in-force hard filter on retrieval (issue #109): kb_consult must never hand
# back an unreviewed / proposed / retired / expired / memory-inbox doc as a
# governing rule. Exercised against a REAL Index (not the search-stub
# _FakeIndex above) so the actual SQL filter is under test.
# ---------------------------------------------------------------------------


def _write(kb, rel: str, *, id_: str, status: str, title: str, body: str) -> None:
    from pathlib import Path
    p = Path(kb) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\nid: {id_}\ntype: standard\nstatus: {status}\ntier: T1\n"
        f"category: foundation\ntitle: {title}\n---\n# {title}\n\n{body}\n",
        encoding="utf-8",
    )


def test_consult_never_surfaces_non_in_force_docs(tmp_path) -> None:
    from data_olympus.index import Index

    kb = tmp_path / "kb"
    kb.mkdir()
    _write(kb, "universal/foundation/active.md", id_="DOC-ACTIVE", status="active",
           title="Caching Rule", body="The caching policy currently in force.")
    _write(kb, "universal/foundation/proposed.md", id_="DOC-PROPOSED",
           status="proposed", title="Caching Draft",
           body="An unreviewed proposed caching rule.")
    _write(kb, "universal/foundation/superseded.md", id_="DOC-SUPERSEDED",
           status="superseded", title="Old Caching Rule",
           body="The retired caching rule.")
    _write(kb, "memory/inbox/2026-01-01-caching.md", id_="DOC-INBOX",
           status="active", title="Caching Memory",
           body="An agent-written memory about caching, claiming active.")
    idx = Index(tmp_path / "idx.db")
    idx.build(kb, source_commit="test")

    led = ConsultationLedger()
    resp = kb_consult_fn(
        idx=idx, classifier=IntentClassifier(), ledger=led,
        workspace="proj", intent="add a new caching library",
        source_session="s1", agent_identity="claude",
        ttl_sec=300.0, now=1000.0,
    )
    assert resp.is_governed_decision is True
    ids = {r.id for r in resp.rules}
    assert ids == {"DOC-ACTIVE"}
    assert "DOC-PROPOSED" not in ids
    assert "DOC-SUPERSEDED" not in ids
    assert "DOC-INBOX" not in ids
