"""Integration test for the issue #114 migration contract: a status-less
document is a `kb lint` error, but the reference implementation keeps SERVING
it (never as in-force) while the maintenance ledger (#113) tracks it as an
open item, until the operator adds `status` and the corpus goes clean.

This ties together five previously-separate guarantees (lint tier,
Index.search default-vs-in_force behavior, the maintenance ledger, and its
pending_actions CTA) as a single tested contract, rather than leaving the
composition implicit."""
from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from data_olympus.format.lint import lint_bundle
from data_olympus.index import DEFAULT_AUTOFILL_STATUS, Index
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
    # This test pins the CONSERVATIVE pre-#147 contract (served but never
    # in-force), which #147 preserved behind the KB_STATUS_AUTOFILL knob: with
    # autofill OFF the status-less doc is exactly this served-but-not-in-force
    # shape. (The default-ON virtual-autofill behavior is covered by the
    # test_virtual_autofill_* tests below.)
    idx = Index(tmp_path / "idx.db", status_autofill=False)
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

    idx2 = Index(tmp_path / "idx2.db", status_autofill=False)
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


# ---------------------------------------------------------------------------
# Issue #147 / KNA-69: auto-add a safe default `status` to legacy corpus docs.
#
# Two lanes, kept side-effect-free at index time:
#   1. VIRTUAL autofill (default on): the build treats a status-less doc as
#      `active` IN MEMORY (SQLite index only), never touching the markdown
#      source or dirtying the worktree.
#   2. EXPLICIT persistence: `data-olympus migrate status --apply` writes the
#      `status` field into the physical files, idempotently and audited.
# ---------------------------------------------------------------------------


def _snapshot_texts(kb: Path) -> dict[str, str]:
    """Every markdown file's exact bytes under ``kb``, keyed by relative path."""
    return {
        str(p.relative_to(kb)): p.read_text(encoding="utf-8")
        for p in sorted(kb.rglob("*.md"))
    }


def test_virtual_autofill_serves_legacy_docs_in_force_without_mutation(
    tmp_path: Path,
) -> None:
    """Lane 1: with autofill ON (default), a status-less legacy doc is served
    AND in-force, but the markdown source is left byte-for-byte untouched (the
    build is a read-only parse)."""
    kb = tmp_path / "kb"
    nostatus_path = _write_kb(kb)
    before = _snapshot_texts(kb)

    idx = Index(tmp_path / "idx.db")  # status_autofill defaults to True
    idx.build(kb, source_commit="x", today="2026-07-08")

    # Every doc (including the status-less one) is in-force under a hard filter.
    in_force_ids = {
        h.id for h in kb_search_fn(idx=idx, query="gizmo", limit=10, in_force=True).hits
    }
    assert {"STD-U-CLEAN", "STD-U-NOSTATUS"} <= in_force_ids

    # The indexed status of the legacy doc is the autofilled default...
    got = idx.get("STD-U-NOSTATUS")
    assert got is not None
    assert got.status == DEFAULT_AUTOFILL_STATUS == "active"

    # ...but NOTHING on disk changed: the build never wrote to the source file.
    # A byte-for-byte snapshot equality is the real invariant (the source doc's
    # body legitimately contains the word "status", so a substring check would
    # be meaningless).
    assert _snapshot_texts(kb) == before
    assert nostatus_path.read_text(encoding="utf-8") == before[
        str(nostatus_path.relative_to(kb))
    ]


def test_virtual_autofill_off_restores_conservative_behavior(tmp_path: Path) -> None:
    """With autofill OFF, a status-less doc is served but NEVER in-force -- the
    pre-#147 stance, preserved behind the knob."""
    kb = tmp_path / "kb"
    _write_kb(kb)
    idx = Index(tmp_path / "idx.db", status_autofill=False)
    idx.build(kb, source_commit="x", today="2026-07-08")

    default_ids = {h.id for h in kb_search_fn(idx=idx, query="gizmo", limit=10).hits}
    assert "STD-U-NOSTATUS" in default_ids  # still served
    in_force_ids = {
        h.id for h in kb_search_fn(idx=idx, query="gizmo", limit=10, in_force=True).hits
    }
    assert "STD-U-NOSTATUS" not in in_force_ids  # but never in-force


