"""Index.build() computes and caches MaintenanceState (issue #113)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from data_olympus.index import Index

if TYPE_CHECKING:
    from pathlib import Path


def test_maintenance_state_none_before_first_build(tmp_path: Path) -> None:
    idx = Index(tmp_path / "idx.db")
    assert idx.maintenance_state is None


def test_status_kb_has_all_statuses_present(status_kb: Path, tmp_path: Path) -> None:
    """status_kb fixture: every doc carries an explicit status."""
    idx = Index(tmp_path / "idx.db")
    idx.build(status_kb, source_commit="x", today="2026-07-08")
    state = idx.maintenance_state
    assert state is not None
    assert state.status_present_in_all_kb_entries is True
    assert state.missing_status_count == 0


def test_tmp_kb_has_missing_status_docs(tmp_kb: Path, tmp_path: Path) -> None:
    """tmp_kb fixture has several docs with no `type`/`status` frontmatter at
    all (e.g. WF-001, the memory/tooling files)."""
    idx = Index(tmp_path / "idx.db")
    idx.build(tmp_kb, source_commit="x", today="2026-07-08")
    state = idx.maintenance_state
    assert state is not None
    assert state.status_present_in_all_kb_entries is False
    assert state.missing_status_count > 0
    assert len(state.missing_status_paths) == state.missing_status_count


def test_recompute_after_remediation_flips_flag(tmp_kb: Path, tmp_path: Path) -> None:
    """Remediation flow (scenario 7): fix the status-less file, rebuild, flag
    flips true."""
    idx = Index(tmp_path / "idx.db")
    idx.build(tmp_kb, source_commit="x", today="2026-07-08")
    assert idx.maintenance_state is not None
    assert idx.maintenance_state.status_present_in_all_kb_entries is False

    # Remediate: add status/type/tier to every doc missing one.
    for rel in idx.maintenance_state.missing_status_paths:
        p = tmp_kb / rel
        text = p.read_text(encoding="utf-8")
        if text.startswith("---\n"):
            # Insert a status line into the existing frontmatter block.
            head, _, rest = text.partition("---\n")
            body_start = rest.find("---\n")
            fm_body = rest[:body_start]
            after = rest[body_start:]
            p.write_text(f"---\n{fm_body}status: active\ntype: reference\ntier: meta\n{after}")
        else:
            p.write_text(f"---\nstatus: active\ntype: reference\ntier: meta\n---\n{text}")

    idx.build(tmp_kb, source_commit="y", today="2026-07-08")
    assert idx.maintenance_state.status_present_in_all_kb_entries is True
    assert idx.maintenance_state.missing_status_count == 0


def test_expiry_buckets_from_real_build(tmp_path: Path) -> None:
    kb = tmp_path / "kb"
    d = kb / "universal" / "foundation"
    d.mkdir(parents=True)
    (d / "expired.md").write_text(
        "---\nid: EXP-1\ntype: standard\nstatus: active\ntier: T1\n"
        "validity:\n  valid_until: 2026-06-28\n---\nold rule\n"
    )
    (d / "expiring-soon.md").write_text(
        "---\nid: SOON-1\ntype: standard\nstatus: active\ntier: T1\n"
        "validity:\n  valid_until: 2026-07-18\n---\nsoon-expiring rule\n"
    )
    idx = Index(tmp_path / "idx.db")
    idx.build(kb, source_commit="x", today="2026-07-08")
    state = idx.maintenance_state
    assert state is not None
    assert [i.id for i in state.recently_expired] == ["EXP-1"]
    assert [i.id for i in state.expiring_soon] == ["SOON-1"]


def test_ledger_doc_itself_excluded_from_build_time_audit(tmp_path: Path) -> None:
    kb = tmp_path / "kb"
    tooling = kb / "tooling"
    tooling.mkdir(parents=True)
    from data_olympus.maintenance import MaintenanceState, render_ledger_markdown
    clean = MaintenanceState(status_present_in_all_kb_entries=True)
    (tooling / "maintenance-ledger.md").write_text(
        render_ledger_markdown(clean, computed_at_iso="2026-07-08T00:00:00+00:00")
    )
    (tooling / "other.md").write_text(
        "---\nid: OTHER\ntype: reference\nstatus: active\ntier: meta\n---\nbody\n"
    )
    idx = Index(
        tmp_path / "idx.db", maintenance_ledger_path="tooling/maintenance-ledger.md",
    )
    idx.build(kb, source_commit="x", today="2026-07-08")
    state = idx.maintenance_state
    assert state is not None
    assert "tooling/maintenance-ledger.md" not in state.missing_status_paths
    assert state.status_present_in_all_kb_entries is True
