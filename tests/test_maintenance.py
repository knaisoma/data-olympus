"""Tests for the maintenance-ledger corpus-state audit (issue #113)."""
from __future__ import annotations

from data_olympus.maintenance import (
    CAP,
    DocAuditRow,
    ExpiryItem,
    MaintenanceState,
    compute_maintenance_state,
    parse_ledger_state,
    pending_actions_for,
    render_ledger_markdown,
)

LEDGER_PATH = "tooling/maintenance-ledger.md"


def _row(path: str, *, status: str = "active", valid_until: str = "",
         is_reserved: bool = False, doc_id: str | None = None) -> DocAuditRow:
    return DocAuditRow(
        path=path, id=doc_id or path, status=status,
        valid_until=valid_until, is_reserved=is_reserved,
    )


# --- scenario 1: all statuses present -----------------------------------


def test_all_statuses_present_flags_true_no_missing_list() -> None:
    rows = [_row("a.md"), _row("b.md"), _row("c.md")]
    state = compute_maintenance_state(rows, today="2026-07-08", ledger_path=LEDGER_PATH)
    assert state.status_present_in_all_kb_entries is True
    assert state.missing_status_paths == ()
    assert state.missing_status_count == 0
    assert state.is_dirty is False


# --- scenario 2: >50 status-less files -----------------------------------


def test_more_than_cap_missing_status_is_capped_with_total_count() -> None:
    n = 73
    rows = [_row(f"doc-{i:03d}.md", status="") for i in range(n)]
    state = compute_maintenance_state(rows, today="2026-07-08", ledger_path=LEDGER_PATH)
    assert state.status_present_in_all_kb_entries is False
    assert len(state.missing_status_paths) == CAP
    assert state.missing_status_count == n
    assert state.is_dirty is True


def test_reserved_filenames_are_exempt_from_missing_status() -> None:
    rows = [
        _row("index.md", status="", is_reserved=True),
        _row("log.md", status="", is_reserved=True),
        _row("real-doc.md", status="active"),
    ]
    state = compute_maintenance_state(rows, today="2026-07-08", ledger_path=LEDGER_PATH)
    assert state.status_present_in_all_kb_entries is True
    assert state.missing_status_count == 0


# --- scenario 3: expiry windows -------------------------------------------


def test_expiry_windows_bucket_correctly() -> None:
    today = "2026-07-08"
    rows = [
        # expired 10 days ago -> recently_expired
        _row("expired-10.md", valid_until="2026-06-28", doc_id="EXP-10"),
        # expiring in 10 days -> expiring_soon
        _row("expiring-10.md", valid_until="2026-07-18", doc_id="EXP-SOON-10"),
        # expired 60 days ago -> outside the 30-day window, excluded entirely
        _row("expired-60.md", valid_until="2026-05-09", doc_id="EXP-60"),
        # no validity at all -> excluded from both buckets
        _row("no-validity.md"),
    ]
    state = compute_maintenance_state(
        rows, today=today, ledger_path=LEDGER_PATH,
        recently_expired_days=30, expiring_soon_days=30,
    )
    assert [i.id for i in state.recently_expired] == ["EXP-10"]
    assert state.recently_expired_count == 1
    assert [i.id for i in state.expiring_soon] == ["EXP-SOON-10"]
    assert state.expiring_soon_count == 1
    assert state.is_dirty is True


def test_expiry_window_boundary_is_inclusive() -> None:
    today = "2026-07-08"
    rows = [
        # exactly at the 30-day recently-expired boundary
        _row("boundary-expired.md", valid_until="2026-06-08", doc_id="B-EXP"),
        # exactly at the 30-day expiring-soon boundary
        _row("boundary-soon.md", valid_until="2026-08-07", doc_id="B-SOON"),
    ]
    state = compute_maintenance_state(
        rows, today=today, ledger_path=LEDGER_PATH,
        recently_expired_days=30, expiring_soon_days=30,
    )
    assert [i.id for i in state.recently_expired] == ["B-EXP"]
    assert [i.id for i in state.expiring_soon] == ["B-SOON"]


