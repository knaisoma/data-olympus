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


def test_consult_audit_event_records_coverage_and_keeps_reason(tmp_path) -> None:
    """Issue #309: the consult row records what the consult covered, so the
    report can apply the gate's coverage rule; ``reason`` is unchanged."""
    al = AuditLog(log_path=str(tmp_path / "events.log"))
    kb_consult_fn(
        idx=_FakeIndex(), classifier=IntentClassifier(), ledger=ConsultationLedger(),
        workspace="proj", intent="edit pyproject.toml to add a dependency",
        source_session="s1", agent_identity="claude",
        ttl_sec=300.0, now=1000.0, audit_log=al,
    )
    (ev,) = [e for e in al.iter_filtered() if e["event_type"] == "consult"]
    assert ev["reason"] == "keyword:dependency"
    assert ev["coverage"] == ["keyword:dependency", "path:pyproject.toml"]


def test_consult_audit_event_records_empty_coverage(tmp_path) -> None:
    """A consult that covers nothing records an empty list, which the report
    tells apart from a row written before coverage was recorded."""
    al = AuditLog(log_path=str(tmp_path / "events.log"))
    kb_consult_fn(
        idx=_FakeIndex(), classifier=IntentClassifier(), ledger=ConsultationLedger(),
        workspace="proj", intent="say hello", source_session="s1",
        agent_identity="claude", ttl_sec=300.0, now=1000.0, audit_log=al,
    )
    (ev,) = [e for e in al.iter_filtered() if e["event_type"] == "consult"]
    assert ev["coverage"] == []


# ---------------------------------------------------------------------------
# Intent length cap: the prompt hook posts whole prompts (review prompts carry
# patches up to 600 KiB) as the intent. kb_consult_fn truncates the intent to
# MAX_CONSULT_INTENT_CHARS before classify, coverage and search, so one consult
# cannot drive an unbounded classifier pass or FTS MATCH.
# ---------------------------------------------------------------------------


def test_consult_truncates_a_600_kib_intent() -> None:
    from data_olympus.tools_enforce import MAX_CONSULT_INTENT_CHARS

    assert MAX_CONSULT_INTENT_CHARS == 4000
    seen: dict[str, list[str]] = {"classify": [], "coverage": [], "search": []}

    class _SpyClassifier(IntentClassifier):
        def classify(self, *, intent="", **kwargs):  # type: ignore[override]
            seen["classify"].append(intent)
            return super().classify(intent=intent, **kwargs)

        def coverage(self, intent):  # type: ignore[override]
            seen["coverage"].append(intent)
            return super().coverage(intent)

    class _SpyIndex(_FakeIndex):
        def search(self, query, *args, **kwargs):  # type: ignore[override]  # noqa: ARG002
            seen["search"].append(query)
            return []

    head = "add a new caching library "
    intent = head + ("x" * (600 * 1024))
    resp = kb_consult_fn(
        idx=_SpyIndex(), classifier=_SpyClassifier(), ledger=ConsultationLedger(),
        workspace="proj", intent=intent,
        source_session="s1", agent_identity="codex",
        ttl_sec=300.0, now=1000.0, audit_log=None,
    )
    assert resp.is_governed_decision is True
    for name in ("classify", "coverage", "search"):
        assert seen[name], f"{name} was not called"
        for value in seen[name]:
            assert len(value) == MAX_CONSULT_INTENT_CHARS, name
            assert value == intent[:MAX_CONSULT_INTENT_CHARS], name
