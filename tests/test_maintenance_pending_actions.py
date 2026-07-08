"""pending_actions CTA surface on kb_consult / kb_health, never kb_search
(issue #113, test scenario 4)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from data_olympus.enforce_policy import ConsultationLedger, IntentClassifier
from data_olympus.index import Index
from data_olympus.tools_enforce import kb_consult_fn
from data_olympus.tools_read import kb_health_fn, kb_search_fn

if TYPE_CHECKING:
    from pathlib import Path


def test_kb_health_omits_pending_actions_when_clean(status_kb: Path, tmp_path: Path) -> None:
    idx = Index(tmp_path / "idx.db")
    idx.build(status_kb, source_commit="x", today="2026-07-08")
    resp = kb_health_fn(idx=idx, last_git_pull_at=None, staleness_degraded_sec=600)
    assert resp.pending_actions is None


def test_kb_health_includes_pending_actions_when_dirty(tmp_kb: Path, tmp_path: Path) -> None:
    idx = Index(tmp_path / "idx.db")
    idx.build(tmp_kb, source_commit="x", today="2026-07-08")
    resp = kb_health_fn(idx=idx, last_git_pull_at=None, staleness_degraded_sec=600)
    assert resp.pending_actions is not None
    assert any(a["kind"] == "missing_status" for a in resp.pending_actions)


def test_kb_health_compact_dump_omits_pending_actions_when_clean(
    status_kb: Path, tmp_path: Path,
) -> None:
    idx = Index(tmp_path / "idx.db")
    idx.build(status_kb, source_commit="x", today="2026-07-08")
    resp = kb_health_fn(idx=idx, last_git_pull_at=None, staleness_degraded_sec=600)
    compact = resp.compact_dump()
    assert "pending_actions" not in compact


def test_kb_health_compact_dump_includes_pending_actions_when_dirty(
    tmp_kb: Path, tmp_path: Path,
) -> None:
    idx = Index(tmp_path / "idx.db")
    idx.build(tmp_kb, source_commit="x", today="2026-07-08")
    resp = kb_health_fn(idx=idx, last_git_pull_at=None, staleness_degraded_sec=600)
    compact = resp.compact_dump()
    assert "pending_actions" in compact


def test_kb_consult_includes_pending_actions_when_dirty(tmp_kb: Path, tmp_path: Path) -> None:
    idx = Index(tmp_path / "idx.db")
    idx.build(tmp_kb, source_commit="x", today="2026-07-08")
    led = ConsultationLedger()
    resp = kb_consult_fn(
        idx=idx, classifier=IntentClassifier(), ledger=led,
        workspace="proj", intent="say hello", source_session="s1",
        agent_identity="claude", ttl_sec=300.0, now=1000.0, audit_log=None,
    )
    assert resp.pending_actions is not None


def test_kb_consult_omits_pending_actions_when_clean(status_kb: Path, tmp_path: Path) -> None:
    idx = Index(tmp_path / "idx.db")
    idx.build(status_kb, source_commit="x", today="2026-07-08")
    led = ConsultationLedger()
    resp = kb_consult_fn(
        idx=idx, classifier=IntentClassifier(), ledger=led,
        workspace="proj", intent="say hello", source_session="s1",
        agent_identity="claude", ttl_sec=300.0, now=1000.0, audit_log=None,
    )
    assert resp.pending_actions is None


def test_kb_consult_model_dump_exclude_none_omits_field_when_clean(
    status_kb: Path, tmp_path: Path,
) -> None:
    """Mirrors how the MCP/REST wrappers serialize: model_dump(exclude_none=True)."""
    idx = Index(tmp_path / "idx.db")
    idx.build(status_kb, source_commit="x", today="2026-07-08")
    led = ConsultationLedger()
    resp = kb_consult_fn(
        idx=idx, classifier=IntentClassifier(), ledger=led,
        workspace="proj", intent="say hello", source_session="s1",
        agent_identity="claude", ttl_sec=300.0, now=1000.0, audit_log=None,
    )
    dumped = resp.model_dump(exclude_none=True)
    assert "pending_actions" not in dumped


def test_kb_search_never_has_pending_actions(tmp_kb: Path, tmp_path: Path) -> None:
    """kb_search must NEVER carry pending_actions, dirty or not (per-hit noise
    trains agents to ignore it -- issue #113)."""
    idx = Index(tmp_path / "idx.db")
    idx.build(tmp_kb, source_commit="x", today="2026-07-08")
    assert idx.maintenance_state is not None
    assert idx.maintenance_state.is_dirty is True  # sanity: this KB IS dirty
    resp = kb_search_fn(idx=idx, query="worktree")
    assert not hasattr(resp, "pending_actions")
    assert "pending_actions" not in resp.model_dump()
    assert "pending_actions" not in resp.compact_dump()


def test_index_without_maintenance_state_yet_omits_pending_actions(tmp_path: Path) -> None:
    """A fresh Index that has never built() yet (maintenance_state is None)
    must not crash kb_health/kb_consult; the field is simply omitted."""
    idx = Index(tmp_path / "idx.db")
    resp = kb_health_fn(idx=idx, last_git_pull_at=None, staleness_degraded_sec=600)
    assert resp.pending_actions is None


class _FakeIndexNoMaintenance:
    """A minimal Index stand-in with no maintenance_state attribute at all,
    mirroring test_tools_enforce_consult.py's _FakeIndex. kb_consult_fn must
    not crash on it (defensive getattr)."""

    def search(self, query, limit=20, tier=None, category=None, status=None,  # noqa: ARG002
               in_force=False, doc_type=None, **kwargs):  # noqa: ARG002
        return []

    def health(self):
        return {"source_commit": "deadbeef"}


def test_kb_consult_tolerates_index_with_no_maintenance_state_attribute() -> None:
    led = ConsultationLedger()
    resp = kb_consult_fn(
        idx=_FakeIndexNoMaintenance(), classifier=IntentClassifier(), ledger=led,
        workspace="proj", intent="say hello", source_session="s1",
        agent_identity="claude", ttl_sec=300.0, now=1000.0, audit_log=None,
    )
    assert resp.pending_actions is None
