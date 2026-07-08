"""Tests for SQLite FTS5 index."""
from __future__ import annotations

from typing import TYPE_CHECKING

from data_olympus.index import Index, IndexBuildResult, SearchHit, _classify_by_path

if TYPE_CHECKING:
    from pathlib import Path


def test_build_index_indexes_all_md_files(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    result = idx.build(tmp_kb, source_commit="deadbeef")
    assert isinstance(result, IndexBuildResult)
    # 3 T1 STDs + 1 T2 STD + 1 T3 project README + 1 T4 component AGENTS.md
    # + 1 GDEC + 1 WF + 1 operator override + 1 tooling = 10 docs
    assert result.docs_indexed == 10
    assert result.source_commit == "deadbeef"
    assert tmp_index_path.exists()


def test_search_finds_by_body_content(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    hits = idx.search("worktree", limit=10)
    # Both STD-U-001 (body) and tooling/worktrees.md match the word "worktree"
    assert len(hits) == 2
    assert isinstance(hits[0], SearchHit)
    ids = {h.id for h in hits}
    assert "STD-U-001" in ids


def test_search_finds_by_id_in_front_matter(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    hits = idx.search("STD-U-002", limit=10)
    assert any(h.id == "STD-U-002" for h in hits)


def test_search_respects_limit(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    hits = idx.search("STD", limit=2)
    assert len(hits) <= 2


def test_search_returns_snippet_with_query_term(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    hits = idx.search("worktree", limit=5)
    assert hits
    assert "worktree" in hits[0].snippet.lower()


def test_outline_returns_tier_and_category_counts(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    outline = idx.outline()
    # Under tier model v2 the fixture spans all four content tiers plus meta
    # tiers (decisions, workflows, operator, tooling).
    tier_names = {t["name"] for t in outline}
    expected_tiers = {"T1", "T2", "T3", "T4", "decisions", "workflows", "memory", "tooling"}
    assert expected_tiers.issubset(tier_names), (
        f"missing tiers in outline; got {tier_names}, expected superset of {expected_tiers}"
    )


def test_index_health_after_build(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="deadbeef")
    health = idx.health()
    assert health["source_commit"] == "deadbeef"
    assert health["total_docs"] == 10
    assert health["index_built_at"] is not None


def test_rebuild_overwrites_previous(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="first")
    idx.build(tmp_kb, source_commit="second")
    health = idx.health()
    assert health["source_commit"] == "second"


def test_index_stores_content_markdown(tmp_kb: Path, tmp_index_path: Path) -> None:
    """The docs table stores the full file text in content_markdown."""
    import sqlite3
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    conn = sqlite3.connect(tmp_index_path)
    row = conn.execute(
        "SELECT content_markdown FROM docs WHERE id = 'STD-U-001'"
    ).fetchone()
    conn.close()
    assert row is not None, "STD-U-001 should be in docs"
    body = row[0]
    assert "worktree" in body, "content_markdown must include the full body text"
    assert body.startswith("---"), "content_markdown must include front matter"


def test_index_stores_last_modified(tmp_git_kb: Path, tmp_index_path: Path) -> None:
    """last_modified is populated from git log for tracked files."""
    import sqlite3
    idx = Index(tmp_index_path)
    idx.build(tmp_git_kb, source_commit="x")
    conn = sqlite3.connect(tmp_index_path)
    row = conn.execute(
        "SELECT last_modified, last_modified_source FROM docs WHERE id = 'STD-U-001'"
    ).fetchone()
    conn.close()
    assert row is not None
    last_modified, source = row
    assert last_modified, "last_modified must not be empty"
    # ISO-8601 starts with YYYY-MM-DDTHH:MM:SS
    assert len(last_modified) >= 19 and last_modified[4] == "-" and last_modified[10] in "T ", (
        f"last_modified must be ISO-8601-ish; got {last_modified!r}"
    )
    assert source == "git", f"source must be 'git' for tracked files; got {source!r}"


def test_index_records_schema_version(tmp_kb: Path, tmp_index_path: Path) -> None:
    import sqlite3
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    conn = sqlite3.connect(tmp_index_path)
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "10", (
        f"schema_version must be '10' after the edges table (issue #110, v9) "
        f"and the validity/freshness columns (issue #107, v10); got {row[0]!r}"
    )


def test_path_classification_T1_for_standards(tmp_kb: Path, tmp_index_path: Path) -> None:
    """universal/foundation/STD-U-* files get tier=T1, category=foundation
    from the path even when front matter is missing."""
    import sqlite3
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    conn = sqlite3.connect(tmp_index_path)
    rows = conn.execute(
        "SELECT id, tier, category FROM docs WHERE path LIKE 'universal/foundation/%'"
    ).fetchall()
    conn.close()
    assert rows, "expected at least one foundation STD in the fixture"
    for id_, tier, category in rows:
        assert tier == "T1", f"{id_}: expected tier=T1, got {tier}"
        assert category == "foundation", f"{id_}: expected category=foundation, got {category}"


def test_path_classification_decisions(tmp_kb: Path, tmp_index_path: Path) -> None:
    import sqlite3
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    conn = sqlite3.connect(tmp_index_path)
    row = conn.execute(
        "SELECT tier, category FROM docs WHERE id = 'DEC-001'"
    ).fetchone()
    conn.close()
    # DEC-001 has front matter `tier: decisions` already; this verifies path
    # derivation doesn't override existing front matter.
    assert row == ("decisions", "decisions")


def test_path_classification_workflows_no_front_matter(tmp_kb: Path, tmp_index_path: Path) -> None:
    """workflows/* without front matter get tier=workflows, category=workflows."""
    import sqlite3
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    conn = sqlite3.connect(tmp_index_path)
    row = conn.execute(
        "SELECT tier, category FROM docs WHERE path LIKE 'workflows/%'"
    ).fetchone()
    conn.close()
    assert row == ("workflows", "workflows")


def test_path_classification_operator_agent_overrides(tmp_kb: Path, tmp_index_path: Path) -> None:
    import sqlite3
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    conn = sqlite3.connect(tmp_index_path)
    row = conn.execute(
        "SELECT tier, category FROM docs WHERE path LIKE 'memory/accepted/%'"
    ).fetchone()
    conn.close()
    assert row == ("memory", "memory-accepted")


def test_path_classification_tooling(tmp_kb: Path, tmp_index_path: Path) -> None:
    import sqlite3
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    conn = sqlite3.connect(tmp_index_path)
    row = conn.execute(
        "SELECT tier, category FROM docs WHERE path LIKE 'tooling/%'"
    ).fetchone()
    conn.close()
    assert row == ("tooling", "tooling")


def test_path_classification_front_matter_overrides_path(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    """Front matter tier/category override path-derived values per-key."""
    import sqlite3
    kb = tmp_path / "kb"
    (kb / "universal" / "foundation").mkdir(parents=True)
    (kb / "universal" / "foundation" / "STD-OVERRIDE.md").write_text(
        "---\nid: STD-OVERRIDE\ntier: custom-tier\ncategory: custom-category\n---\n# Body\n"
    )
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    conn = sqlite3.connect(tmp_index_path)
    row = conn.execute("SELECT tier, category FROM docs WHERE id='STD-OVERRIDE'").fetchone()
    conn.close()
    assert row == ("custom-tier", "custom-category"), (
        f"front matter should override path derivation; got {row}"
    )


def test_excluded_dirs_not_indexed(tmp_kb: Path, tmp_index_path: Path) -> None:
    """Files inside excluded directories are skipped during index build."""
    import sqlite3
    # Add files in directories that should be excluded.
    for excluded in [".pytest_cache", ".venv", "archive", "_archive", ".worktrees",
                     "to-delete", "node_modules"]:
        d = tmp_kb / excluded
        d.mkdir(exist_ok=True)
        (d / "junk.md").write_text("# Junk\n\nShould not be indexed.\n")
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    conn = sqlite3.connect(tmp_index_path)
    junk_count = conn.execute(
        "SELECT COUNT(*) FROM docs WHERE path LIKE '%junk%'"
    ).fetchone()[0]
    conn.close()
    assert junk_count == 0, f"excluded dirs leaked into index; junk count = {junk_count}"


def test_excluded_dirs_nested(tmp_kb: Path, tmp_index_path: Path) -> None:
    """Excluded dirs work at any depth, not just at root."""
    import sqlite3
    nested = tmp_kb / "universal" / ".pytest_cache"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "junk.md").write_text("# Nested junk\n")
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    conn = sqlite3.connect(tmp_index_path)
    junk = conn.execute("SELECT COUNT(*) FROM docs WHERE path LIKE '%pytest_cache%'").fetchone()[0]
    conn.close()
    assert junk == 0


def test_get_returns_full_doc(tmp_kb: Path, tmp_index_path: Path) -> None:
    from data_olympus.index import IndexedDoc
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="abc")
    doc = idx.get("STD-U-001")
    assert isinstance(doc, IndexedDoc)
    assert doc.id == "STD-U-001"
    assert doc.path == "universal/foundation/STD-U-001-test-policy.md"
    assert doc.tier == "T1"
    assert doc.category == "foundation"
    assert "worktree" in doc.content_markdown
    assert doc.content_markdown.startswith("---"), "must include front matter"


def test_get_returns_none_for_missing_id(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    doc = idx.get("STD-DOES-NOT-EXIST")
    assert doc is None


def test_get_includes_last_modified(tmp_git_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_git_kb, source_commit="x")
    doc = idx.get("STD-U-001")
    assert doc is not None
    assert doc.last_modified
    assert doc.last_modified_source == "git"


def test_get_includes_source_commit_from_meta(tmp_kb: Path, tmp_index_path: Path) -> None:
    """The returned doc carries source_commit so consumers can detect lag."""
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="commitsha123")
    doc = idx.get("STD-U-001")
    assert doc is not None
    assert doc.source_commit == "commitsha123"


def test_list_filters_by_tier(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    entries = idx.list(tier="T1")
    assert entries, "expected at least one T1 doc"
    for e in entries:
        # Path of T1 docs starts with universal/ under tier model v2.
        assert e["path"].startswith("universal/"), f"non-T1 path in T1 results: {e['path']}"


def test_list_filters_by_tier_and_category(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    entries = idx.list(tier="T1", category="foundation")
    assert entries
    for e in entries:
        assert e["path"].startswith("universal/foundation/")


def test_list_returns_ordered_by_id(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    entries = idx.list(tier="T1")
    ids = [e["id"] for e in entries]
    assert ids == sorted(ids), f"list must return entries ordered by id; got {ids}"


def test_list_returns_id_title_path(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    entries = idx.list(tier="T1", category="foundation")
    assert entries
    e = entries[0]
    assert set(e.keys()) == {"id", "title", "path"}, f"unexpected keys: {e.keys()}"


def test_list_empty_for_unknown_tier(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    entries = idx.list(tier="does-not-exist")
    assert entries == []


def test_atomic_swap_old_inode_serves_during_rebuild(
    tmp_kb: Path, tmp_index_path: Path,
) -> None:
    """Open a read connection, hold it, trigger a rebuild, the open connection still serves."""
    import sqlite3
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="first")
    # Open a long-lived connection to the old inode
    old_conn = sqlite3.connect(tmp_index_path)
    old_inode = tmp_index_path.stat().st_ino
    # Rebuild with a new commit
    idx.build(tmp_kb, source_commit="second")
    # The path's inode should differ (os.replace gave us a new file)
    new_inode = tmp_index_path.stat().st_ino
    assert old_inode != new_inode, "os.replace should produce a new inode"
    # The old connection should still answer
    row = old_conn.execute("SELECT value FROM meta WHERE key='source_commit'").fetchone()
    old_conn.close()
    assert row == ("first",), f"old inode must still report old commit; got {row}"
    # A new connection sees the new commit
    new_conn = sqlite3.connect(tmp_index_path)
    new_row = new_conn.execute("SELECT value FROM meta WHERE key='source_commit'").fetchone()
    new_conn.close()
    assert new_row == ("second",)


def test_docs_table_has_git_remote_url_column(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="test")
    import sqlite3
    conn = sqlite3.connect(tmp_index_path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(docs)").fetchall()}
    finally:
        conn.close()
    assert "git_remote_url" in cols


def test_duplicate_id_aborts_build(tmp_path: Path, tmp_index_path: Path) -> None:
    """If two files claim the same id, build() raises and leaves the previous index intact."""
    from data_olympus.index import DuplicateIdError
    kb = tmp_path / "kb"
    (kb / "universal" / "foundation").mkdir(parents=True)
    (kb / "universal" / "foundation" / "STD-U-007-a.md").write_text(
        "---\nid: STD-U-007\ntier: T1\n---\n# A\n"
    )
    (kb / "universal" / "foundation" / "STD-U-007-b.md").write_text(
        "---\nid: STD-U-007\ntier: T1\n---\n# B\n"
    )
    idx = Index(tmp_index_path)
    # First build with only one of the files: succeeds
    (kb / "universal" / "foundation" / "STD-U-007-b.md").rename(
        kb / "universal" / "foundation" / "STD-U-007-b.md.disabled"
    )
    idx.build(kb, source_commit="ok")
    # Now re-enable the duplicate and try to rebuild
    (kb / "universal" / "foundation" / "STD-U-007-b.md.disabled").rename(
        kb / "universal" / "foundation" / "STD-U-007-b.md"
    )
    import pytest
    with pytest.raises(DuplicateIdError) as exc:
        idx.build(kb, source_commit="should-fail")
    assert "STD-U-007" in str(exc.value)
    # The old index is intact
    import sqlite3
    conn = sqlite3.connect(tmp_index_path)
    row = conn.execute("SELECT value FROM meta WHERE key='source_commit'").fetchone()
    conn.close()
    assert row == ("ok",), f"previous index must remain; got {row}"


# ---- classification under the tier model ----


def test_classify_universal_foundation() -> None:
    assert _classify_by_path("universal/foundation/STD-U-001.md") == ("T1", "foundation")


def test_classify_universal_quality() -> None:
    assert _classify_by_path("universal/quality/STD-U-500.md") == ("T1", "quality")


def test_classify_universal_security() -> None:
    assert _classify_by_path("universal/security/STD-U-600.md") == ("T1", "security")


def test_classify_tech_stacks_backend_nestjs() -> None:
    assert _classify_by_path("tech-stacks/backend-nestjs/STD-BN-001.md") == (
        "T2", "stack:backend-nestjs",
    )


def test_classify_tech_stacks_project_setup() -> None:
    assert _classify_by_path("tech-stacks/project-setup/STD-PS-001.md") == (
        "T2", "stack:project-setup",
    )


def test_classify_project_root_doc() -> None:
    assert _classify_by_path("projects/example-project/README.md") == (
        "T3", "project:example-project"
    )


def test_classify_project_arbitrary_doc() -> None:
    assert _classify_by_path("projects/example-project/architecture.md") == (
        "T3", "project:example-project"
    )


def test_classify_component_agents_md() -> None:
    assert _classify_by_path("projects/example-project/components/payment-service/AGENTS.md") == (
        "T4",
        "component:example-project/payment-service",
    )


def test_classify_component_nested_doc() -> None:
    nested = "projects/example-project/components/payment-service/runbook/oncall.md"
    assert _classify_by_path(nested) == (
        "T4",
        "component:example-project/payment-service",
    )


def test_classify_empty_components_dir_is_t3() -> None:
    # projects/<name>/components/index.md (no component yet) falls back to T3.
    assert _classify_by_path("projects/example-project/components/index.md") == (
        "T3", "project:example-project"
    )


def test_classify_projects_top_level_file_classified_as_t3_with_filename_as_project_name() -> None:
    # Edge: projects/index.md, ("T3", "project:index"). Acceptable; operator
    # can prune from outline if noisy.
    assert _classify_by_path("projects/index.md") == ("T3", "project:index")


def test_list_by_prefix_returns_matching_entries(tmp_kb, tmp_index_path):
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="test")
    entries = idx.list_by_prefix("projects/example-project/")
    paths = {e["path"] for e in entries}
    assert "projects/example-project/README.md" in paths


def test_list_by_prefix_excludes_under(tmp_kb, tmp_index_path):
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="test")
    # Exclude components/ so we only get T3 entries.
    entries = idx.list_by_prefix("projects/example-project/", exclude_under="components/")
    paths = {e["path"] for e in entries}
    assert "projects/example-project/README.md" in paths
    # T4 entry should be excluded.
    assert not any("components/" in p for p in paths)


def test_list_with_remote_url_returns_only_entries_with_url(tmp_kb, tmp_index_path):
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="test")
    entries = idx.list_with_remote_url()
    # Fixture has git_remote_url on T3 + T4 entries.
    urls = [e["git_remote_url"] for e in entries if e.get("git_remote_url")]
    assert len(urls) >= 2


def test_get_returns_status_and_type(status_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(status_kb, source_commit="x")
    doc = idx.get("STD-NEW")
    assert doc is not None
    assert doc.status == "active"
    assert doc.doc_type == "standard"


def test_search_filters_by_status(status_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(status_kb, source_commit="x")
    hits = idx.search("caching", limit=10, status="active")
    assert {h.id for h in hits} == {"STD-NEW"}, (
        f"status=active must exclude superseded/accepted; got {[h.id for h in hits]}"
    )


def test_search_filters_by_type(status_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(status_kb, source_commit="x")
    hits = idx.search("caching", limit=10, doc_type="decision")
    assert {h.id for h in hits} == {"DEC-1"}


def test_search_hit_carries_status_and_type(status_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(status_kb, source_commit="x")
    hits = idx.search("caching", limit=10, doc_type="decision")
    assert hits[0].status == "accepted"
    assert hits[0].doc_type == "decision"


def test_search_no_status_filter_returns_all_matches(status_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(status_kb, source_commit="x")
    hits = idx.search("caching", limit=10)
    assert {h.id for h in hits} == {"STD-OLD", "STD-NEW", "DEC-1"}


def test_docs_table_has_status_and_type_columns(tmp_kb: Path, tmp_index_path: Path) -> None:
    import sqlite3
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    conn = sqlite3.connect(tmp_index_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(docs)").fetchall()}
    conn.close()
    assert {"status", "type"} <= cols, f"missing status/type columns; got {cols}"


def test_build_populates_status_and_type(tmp_path: Path, tmp_index_path: Path) -> None:
    import sqlite3
    kb = tmp_path / "kb"
    (kb / "universal" / "foundation").mkdir(parents=True)
    (kb / "universal" / "foundation" / "STD-S.md").write_text(
        "---\nid: STD-S\ntier: T1\ntype: standard\nstatus: active\n---\n# Body about caching\n"
    )
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    conn = sqlite3.connect(tmp_index_path)
    row = conn.execute("SELECT status, type FROM docs WHERE id='STD-S'").fetchone()
    conn.close()
    assert row == ("active", "standard"), f"status/type not populated; got {row}"


# ---- NL query / OR-of-terms fix ----


def test_search_multiword_nl_query_retrieves(status_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(status_kb, source_commit="x")
    # Previously this exact-phrase query matched nothing; now OR-of-terms retrieves.
    hits = idx.search("current rule for caching", limit=10)
    ids = {h.id for h in hits}
    assert "STD-NEW" in ids, f"NL query must retrieve the caching concept; got {ids}"


def test_search_multiword_composes_with_status_filter(
    status_kb: Path, tmp_index_path: Path,
) -> None:
    idx = Index(tmp_index_path)
    idx.build(status_kb, source_commit="x")
    hits = idx.search("current rule for caching", limit=10, status="active")
    assert {h.id for h in hits} == {"STD-NEW"}, "status=active must still filter NL-query results"


def test_search_or_semantics_matches_any_term(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    # 'worktree' is in STD-U-001/tooling; 'findings' is in STD-U-007.
    hits = idx.search("worktree findings", limit=10)
    ids = {h.id for h in hits}
    assert "STD-U-001" in ids and "STD-U-007" in ids, f"OR must match either term; got {ids}"


def test_search_single_term_id_lookup_still_works(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    hits = idx.search("STD-U-002", limit=10)
    assert any(h.id == "STD-U-002" for h in hits)


def test_search_empty_query_returns_empty(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    assert idx.search("   ", limit=10) == []
    assert idx.search("", limit=10) == []


def test_fts_indexes_applies_when_and_description(tmp_path: Path, tmp_index_path: Path) -> None:
    kb = tmp_path / "kb"
    (kb / "universal" / "foundation").mkdir(parents=True)
    (kb / "universal" / "foundation" / "STD-XL.md").write_text(
        "---\nid: STD-XL\ntier: T1\ntype: standard\nstatus: active\n"
        "applies_when: [openpyxl, insert_cols, spreadsheet]\n"
        "description: Prefer xlsxwriter for new Excel files.\n---\n"
        "# Excel standard\n\nUse the documented Excel approach.\n"
    )
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    # A query term that appears ONLY in applies_when must retrieve the doc.
    hits = idx.search("openpyxl", limit=10)
    assert any(h.id == "STD-XL" for h in hits), "applies_when trigger must be searchable"
    # A query term that appears ONLY in description must retrieve the doc.
    hits2 = idx.search("xlsxwriter", limit=10)
    assert any(h.id == "STD-XL" for h in hits2), "description must be searchable"


def test_docs_table_has_applies_when_and_description_columns(
    tmp_kb: Path, tmp_index_path: Path
) -> None:
    import sqlite3
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    conn = sqlite3.connect(tmp_index_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(docs)").fetchall()}
    conn.close()
    assert {"applies_when", "description"} <= cols


def _excel_governance_kb(tmp_path: Path) -> Path:
    kb = tmp_path / "kb"
    d = kb / "universal" / "foundation"
    d.mkdir(parents=True)
    (d / "STD-XL.md").write_text(
        "---\nid: STD-XL\ntier: T1\ntype: standard\nstatus: active\n"
        "applies_when: [openpyxl, insert_cols, excel]\n"
        "description: Prefer xlsxwriter for new Excel files.\n---\n"
        "# Excel standard\n\nGuidance about spreadsheets.\n"
    )
    (d / "STD-LOG.md").write_text(
        "---\nid: STD-LOG\ntier: T1\ntype: standard\nstatus: active\n"
        "applies_when: [logging, structured-logs]\n"
        "description: Use structured logging.\n---\n"
        "# Logging\n\nopenpyxl is mentioned once here in passing.\n"
    )
    return kb


def test_applies_when_match_outranks_incidental_body_match(
    tmp_path: Path, tmp_index_path: Path
) -> None:
    idx = Index(tmp_index_path)
    idx.build(_excel_governance_kb(tmp_path), source_commit="x")
    hits = idx.search("openpyxl", limit=5)
    assert hits[0].id == "STD-XL", (
        "a doc whose applies_when trigger matches must outrank a doc with only an "
        f"incidental body mention; got {[h.id for h in hits]}"
    )


def test_search_columns_ablation_restricts_match(tmp_path: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(_excel_governance_kb(tmp_path), source_commit="x")
    # Restrict matching to body only: the applies_when-only trigger 'insert_cols'
    # appears in no body, so nothing matches.
    body_only = idx.search("insert_cols", limit=5, columns=["body"])
    assert body_only == []
    # Default (all columns) retrieves via applies_when.
    full = idx.search("insert_cols", limit=5)
    assert any(h.id == "STD-XL" for h in full)


def test_search_rejects_unknown_column(tmp_path: Path, tmp_index_path: Path) -> None:
    import pytest
    idx = Index(tmp_index_path)
    idx.build(_excel_governance_kb(tmp_path), source_commit="x")
    with pytest.raises(ValueError, match="unknown fts column"):
        idx.search("excel", limit=5, columns=["title", "bogus"])
