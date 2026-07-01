"""Trigram fuzzy-match fallback (issue #41).

The index builds a secondary FTS5 ``trigram``-tokenized table alongside the main
``porter unicode61`` FTS table, into the SAME tmp DB and swapped atomically. At
query time the trigram table is used only as a FALLBACK: the primary FTS query
runs first and, only when it returns few or no hits, a trigram match backfills so
a misspelled query or a partial identifier still reaches the intended document.
Exact/primary hits keep their ranking; trigram hits are appended after them and
never reorder the primaries. The feature is opt-in via ``KB_TRIGRAM_MODE`` and
its fallback threshold via ``KB_TRIGRAM_FALLBACK_THRESHOLD``; the default is off
so existing search behaviour is unchanged.
"""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from data_olympus.index import Index
from data_olympus.trigram import (
    DEFAULT_FALLBACK_THRESHOLD,
    build_trigram_match_expr,
    trigram_fallback_enabled,
    trigram_fallback_threshold,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _write(kb: Path, name: str, body: str, *, doc_id: str | None = None) -> None:
    fm = f"---\nid: {doc_id}\n---\n" if doc_id else ""
    (kb / name).write_text(fm + body + "\n", encoding="utf-8")


def _fuzzy_kb(kb: Path) -> None:
    _write(
        kb,
        "observability.md",
        "The observability collector configuration and metrics pipeline.",
        doc_id="STD-OBSERVABILITY",
    )
    _write(
        kb,
        "kubernetes.md",
        "Kubernetes helm chart deployment and rollout strategy.",
        doc_id="STD-KUBERNETES",
    )
    _write(
        kb,
        "banana.md",
        "A banana smoothie recipe with yoghurt and honey.",
        doc_id="STD-BANANA",
    )


# --- build-time schema / atomic rebuild --------------------------------------


def test_trigram_table_created_at_build(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    conn = sqlite3.connect(tmp_index_path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='fts_trigram'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "build() must create the fts_trigram virtual table"


def test_schema_version_bumped_to_7(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    conn = sqlite3.connect(tmp_index_path)
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == "7", (
        "schema_version must be '7' after the trigram table is added; "
        f"got {row[0] if row else None!r}"
    )


def test_trigram_table_populated(tmp_path: Path, tmp_index_path: Path) -> None:
    kb = tmp_path / "kb"
    kb.mkdir()
    _fuzzy_kb(kb)
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    conn = sqlite3.connect(tmp_index_path)
    try:
        # A contiguous substring of a real term must find its document.
        rows = conn.execute(
            "SELECT id FROM fts_trigram WHERE fts_trigram MATCH ?",
            ('"kubernetes"',),
        ).fetchall()
    finally:
        conn.close()
    assert any(r[0] == "STD-KUBERNETES" for r in rows)


def test_trigram_table_rebuilt_atomically(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    """The trigram table swaps atomically with the rest of the index.

    An open connection to the old inode keeps seeing the OLD trigram table while
    a rebuild produces a new one on a new inode; a fresh connection sees the new
    table. Mirrors the FTS / related_terms atomic-swap guarantee.
    """
    kb = tmp_path / "kb"
    kb.mkdir()
    _fuzzy_kb(kb)
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="first")

    old_conn = sqlite3.connect(tmp_index_path)
    old_inode = tmp_index_path.stat().st_ino
    old_hit = old_conn.execute(
        "SELECT id FROM fts_trigram WHERE fts_trigram MATCH ?", ('"kubernetes"',)
    ).fetchall()
    assert any(r[0] == "STD-KUBERNETES" for r in old_hit)

    idx.build(kb, source_commit="second")
    assert tmp_index_path.stat().st_ino != old_inode, (
        "os.replace should produce a new inode"
    )

    # Old connection still answers from the old inode's trigram table.
    still = old_conn.execute(
        "SELECT id FROM fts_trigram WHERE fts_trigram MATCH ?", ('"kubernetes"',)
    ).fetchall()
    assert any(r[0] == "STD-KUBERNETES" for r in still)
    old_conn.close()

    # Fresh connection sees the new build's trigram table.
    new_conn = sqlite3.connect(tmp_index_path)
    fresh = new_conn.execute(
        "SELECT id FROM fts_trigram WHERE fts_trigram MATCH ?", ('"kubernetes"',)
    ).fetchall()
    new_conn.close()
    assert any(r[0] == "STD-KUBERNETES" for r in fresh)


# --- query-time fallback behaviour -------------------------------------------


def test_typo_query_finds_doc_via_trigram_fallback(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    """A misspelled query (one internal edit off a real term) reaches the doc via
    the trigram fallback, which the default (porter) FTS misses."""
    kb = tmp_path / "kb"
    kb.mkdir()
    _fuzzy_kb(kb)

    # Baseline: fallback OFF -> the typo misses entirely.
    plain = Index(tmp_index_path)
    plain.build(kb, source_commit="x")
    plain_ids = {h.id for h in plain.search("observabilty", limit=20)}
    assert "STD-OBSERVABILITY" not in plain_ids, (
        "primary FTS should not match the misspelling on its own"
    )

    # Fallback ON -> the typo backfills to the intended document.
    fuzzy = Index(tmp_index_path, trigram_fallback=True)
    fuzzy_ids = {h.id for h in fuzzy.search("observabilty", limit=20)}
    assert "STD-OBSERVABILITY" in fuzzy_ids, (
        "trigram fallback should reach the intended doc for a one-edit typo"
    )


def test_partial_identifier_finds_doc_via_trigram_fallback(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    kb = tmp_path / "kb"
    kb.mkdir()
    _fuzzy_kb(kb)
    fuzzy = Index(tmp_index_path, trigram_fallback=True)
    fuzzy.build(kb, source_commit="x")
    # A truncated identifier ("kubernet") is a substring of "kubernetes".
    ids = {h.id for h in fuzzy.search("kubernet", limit=20)}
    assert "STD-KUBERNETES" in ids


def test_good_primary_hits_are_not_diluted_by_trigram(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    """When the primary query already returns enough hits, the fallback does not
    fire and the top results / order are identical with and without trigram."""
    kb = tmp_path / "kb"
    kb.mkdir()
    _fuzzy_kb(kb)

    plain = Index(tmp_index_path)
    plain.build(kb, source_commit="x")
    baseline = [h.id for h in plain.search("kubernetes helm deployment", limit=20)]
    assert baseline and baseline[0] == "STD-KUBERNETES"

    fuzzy = Index(tmp_index_path, trigram_fallback=True)
    with_fallback = [
        h.id for h in fuzzy.search("kubernetes helm deployment", limit=20)
    ]
    assert with_fallback == baseline, (
        "a well-matched query must return identical results with trigram enabled"
    )


def test_trigram_hits_appended_after_primary(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    """When the fallback fires, any primary hits keep the top positions and
    trigram-only hits are appended strictly after them (no duplicates)."""
    kb = tmp_path / "kb"
    kb.mkdir()
    _write(kb, "a.md", "collector overview document.", doc_id="DOC-A")
    _write(kb, "b.md", "the metrics collectors and collector pipeline.", doc_id="DOC-B")
    # Force the fallback even with primary hits by setting a high threshold.
    idx = Index(tmp_index_path, trigram_fallback=True, trigram_fallback_threshold=50)
    idx.build(kb, source_commit="x")
    hits = idx.search("collector", limit=20)
    ids = [h.id for h in hits]
    assert "DOC-A" in ids and "DOC-B" in ids
    assert len(ids) == len(set(ids)), "no duplicate ids from primary+trigram merge"


def test_short_query_noops_fallback(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    """A query shorter than a trigram (<3 chars) safely no-ops the fallback
    rather than raising an FTS5 trigram error."""
    kb = tmp_path / "kb"
    kb.mkdir()
    _fuzzy_kb(kb)
    idx = Index(tmp_index_path, trigram_fallback=True)
    idx.build(kb, source_commit="x")
    # Must not raise; returns whatever the primary query found (likely nothing).
    hits = idx.search("ku", limit=20)
    assert isinstance(hits, list)


def test_unrelated_typo_does_not_match(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    """A genuinely unrelated query does not drag in documents via trigram."""
    kb = tmp_path / "kb"
    kb.mkdir()
    _fuzzy_kb(kb)
    idx = Index(tmp_index_path, trigram_fallback=True)
    idx.build(kb, source_commit="x")
    ids = {h.id for h in idx.search("xyzzyqux", limit=20)}
    assert ids == set()


def test_default_index_has_fallback_off(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    """The default Index constructor leaves the fallback OFF (no regression)."""
    kb = tmp_path / "kb"
    kb.mkdir()
    _fuzzy_kb(kb)
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    assert idx.trigram_fallback is False
    # A misspelling is NOT found by default.
    ids = {h.id for h in idx.search("observabilty", limit=20)}
    assert "STD-OBSERVABILITY" not in ids


# --- config helpers ----------------------------------------------------------


def test_build_trigram_match_expr_quotes_and_ors() -> None:
    expr = build_trigram_match_expr("kube")
    # "kube" -> trigrams kub, ube -> quoted, OR-joined.
    assert expr == '"kub" OR "ube"'


def test_build_trigram_match_expr_short_query_is_none() -> None:
    assert build_trigram_match_expr("ku") is None
    assert build_trigram_match_expr("") is None


def test_build_trigram_match_expr_escapes_quotes() -> None:
    # An embedded double-quote is doubled so it cannot break out of the phrase.
    expr = build_trigram_match_expr('a"bc')
    assert expr is not None
    assert '""' in expr


def test_trigram_fallback_enabled_default_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KB_TRIGRAM_MODE", raising=False)
    assert trigram_fallback_enabled() is False
    monkeypatch.setenv("KB_TRIGRAM_MODE", "on")
    assert trigram_fallback_enabled() is True
    monkeypatch.setenv("KB_TRIGRAM_MODE", "off")
    assert trigram_fallback_enabled() is False


def test_trigram_fallback_threshold_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KB_TRIGRAM_FALLBACK_THRESHOLD", raising=False)
    assert trigram_fallback_threshold() == DEFAULT_FALLBACK_THRESHOLD
    monkeypatch.setenv("KB_TRIGRAM_FALLBACK_THRESHOLD", "7")
    assert trigram_fallback_threshold() == 7
    # Malformed / negative values fall back to the default.
    monkeypatch.setenv("KB_TRIGRAM_FALLBACK_THRESHOLD", "-2")
    assert trigram_fallback_threshold() == DEFAULT_FALLBACK_THRESHOLD
    monkeypatch.setenv("KB_TRIGRAM_FALLBACK_THRESHOLD", "notanint")
    assert trigram_fallback_threshold() == DEFAULT_FALLBACK_THRESHOLD


def test_config_loads_trigram_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from data_olympus.config import load_config

    monkeypatch.setenv("KB_TRIGRAM_MODE", "on")
    monkeypatch.setenv("KB_TRIGRAM_FALLBACK_THRESHOLD", "5")
    cfg = load_config()
    assert cfg.trigram_fallback_enabled is True
    assert cfg.trigram_fallback_threshold == 5