def test_virtual_autofill_does_not_dirty_git_worktree(tmp_path: Path) -> None:
    """Lane 1 must not dirty the git worktree: building an index over a git
    corpus with status-less docs leaves `git status` clean."""
    kb = tmp_path / "kb"
    _write_kb(kb)
    subprocess.run(["git", "-C", str(kb), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(kb), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(kb), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "seed"],
        check=True,
    )

    # Index DB lives OUTSIDE the corpus so it cannot itself show as untracked.
    idx = Index(tmp_path / "idx.db")
    idx.build(kb, source_commit="x", today="2026-07-08")

    dirty = subprocess.run(
        ["git", "-C", str(kb), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert dirty == ""


def test_migrate_status_apply_persists_idempotently_and_audited(
    tmp_path: Path,
) -> None:
    """Lane 2: `apply_status_autofill` writes `status: active` into the
    status-less doc on disk, leaves already-statused docs untouched, is a no-op
    on a second run, and records one audited event per changed file."""
    from data_olympus.audit_log import AuditLog
    from data_olympus.status_migrate import (
        DEFAULT_STATUS,
        apply_status_autofill,
    )

    kb = tmp_path / "kb"
    nostatus_path = _write_kb(kb)
    clean_path = kb / "universal" / "foundation" / "STD-U-CLEAN.md"
    clean_before = clean_path.read_text(encoding="utf-8")

    audit_path = str(tmp_path / "audit.log")
    audit = AuditLog(log_path=audit_path)

    # First apply: rewrites exactly the status-less doc.
    result = apply_status_autofill(kb, audit_log=audit)
    assert result.changed == 1
    rel_nostatus = str(nostatus_path.relative_to(kb))
    assert result.changed_paths == (rel_nostatus,)
    assert result.already_present == 1  # the CLEAN doc

    # The persisted file now parses with status == active and re-indexes as
    # in-force with NO virtual autofill needed.
    from data_olympus.format.document import Document
    migrated = Document.load(nostatus_path)
    assert migrated.frontmatter.get("status") == DEFAULT_STATUS == "active"
    idx = Index(tmp_path / "idx.db", status_autofill=False)
    idx.build(kb, source_commit="x", today="2026-07-08")
    in_force_ids = {
        h.id for h in kb_search_fn(idx=idx, query="gizmo", limit=10, in_force=True).hits
    }
    assert "STD-U-NOSTATUS" in in_force_ids

    # The already-statused doc was left byte-for-byte untouched.
    assert clean_path.read_text(encoding="utf-8") == clean_before

    # Idempotent: a second apply changes nothing.
    result2 = apply_status_autofill(kb, audit_log=audit)
    assert result2.changed == 0
    assert result2.already_present == 2

    # Audited: exactly one committed status_migrate event across both runs.
    events = [
        e for e in audit.iter_filtered()
        if e.get("event_type") == "status_migrate"
    ]
    assert len(events) == 1
    assert events[0]["target_path"] == rel_nostatus
    assert events[0]["status"] == "committed"
    assert events[0]["value"] == "active"


def test_migrate_status_preserves_previously_in_force_legacy_docs(
    tmp_path: Path,
) -> None:
    """A legacy doc that WAS already in-force (had `status: active`) keeps its
    status verbatim through the migration -- migration only fills the gap, never
    overwrites an existing status."""
    from data_olympus.status_migrate import apply_status_autofill

    kb = tmp_path / "kb"
    _write_kb(kb)
    # STD-U-CLEAN already has status: active. Migrate, then confirm it is
    # untouched and still in-force.
    result = apply_status_autofill(kb)
    assert result.changed == 1  # only the status-LESS doc

    idx = Index(tmp_path / "idx.db", status_autofill=False)
    idx.build(kb, source_commit="x", today="2026-07-08")
    in_force_ids = {
        h.id for h in kb_search_fn(idx=idx, query="gizmo", limit=10, in_force=True).hits
    }
    assert "STD-U-CLEAN" in in_force_ids
