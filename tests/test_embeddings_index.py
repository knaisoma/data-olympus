"""Build-time vectors + hybrid ranking wired through Index (issue #42).

These tests exercise the enabled path end to end:

- ``Index.build`` populates the ``doc_vectors`` table (schema v8) only when the
  feature is enabled, into the same tmp DB, swapped atomically.
- The build does NOT create/require vectors when the feature is off (default),
  so the zero-dependency lexical product is unchanged.
- A hybrid reranker built from the index beats BM25-only on a paraphrase query
  whose relevant doc shares no tokens with the query.

The model-backed cases SKIP when the embeddings dep/model is unavailable (see
``test_embeddings._MODEL``) rather than making the suite flaky.
"""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from data_olympus import embeddings as emb
from data_olympus.index import Index

from .test_embeddings import _needs_model

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _write(kb: Path, name: str, body: str, *, doc_id: str) -> None:
    (kb / name).write_text(f"---\nid: {doc_id}\n---\n{body}\n", encoding="utf-8")


def _paraphrase_kb(kb: Path) -> None:
    # The relevant doc for the query "car" shares no tokens with "automobile".
    _write(
        kb,
        "auto.md",
        "An automobile is a wheeled motor vehicle used for transportation.",
        doc_id="DOC-AUTO",
    )
    _write(
        kb,
        "banana.md",
        "A banana smoothie recipe with yoghurt and honey and ice.",
        doc_id="DOC-BANANA",
    )
    _write(
        kb,
        "python.md",
        "Python packaging with uv and pyproject metadata and wheels.",
        doc_id="DOC-PYTHON",
    )


def _table_names(db: Path) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        ).fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def _vector_count(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM doc_vectors").fetchone()[0])
    finally:
        conn.close()


# --- default OFF: no vectors, no dep required --------------------------------


def test_build_default_off_has_no_vectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KB_EMBEDDINGS_MODE", raising=False)
    kb = tmp_path / "kb"
    kb.mkdir()
    _paraphrase_kb(kb)
    db = tmp_path / "kb.db"
    idx = Index(db)
    idx.build(kb, source_commit="c0")
    # The doc_vectors table exists in the schema (created empty) but must hold no
    # rows when the feature is off, and no embedding library is needed.
    assert _vector_count(db) == 0


def test_schema_version_is_v8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KB_EMBEDDINGS_MODE", raising=False)
    kb = tmp_path / "kb"
    kb.mkdir()
    _write(kb, "a.md", "hello world", doc_id="DOC-A")
    db = tmp_path / "kb.db"
    Index(db).build(kb, source_commit="c0")
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "8"


def test_doc_vectors_table_present_in_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KB_EMBEDDINGS_MODE", raising=False)
    kb = tmp_path / "kb"
    kb.mkdir()
    _write(kb, "a.md", "hello world", doc_id="DOC-A")
    db = tmp_path / "kb.db"
    Index(db).build(kb, source_commit="c0")
    assert "doc_vectors" in _table_names(db)


# --- enabled: vectors built, atomic rebuild ----------------------------------


@_needs_model
def test_build_enabled_populates_vectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KB_EMBEDDINGS_MODE", "on")
    kb = tmp_path / "kb"
    kb.mkdir()
    _paraphrase_kb(kb)
    db = tmp_path / "kb.db"
    Index(db).build(kb, source_commit="c0")
    assert _vector_count(db) == 3
    # Each stored vector deserialises to a non-empty float list.
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT id, vector FROM doc_vectors").fetchall()
    finally:
        conn.close()
    for _id, blob in rows:
        assert len(emb.deserialize_vector(blob)) > 0


@_needs_model
def test_vectors_rebuild_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KB_EMBEDDINGS_MODE", "on")
    kb = tmp_path / "kb"
    kb.mkdir()
    _paraphrase_kb(kb)
    db = tmp_path / "kb.db"
    idx = Index(db)
    idx.build(kb, source_commit="c0")
    first = _vector_count(db)
    assert first == 3
    # Remove a doc and rebuild: the swapped DB reflects the new corpus exactly
    # (no stale vectors, no leftover tmp files).
    (kb / "banana.md").unlink()
    idx.build(kb, source_commit="c1")
    assert _vector_count(db) == 2
    leftover = list(tmp_path.glob("kb.db.tmp.*"))
    assert leftover == []


@_needs_model
def test_hybrid_beats_bm25_on_paraphrase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core acceptance test: a query with NO shared tokens with the target
    doc retrieves it via hybrid ranking, where BM25-only would miss it."""
    monkeypatch.setenv("KB_EMBEDDINGS_MODE", "on")
    kb = tmp_path / "kb"
    kb.mkdir()
    _paraphrase_kb(kb)
    db = tmp_path / "kb.db"
    idx = Index(db)
    idx.build(kb, source_commit="c0")

    # BM25-only: "car" shares no token with "automobile ... vehicle", so the
    # lexical index returns nothing for it.
    bm25_hits = idx.search("car", limit=3)
    assert not any(h.id == "DOC-AUTO" for h in bm25_hits)

    # Hybrid: embed the corpus-wide candidate pool and blend. We drive the
    # reranker directly over ALL docs (the server composes it into the stack;
    # here we assert the ranking capability in isolation).
    cfg = emb.embeddings_config()
    embedder = emb.build_embedder(cfg)
    reranker = idx.make_hybrid_reranker(embedder, weight=1.0)
    # Candidate pool = every doc as a neutral-bm25 hit, so ranking is decided by
    # cosine alone (weight=1.0). This mirrors how the reranker rescues a doc that
    # bm25 could not surface.
    from data_olympus.index import SearchHit
    pool = [
        SearchHit(id=i, path=f"{i}.md", title=i, snippet="", score=0.0)
        for i in ("DOC-AUTO", "DOC-BANANA", "DOC-PYTHON")
    ]
    ranked = reranker("car", pool)
    assert ranked[0].id == "DOC-AUTO"