def test_expiry_windows_are_configurable() -> None:
    today = "2026-07-08"
    rows = [_row("expired-10.md", valid_until="2026-06-28", doc_id="EXP-10")]
    # A 5-day window excludes a doc expired 10 days ago.
    state = compute_maintenance_state(
        rows, today=today, ledger_path=LEDGER_PATH, recently_expired_days=5,
    )
    assert state.recently_expired_count == 0
    # A 15-day window includes it.
    state2 = compute_maintenance_state(
        rows, today=today, ledger_path=LEDGER_PATH, recently_expired_days=15,
    )
    assert state2.recently_expired_count == 1


# --- scenario 8: ledger doc excluded from its own audit -------------------


def test_ledger_path_excluded_from_its_own_audit() -> None:
    rows = [
        _row(LEDGER_PATH, status="active", valid_until="2026-01-01"),
        _row("real.md", status="active"),
    ]
    state = compute_maintenance_state(rows, today="2026-07-08", ledger_path=LEDGER_PATH)
    assert state.status_present_in_all_kb_entries is True
    assert state.recently_expired_count == 0
    assert state.expiring_soon_count == 0


def test_ledger_path_excluded_even_when_missing_status() -> None:
    """Belt-and-suspenders: even if the ledger doc somehow had no status, it
    must never appear in the missing-status list (it is excluded by path, not
    merely because it normally carries valid frontmatter)."""
    rows = [_row(LEDGER_PATH, status=""), _row("real.md", status="active")]
    state = compute_maintenance_state(rows, today="2026-07-08", ledger_path=LEDGER_PATH)
    assert state.status_present_in_all_kb_entries is True
    assert LEDGER_PATH not in state.missing_status_paths


# --- pending_actions envelope ---------------------------------------------


def test_pending_actions_is_none_when_state_is_none() -> None:
    assert pending_actions_for(None) is None


def test_pending_actions_is_none_when_clean() -> None:
    state = MaintenanceState(status_present_in_all_kb_entries=True)
    assert pending_actions_for(state) is None


def test_pending_actions_present_when_dirty_with_all_three_kinds() -> None:
    state = MaintenanceState(
        status_present_in_all_kb_entries=False,
        missing_status_paths=("a.md",), missing_status_count=1,
        recently_expired=(ExpiryItem(id="X", path="x.md", valid_until="2026-06-28"),),
        recently_expired_count=1,
        expiring_soon=(ExpiryItem(id="Y", path="y.md", valid_until="2026-07-18"),),
        expiring_soon_count=1,
    )
    actions = pending_actions_for(state)
    assert actions is not None
    kinds = {a["kind"] for a in actions}
    assert kinds == {"missing_status", "recently_expired", "expiring_soon"}
    for a in actions:
        assert "operator confirmation" in a["message"]
        assert isinstance(a["count"], int)


# --- render / parse round-trip --------------------------------------------


def test_render_parse_round_trip_preserves_state() -> None:
    state = compute_maintenance_state(
        [
            _row("a.md", status=""),
            _row("b.md", valid_until="2026-06-28", doc_id="B"),
        ],
        today="2026-07-08", ledger_path=LEDGER_PATH,
    )
    md = render_ledger_markdown(state, computed_at_iso="2026-07-08T00:00:00+00:00")
    assert md.startswith("---\n")
    assert "id: maintenance-ledger" in md
    assert "status: active" in md
    parsed = parse_ledger_state(md)
    assert parsed == state


def test_render_ledger_markdown_lints_clean() -> None:
    """The rendered doc carries its own valid id/type/status/tier so it lints
    clean rather than relying on reserved-filename exemption (issue #113)."""
    from pathlib import Path

    from data_olympus.format.document import Document
    from data_olympus.format.frontmatter import parse_frontmatter
    from data_olympus.format.validate import validate_document

    state = MaintenanceState(status_present_in_all_kb_entries=True)
    md = render_ledger_markdown(state, computed_at_iso="2026-07-08T00:00:00+00:00")
    fm, body = parse_frontmatter(md)
    doc = Document(path=Path("tooling/maintenance-ledger.md"), frontmatter=fm, body=body)
    findings = validate_document(doc, today="2026-07-08")
    errors = [f for f in findings if f.severity == "error"]
    assert errors == []


def test_parse_ledger_state_handles_absent_or_malformed_content() -> None:
    assert parse_ledger_state(None) is None
    assert parse_ledger_state("") is None
    assert parse_ledger_state("no frontmatter here") is None
    assert parse_ledger_state("---\nid: x\n---\nno maintenance block\n") is None
