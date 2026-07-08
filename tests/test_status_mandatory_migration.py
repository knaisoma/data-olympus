"""Integration test for the issue #114 migration contract: a status-less
document is a `kb lint` error, but the reference implementation keeps SERVING
it (never as in-force) while the maintenance ledger (#113) tracks it as an
open item, until the operator adds `status` and the corpus goes clean.

This ties together five previously-separate guarantees (lint tier,
Index.search default-vs-in_force behavior, the maintenance ledger, and its
pending_actions CTA) as a single tested contract, rather than leaving the
composition implicit."""
from __future__ import annotations

from typing import TYPE_CHECKING

from data_olympus.format.lint import lint_bundle
from data_olympus.index import Index
from data_olympus.maintenance import pending_actions_for
from data_olympus.tools_read import kb_search_fn

if TYPE_CHECKING:
    from pathlib import Path


def _write_kb(kb: Path) -> Path:
    """A two-document bundle: one fully conformant, one missing `status`.
    Both share the distinctive topic word 'gizmo' so a single query surfaces
    either depending on the `in_force` filter. Returns the status-less doc's
    path."""
    clean_dir = kb / "universal" / "foundation"
    clean_dir.mkdir(parents=True)
    (clean_dir / "STD-U-CLEAN.md").write_text(
        "---\nid: STD-U-CLEAN\ntype: standard\nstatus: active\ntier: T1\n"
        "title: Clean Gizmo Rule\ndescription: d\ntags: [x]\ntimestamp: 2026-01-01\n"
        "---\n# Clean Gizmo Rule\n\nA conformant rule about gizmos.\n",
        encoding="utf-8",
    )
    nostatus_path = clean_dir / "STD-U-NOSTATUS.md"
    nostatus_path.write_text(
        "---\nid: STD-U-NOSTATUS\ntype: standard\ntier: T1\ntitle: Gizmo Draft\n---\n"
        "# Gizmo Draft\n\nA legacy rule about gizmos, missing its status field.\n",
        encoding="utf-8",
    )
    return nostatus_path


def test_status_mandatory_migration_round_trip(tmp_path: Path) -> None:
    kb = tmp_path / "kb"
    nostatus_path = _write_kb(kb)
    rel_nostatus = str(nostatus_path.relative_to(kb))

    # 1. `kb lint` reports the missing status as an ERROR (SPEC.md 4.2 / 9).
    findings = lint_bundle(kb)
    assert nostatus_path in findings
    error_fields = {
        f.field for f in findings[nostatus_path] if f.severity == "error"
    }
    assert "status" in error_fields

    # 2. The indexer still SERVES the status-less doc in a default search...
    idx = Index(tmp_path / "idx.db")
    idx.build(kb, source_commit="x", today="2026-07-08")
    default_ids = {h.id for h in kb_search_fn(idx=idx, query="gizmo", limit=10).hits}
    assert {"STD-U-CLEAN", "STD-U-NOSTATUS"} <= default_ids

    # 3. ...but it is NEVER in-force: it can never govern, and this is exactly
    # the filter kb_consult applies internally (SPEC.md section 8).
    in_force_ids = {
        h.id for h in kb_search_fn(idx=idx, query="gizmo", limit=10, in_force=True).hits
    }
    assert "STD-U-NOSTATUS" not in in_force_ids
    assert "STD-U-CLEAN" in in_force_ids

    # 4. The maintenance ledger (#113) flags the corpus dirty and names the file.
    state = idx.maintenance_state
    assert state is not None
    assert state.status_present_in_all_kb_entries is False
    assert rel_nostatus in state.missing_status_paths

    # 4b. The dirty state produces a `missing_status` pending_actions CTA (the
    # envelope kb_consult/kb_health surface to nag the operator) naming the
    # exact count -- this is the "nags until clean" half of the migration
    # contract, not just the raw flag.
    actions = pending_actions_for(state)
    assert actions is not None
    missing_status_actions = [a for a in actions if a["kind"] == "missing_status"]
    assert len(missing_status_actions) == 1
    assert missing_status_actions[0]["count"] == state.missing_status_count

    # 5. Migration: the operator adds `status`. The corpus goes clean
    # end-to-end -- lint, the ledger flag, AND in-force retrieval all flip
    # together from the SAME edit.
    nostatus_path.write_text(
        "---\nid: STD-U-NOSTATUS\ntype: standard\nstatus: active\ntier: T1\n"
        "title: Gizmo Draft\n---\n"
        "# Gizmo Draft\n\nA legacy rule about gizmos, missing its status field.\n",
        encoding="utf-8",
    )

    findings_after = lint_bundle(kb)
    assert not any(
        f.severity == "error" for f in findings_after.get(nostatus_path, [])
    )

    idx2 = Index(tmp_path / "idx2.db")
    idx2.build(kb, source_commit="y", today="2026-07-08")
    state_after = idx2.maintenance_state
    assert state_after is not None
    assert state_after.status_present_in_all_kb_entries is True
    assert state_after.missing_status_count == 0

    # The CTA disappears entirely once the corpus is clean (no manual acking).
    assert pending_actions_for(state_after) is None

    in_force_ids_after = {
        h.id
        for h in kb_search_fn(idx=idx2, query="gizmo", limit=10, in_force=True).hits
    }
    assert "STD-U-NOSTATUS" in in_force_ids_after
