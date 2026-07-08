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


class _StubEmbedder:
    """Deterministic 2-d embedder for model-free tests of the Config-threaded
    build and dense-source paths. Maps a keyword to a fixed unit vector so cosine
    is predictable; unknown text maps to a distinct direction."""

    model_name = "stub"

    def _vec(self, text: str) -> list[float]:
        t = text.lower()
        if "car" in t or "automobile" in t or "vehicle" in t:
            return [1.0, 0.0]
        if "banana" in t or "smoothie" in t:
            return [0.0, 1.0]
        return [0.0, 1.0]  # unrelated: orthogonal to the "car" direction

    def embed_one(self, text: str) -> list[float]:
        return self._vec(text)

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


def test_build_uses_threaded_config_not_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewer concern 2: build() embeds when a Config is threaded in, even with
    KB_EMBEDDINGS_MODE UNSET (env off). Config is the single source of truth."""
    monkeypatch.delenv("KB_EMBEDDINGS_MODE", raising=False)
    kb = tmp_path / "kb"
    kb.mkdir()
    _paraphrase_kb(kb)
    db = tmp_path / "kb.db"
    cfg = emb.EmbeddingsConfig(model_name="stub", weight=0.5)
    Index(db, embeddings=cfg, embedder=_StubEmbedder()).build(kb, source_commit="c0")
    # Env said off, but the threaded Config turned it on: vectors are stored.
    assert _vector_count(db) == 3


def test_build_off_ignores_env_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewer concern 2 (converse): with NO Config threaded in, build() stores
    no vectors even when KB_EMBEDDINGS_MODE=on in env; the env is not consulted."""
    monkeypatch.setenv("KB_EMBEDDINGS_MODE", "on")
    kb = tmp_path / "kb"
    kb.mkdir()
    _paraphrase_kb(kb)
    db = tmp_path / "kb.db"
    Index(db).build(kb, source_commit="c0")  # no embeddings config
    assert _vector_count(db) == 0


def test_dense_source_unions_tokenless_hit_model_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewer concern 1, model-free: search("car") returns DOC-AUTO via the
    dense candidate source even though bm25 finds nothing, using a stub embedder
    so the assertion is deterministic and needs no model download."""
    monkeypatch.delenv("KB_EMBEDDINGS_MODE", raising=False)
    kb = tmp_path / "kb"
    kb.mkdir()
    _paraphrase_kb(kb)
    db = tmp_path / "kb.db"
    cfg = emb.EmbeddingsConfig(model_name="stub", weight=1.0)
    embedder = _StubEmbedder()
    idx = Index(db, embeddings=cfg, embedder=embedder, dense_min_cosine=0.5)
    idx.reranker = idx.make_hybrid_reranker(embedder, weight=1.0)
    idx.build(kb, source_commit="c0")
    ids = [h.id for h in idx.search("car", limit=3)]
    assert "DOC-AUTO" in ids, ids
    # The banana doc is orthogonal to the "car" query (cosine 0 < 0.5), so the
    # threshold keeps it out of the dense source.
    dense = idx.dense_candidates("car", limit=5, min_cosine=0.5)
    assert {h.id for h in dense} == {"DOC-AUTO"}


def test_search_off_is_pure_fts_model_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewer concern 1 CRITICAL: with embeddings OFF (no config), search() is
    byte-for-byte pure FTS. "car" returns nothing; the dense path is never run."""
    monkeypatch.setenv("KB_EMBEDDINGS_MODE", "on")  # env on, but no Config
    kb = tmp_path / "kb"
    kb.mkdir()
    _paraphrase_kb(kb)
    db = tmp_path / "kb.db"
    idx = Index(db)  # no embeddings config -> feature inert
    idx.build(kb, source_commit="c0")
    assert idx.search("car", limit=3) == []


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


