"""Optional local-embedding hybrid ranking (issue #42).

The feature is OFF by default (``KB_EMBEDDINGS_MODE`` unset) so the zero-
dependency lexical product is unchanged and the embedding libraries are never
imported. When enabled, each doc is embedded at ``Index.build`` time into a new
``doc_vectors`` table (schema v8) written into the same tmp DB and swapped
atomically; a hybrid reranker then blends NORMALISED bm25 with cosine similarity
over the candidate hits' stored vectors.

Tests split into two groups:

- Pure-logic tests (no model download): vector (de)serialisation, cosine, the
  hybrid blend math, config gating, and the loud-failure-when-misconfigured
  contract. These always run.
- Model-backed integration tests: build vectors and beat BM25 on a paraphrase
  query. These require the ``embeddings`` extra AND a downloadable/cached model,
  so they SKIP when the model/dep is unavailable rather than making the suite
  flaky (issue #42 acceptance allows this).
"""
from __future__ import annotations

import math

import pytest

from data_olympus import embeddings as emb
from data_olympus.index import SearchHit

# --- config gating -----------------------------------------------------------


def test_embeddings_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KB_EMBEDDINGS_MODE", raising=False)
    assert emb.embeddings_enabled() is False


@pytest.mark.parametrize("value", ["on", "On", "ON"])
def test_embeddings_enabled_when_on(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("KB_EMBEDDINGS_MODE", value)
    assert emb.embeddings_enabled() is True


@pytest.mark.parametrize("value", ["off", "", "0", "false", "yes"])
def test_embeddings_off_for_other_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("KB_EMBEDDINGS_MODE", value)
    assert emb.embeddings_enabled() is False


def test_embeddings_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KB_EMBEDDINGS_MODEL", raising=False)
    monkeypatch.delenv("KB_EMBEDDINGS_WEIGHT", raising=False)
    cfg = emb.embeddings_config()
    assert cfg.model_name == emb.DEFAULT_MODEL_NAME
    assert cfg.weight == emb.DEFAULT_WEIGHT


def test_embeddings_config_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_EMBEDDINGS_MODEL", "some/other-model")
    monkeypatch.setenv("KB_EMBEDDINGS_WEIGHT", "0.75")
    cfg = emb.embeddings_config()
    assert cfg.model_name == "some/other-model"
    assert cfg.weight == 0.75


@pytest.mark.parametrize("bad", ["-0.1", "1.5", "abc"])
def test_embeddings_weight_out_of_range_raises(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv("KB_EMBEDDINGS_WEIGHT", bad)
    with pytest.raises(ValueError):
        emb.embeddings_config()


# --- vector (de)serialisation ------------------------------------------------


def test_vector_roundtrip() -> None:
    v = [0.1, -0.2, 0.3, 0.0, 1.5]
    blob = emb.serialize_vector(v)
    assert isinstance(blob, bytes)
    out = emb.deserialize_vector(blob)
    assert len(out) == len(v)
    for a, b in zip(v, out, strict=True):
        assert a == pytest.approx(b, abs=1e-6)


def test_empty_vector_roundtrip() -> None:
    assert emb.deserialize_vector(emb.serialize_vector([])) == []


# --- cosine similarity -------------------------------------------------------


def test_cosine_identical_is_one() -> None:
    v = [1.0, 2.0, 3.0]
    assert emb.cosine(v, v) == pytest.approx(1.0)


def test_cosine_orthogonal_is_zero() -> None:
    assert emb.cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_opposite_is_minus_one() -> None:
    assert emb.cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_zero_vector_is_zero() -> None:
    # A zero-norm vector cannot have a direction; define similarity as 0 rather
    # than dividing by zero.
    assert emb.cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cosine_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        emb.cosine([1.0, 2.0], [1.0, 2.0, 3.0])


# --- hybrid reranker (pure math, no model) -----------------------------------


def _hit(doc_id: str, score: float) -> SearchHit:
    return SearchHit(id=doc_id, path=f"{doc_id}.md", title=doc_id, snippet="", score=score)


def test_hybrid_reranker_promotes_semantic_match() -> None:
    """A doc that BM25 ranks second but is semantically closest to the query is
    lifted to the top when the blend weight favours cosine."""
    # BM25: A best (-5.0), B worse (-1.0). Lower is better.
    hits = [_hit("A", -5.0), _hit("B", -1.0)]
    # Query embeds close to B, far from A.
    query_vec = [1.0, 0.0]
    vectors = {"A": [0.0, 1.0], "B": [1.0, 0.0]}
    reranker = emb.make_hybrid_reranker(
        embed_query=lambda _q: query_vec,
        get_vector=vectors.get,
        weight=1.0,  # pure cosine
    )
    out = reranker("anything", hits)
    assert [h.id for h in out] == ["B", "A"]


def test_hybrid_reranker_weight_zero_preserves_bm25_order() -> None:
    hits = [_hit("A", -5.0), _hit("B", -1.0)]
    reranker = emb.make_hybrid_reranker(
        embed_query=lambda _q: [1.0, 0.0],
        get_vector={"A": [0.0, 1.0], "B": [1.0, 0.0]}.get,
        weight=0.0,  # pure bm25
    )
    out = reranker("anything", hits)
    assert [h.id for h in out] == ["A", "B"]


def test_hybrid_reranker_missing_vector_uses_bm25_only_component() -> None:
    """A hit whose doc has no stored vector must not be dropped; it keeps its
    bm25 contribution and a neutral (zero) cosine contribution."""
    hits = [_hit("A", -5.0), _hit("B", -1.0)]
    reranker = emb.make_hybrid_reranker(
        embed_query=lambda _q: [1.0, 0.0],
        get_vector={"A": [1.0, 0.0]}.get,  # B missing
        weight=0.5,
    )
    out = reranker("anything", hits)
    assert {h.id for h in out} == {"A", "B"}
    assert len(out) == 2


def test_hybrid_reranker_empty_hits() -> None:
    reranker = emb.make_hybrid_reranker(
        embed_query=lambda _q: [1.0, 0.0], get_vector={}.get, weight=0.5
    )
    assert reranker("q", []) == []


def test_hybrid_reranker_single_hit_no_normalisation_error() -> None:
    """A single hit has zero bm25 spread; normalisation must not divide by zero."""
    hits = [_hit("A", -3.0)]
    reranker = emb.make_hybrid_reranker(
        embed_query=lambda _q: [1.0, 0.0],
        get_vector={"A": [1.0, 0.0]}.get,
        weight=0.5,
    )
    out = reranker("q", hits)
    assert [h.id for h in out] == ["A"]


def test_hybrid_reranker_embed_query_none_is_passthrough() -> None:
    """If the query cannot be embedded (embedder returned None), fall back to the
    incoming bm25 order rather than raising."""
    hits = [_hit("A", -5.0), _hit("B", -1.0)]
    reranker = emb.make_hybrid_reranker(
        embed_query=lambda _q: None,
        get_vector={"A": [0.0, 1.0], "B": [1.0, 0.0]}.get,
        weight=1.0,
    )
    out = reranker("q", hits)
    assert [h.id for h in out] == ["A", "B"]


# --- loud failure when enabled but unavailable -------------------------------


def test_require_embedder_raises_clear_message_when_dep_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the feature is enabled but fastembed cannot be imported, startup must
    fail loudly with an actionable message (not silently fall back)."""

    def _boom(*_a: object, **_k: object) -> object:
        raise ImportError("No module named 'fastembed'")

    monkeypatch.setattr(emb, "_load_text_embedding_cls", _boom)
    with pytest.raises(emb.EmbeddingsUnavailableError) as ei:
        emb.build_embedder(emb.EmbeddingsConfig(model_name="x", weight=0.5))
    msg = str(ei.value)
    assert "embeddings" in msg.lower()
    assert "install" in msg.lower() or "extra" in msg.lower()


# --- schema constant ---------------------------------------------------------


def test_schema_defines_doc_vectors_table() -> None:
    assert "doc_vectors" in emb.EMBEDDINGS_SCHEMA
    assert "vector" in emb.EMBEDDINGS_SCHEMA


# --- helper for the model-gated integration tests ----------------------------


def _model_available() -> bool:
    """True when fastembed imports AND a model can be loaded/cached locally.

    Kept cheap: import guard first, then a single tiny embedding call. Any
    failure (missing dep, no network for the one-time model fetch) returns False
    so the integration tests SKIP rather than fail.
    """
    try:
        cfg = emb.EmbeddingsConfig(
            model_name=emb.DEFAULT_MODEL_NAME, weight=emb.DEFAULT_WEIGHT
        )
        embedder = emb.build_embedder(cfg)
        vec = embedder.embed_one("smoke test")
    except Exception:
        return False
    return bool(vec) and len(vec) > 0


_MODEL = _model_available()
_needs_model = pytest.mark.skipif(
    not _MODEL, reason="embeddings model/dep unavailable (no fastembed or no model fetch)"
)


@_needs_model
def test_embed_one_returns_unit_ish_vector() -> None:
    cfg = emb.embeddings_config()
    embedder = emb.build_embedder(cfg)
    v = embedder.embed_one("kubernetes deployment rollout")
    assert len(v) > 0
    # bge models emit normalised vectors; norm ~= 1.
    norm = math.sqrt(sum(x * x for x in v))
    assert norm == pytest.approx(1.0, abs=0.05)
