"""Tests for the lifecycle-relationship edges table (issue #110, slice 1).

The indexer extracts `supersedes` / `superseded_by` / `contradicts` front
matter into a dedicated `edges` table: (source_id, rel, target_id). Slice 2
consumes this table for in-force graph exclusion and retrieval surfacing;
this slice only needs the table to exist and be populated correctly.
"""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from data_olympus.index import Index

if TYPE_CHECKING:
    from pathlib import Path


def _edge_rows(index_path: Path) -> set[tuple[str, str, str]]:
    conn = sqlite3.connect(index_path)
    rows = conn.execute("SELECT source_id, rel, target_id FROM edges").fetchall()
    conn.close()
    return set(rows)


def test_edges_table_populated_from_status_kb(status_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(status_kb, source_commit="x")
    rows = _edge_rows(tmp_index_path)
    assert rows == {
        ("STD-OLD", "superseded_by", "STD-NEW"),
        ("STD-NEW", "supersedes", "STD-OLD"),
    }


def test_edges_table_rebuild_is_idempotent(status_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(status_kb, source_commit="first")
    first = _edge_rows(tmp_index_path)
    idx.build(status_kb, source_commit="second")
    second = _edge_rows(tmp_index_path)
    assert first == second == {
        ("STD-OLD", "superseded_by", "STD-NEW"),
        ("STD-NEW", "supersedes", "STD-OLD"),
    }


def test_edges_table_multi_target_supersedes_and_contradicts(
    tmp_path: Path, tmp_index_path: Path
) -> None:
    kb = tmp_path / "kb"
    d = kb / "universal" / "foundation"
    d.mkdir(parents=True)
    (d / "STD-3.md").write_text(
        "---\nid: STD-3\ntier: T1\ntype: standard\nstatus: active\n"
        "supersedes:\n  - STD-1\n  - STD-2\n"
        "contradicts:\n  - STD-9\n"
        "---\n# Body\n"
    )
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    rows = _edge_rows(tmp_index_path)
    assert rows == {
        ("STD-3", "supersedes", "STD-1"),
        ("STD-3", "supersedes", "STD-2"),
        ("STD-3", "contradicts", "STD-9"),
    }


def test_edges_table_empty_when_no_lifecycle_fields(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    assert _edge_rows(tmp_index_path) == set()


def test_schema_version_bumped_for_edges_table(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    conn = sqlite3.connect(tmp_index_path)
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "10", (
        f"schema_version must be '10' (v9 edges table, v10 validity columns); got {row[0]!r}"
    )