def test_schema_version_is_v11(
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
    # v11 adds the is_inbox column (issue #109).
    assert row[0] == "11"


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
    monkeypatch.delenv("KB_EMBEDDINGS_MODE", raising=False)
    kb = tmp_path / "kb"
    kb.mkdir()
    _paraphrase_kb(kb)
    db = tmp_path / "kb.db"
    cfg = emb.embeddings_config()
    Index(db, embeddings=cfg).build(kb, source_commit="c0")
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
    monkeypatch.delenv("KB_EMBEDDINGS_MODE", raising=False)
    kb = tmp_path / "kb"
    kb.mkdir()
    _paraphrase_kb(kb)
    db = tmp_path / "kb.db"
    cfg = emb.embeddings_config()
    idx = Index(db, embeddings=cfg)
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
def test_bm25_only_misses_tokenless_paraphrase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BM25-only baseline: "car" shares no token with "automobile ... vehicle",
    so a lexical-only index cannot surface DOC-AUTO. This is the gap the dense
    candidate source closes (see test_public_search_retrieves_tokenless_paraphrase)."""
    monkeypatch.delenv("KB_EMBEDDINGS_MODE", raising=False)
    kb = tmp_path / "kb"
    kb.mkdir()
    _paraphrase_kb(kb)
    db = tmp_path / "kb.db"
    idx = Index(db)  # no embeddings config: pure FTS
    idx.build(kb, source_commit="c0")
    hits = idx.search("car", limit=3)
    assert not any(h.id == "DOC-AUTO" for h in hits)


@_needs_model
def test_public_search_retrieves_tokenless_paraphrase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance test (reviewer concern 1): the PUBLIC ``Index.search("car")``
    returns the automobile doc when embeddings are enabled, even though "car"
    shares NO token with the corpus. This exercises the shipped search path (the
    dense candidate SOURCE unioned into the FTS pool + hybrid blend), not a
    hand-built pool driven through the reranker in isolation."""
    monkeypatch.delenv("KB_EMBEDDINGS_MODE", raising=False)  # prove Config, not env
    kb = tmp_path / "kb"
    kb.mkdir()
    _paraphrase_kb(kb)
    db = tmp_path / "kb.db"
    cfg = emb.EmbeddingsConfig(model_name=emb.DEFAULT_MODEL_NAME, weight=0.5)
    embedder = emb.build_embedder(cfg)
    idx = Index(db, embeddings=cfg, embedder=embedder)
    idx.reranker = idx.make_hybrid_reranker(embedder, weight=cfg.weight)
    idx.build(kb, source_commit="c0")

    hits = idx.search("car", limit=3)
    ids = [h.id for h in hits]
    # The dense source pulled DOC-AUTO into the pool and the blend ranked it.
    assert "DOC-AUTO" in ids, ids


@_needs_model
def test_public_search_negative_query_not_pulled_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dense source's min-cosine threshold protects abstention: an unrelated
    query far from every doc must NOT drag a doc in. If it did, the negative-
    stratum false-positive rate would blow up (reviewer concern 1)."""
    monkeypatch.delenv("KB_EMBEDDINGS_MODE", raising=False)
    kb = tmp_path / "kb"
    kb.mkdir()
    _paraphrase_kb(kb)
    db = tmp_path / "kb.db"
    cfg = emb.EmbeddingsConfig(model_name=emb.DEFAULT_MODEL_NAME, weight=0.5)
    embedder = emb.build_embedder(cfg)
    # A high threshold so only genuinely-similar neighbours are admitted; an
    # out-of-domain query clears it for nothing.
    idx = Index(db, embeddings=cfg, embedder=embedder, dense_min_cosine=0.9)
    idx.reranker = idx.make_hybrid_reranker(embedder, weight=cfg.weight)
    idx.build(kb, source_commit="c0")

    dense = idx.dense_candidates(
        "quantum chromodynamics lattice gauge theory",
        limit=5,
        min_cosine=0.9,
    )
    assert dense == []
