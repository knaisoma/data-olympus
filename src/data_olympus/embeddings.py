"""Optional local-embedding hybrid ranking (issue #42).

Real paraphrase / synonymy handling via LOCAL embeddings, blended with BM25. No
external API is ever called at query time; a small ONNX MiniLM-class model runs
in-process. The feature is OFF by default and, when off, this module is the ONLY
place that would touch the embedding libraries, and it imports them LAZILY
(inside :func:`_load_text_embedding_cls`), so with the ``embeddings`` extra NOT
installed and ``KB_EMBEDDINGS_MODE`` unset the default product is byte-for-byte
unchanged and never imports ``fastembed`` / ``onnxruntime``.

Design (mirrors ``trigram.py`` / ``cooccurrence.py``):

- ``EMBEDDINGS_SCHEMA``: the ``doc_vectors`` table DDL, appended to the index
  schema so it is created (and, via the tmp-DB build + ``os.replace``, swapped)
  atomically with the primary FTS table. The table is created unconditionally
  (an empty table costs nothing) but only POPULATED when the feature is enabled,
  so a default build needs no embedding dependency.
- ``embeddings_enabled`` / ``embeddings_config``: env config. ``KB_EMBEDDINGS_MODE``
  gates the whole feature (default off); ``KB_EMBEDDINGS_MODEL`` and
  ``KB_EMBEDDINGS_WEIGHT`` tune the model and blend weight.
- ``build_embedder``: construct the local embedder, raising a LOUD, actionable
  :class:`EmbeddingsUnavailableError` when the dep/model is missing rather than
  silently falling back (which would hide misconfiguration).
- ``serialize_vector`` / ``deserialize_vector`` / ``cosine``: pure-Python vector
  storage + similarity, so the query-time blend needs no numpy.
- ``make_hybrid_reranker``: blend NORMALISED bm25 with cosine over candidate
  hits' stored vectors and re-sort. Composed as an INNER reranker under the
  id/tag short-circuit in ``server.build_app`` so an exact id/tag still wins.

Ranking discipline: the hybrid reranker only RE-ORDERS the existing candidate
pool; it never drops a hit (a doc with no stored vector keeps its bm25
contribution and a neutral cosine of 0). Because it is composed as ``inner``
under the id/tag short-circuit, an exact-id or exact-tag query is unaffected.
"""
from __future__ import annotations

import array
import math
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from data_olympus.index import SearchHit


# --- config ------------------------------------------------------------------

# A small, local, ONNX MiniLM-class model. bge-small-en-v1.5 is 384-dim, emits
# L2-normalised vectors, and ships as a quantised ONNX file (~33 MB) that
# fastembed fetches once and caches; it needs no torch. A deployment can point
# ``KB_EMBEDDINGS_MODEL`` at any fastembed-supported model.
DEFAULT_MODEL_NAME = "BAAI/bge-small-en-v1.5"

# Blend weight in [0, 1]: the fraction of the final score contributed by cosine
# similarity; ``1 - weight`` is contributed by normalised bm25. 0.0 == pure
# lexical (bm25) ordering, 1.0 == pure semantic (cosine). The default leans on
# bm25 (the tuned lexical signal) while letting cosine rescue paraphrase misses.
DEFAULT_WEIGHT = 0.35


@dataclass(frozen=True, slots=True)
class EmbeddingsConfig:
    """Resolved embeddings configuration (model + blend weight)."""

    model_name: str
    weight: float


class EmbeddingsUnavailableError(RuntimeError):
    """Raised when embeddings are ENABLED but the dep/model cannot be loaded.

    Carries an actionable message so a misconfigured deployment fails loudly at
    startup instead of silently shipping default (lexical-only) ranking.
    """


def embeddings_enabled() -> bool:
    """Whether local-embedding hybrid ranking is active. Default OFF.

    ``KB_EMBEDDINGS_MODE=on`` (case-insensitive) enables it; any other value
    (including unset) leaves it off so the zero-dependency lexical product is
    unchanged and the embedding libraries are never imported.
    """
    return os.getenv("KB_EMBEDDINGS_MODE", "off").strip().lower() == "on"


def embeddings_config() -> EmbeddingsConfig:
    """Resolve the embeddings config from env, applying defaults.

    ``KB_EMBEDDINGS_WEIGHT`` must parse to a float in [0, 1]; a malformed or
    out-of-range value raises ``ValueError`` rather than silently clamping, so a
    misconfigured blend fails loudly (matching ``KB_STATUS_WEIGHTS`` semantics).
    """
    model_name = os.getenv("KB_EMBEDDINGS_MODEL", "").strip() or DEFAULT_MODEL_NAME
    raw_weight = os.getenv("KB_EMBEDDINGS_WEIGHT", "").strip()
    if not raw_weight:
        weight = DEFAULT_WEIGHT
    else:
        try:
            weight = float(raw_weight)
        except ValueError as e:
            raise ValueError(
                f"KB_EMBEDDINGS_WEIGHT must be a float in [0, 1]; got {raw_weight!r}"
            ) from e
        if not 0.0 <= weight <= 1.0:
            raise ValueError(
                f"KB_EMBEDDINGS_WEIGHT must be in [0, 1]; got {weight}"
            )
    return EmbeddingsConfig(model_name=model_name, weight=weight)


# --- vector storage + similarity (pure Python, no numpy) ---------------------


def serialize_vector(vec: list[float]) -> bytes:
    """Pack a float vector into a compact little-endian float32 blob.

    float32 halves the stored size versus float64 at a precision loss well below
    the ranking signal. ``array`` keeps this dependency-free; the blob round-trips
    through :func:`deserialize_vector`.
    """
    return array.array("f", vec).tobytes()


def deserialize_vector(blob: bytes) -> list[float]:
    """Unpack a float32 blob written by :func:`serialize_vector`."""
    a = array.array("f")
    a.frombytes(blob)
    return list(a)


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors, in [-1, 1].

    A zero-norm vector has no direction, so its similarity is defined as 0 rather
    than dividing by zero. A length mismatch is a programming error and raises.
    """
    if len(a) != len(b):
        raise ValueError(f"vector length mismatch: {len(a)} != {len(b)}")
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


# --- embedder (lazy fastembed import) ----------------------------------------


def _load_text_embedding_cls() -> Any:
    """Import and return ``fastembed.TextEmbedding``.

    Isolated so it can be monkeypatched in tests and so the import stays LAZY:
    it runs only from :func:`build_embedder`, which is only reached when the
    feature is enabled. With the feature off this line is never executed and
    ``fastembed`` is never imported.
    """
    from fastembed import TextEmbedding

    return TextEmbedding


class Embedder:
    """Thin wrapper over a local fastembed model.

    Holds the loaded model and exposes ``embed_one`` (query) and ``embed_many``
    (build) returning plain ``list[float]`` so the rest of the codebase stays
    numpy-free. Construction is via :func:`build_embedder`.
    """

    def __init__(self, model: Any, *, model_name: str) -> None:
        self._model = model
        self.model_name = model_name

    def embed_one(self, text: str) -> list[float]:
        """Embed a single text into a plain float list."""
        for vec in self._model.embed([text]):
            return [float(x) for x in vec]
        return []

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch, preserving input order."""
        return [[float(x) for x in vec] for vec in self._model.embed(texts)]


def build_embedder(config: EmbeddingsConfig) -> Embedder:
    """Load the local embedding model, or fail loudly.

    Raises :class:`EmbeddingsUnavailableError` with an actionable message when
    the ``embeddings`` extra is not installed or the model cannot be loaded
    (e.g. it could not be fetched to the local cache). The caller (startup path)
    surfaces this so a misconfigured deployment fails visibly rather than
    silently reverting to lexical-only ranking.
    """
    try:
        text_embedding_cls = _load_text_embedding_cls()
    except ImportError as e:
        raise EmbeddingsUnavailableError(
            "KB_EMBEDDINGS_MODE is on but the embeddings dependency is not "
            "installed. Install the extra: `uv sync --extra embeddings` (or "
            "`pip install 'data-olympus[embeddings]'`), or set KB_EMBEDDINGS_MODE=off "
            f"to run lexical-only. Underlying import error: {e}"
        ) from e
    try:
        model = text_embedding_cls(model_name=config.model_name)
    except Exception as e:  # noqa: BLE001 - any load failure must fail loudly
        raise EmbeddingsUnavailableError(
            f"KB_EMBEDDINGS_MODE is on but the embedding model "
            f"{config.model_name!r} could not be loaded (it is fetched once and "
            f"cached; a first-run needs the model available). Set a cached "
            f"KB_EMBEDDINGS_MODEL or KB_EMBEDDINGS_MODE=off. Underlying error: {e}"
        ) from e
    return Embedder(model, model_name=config.model_name)


# --- SQLite persistence (built inside Index.build, read at query time) -------

# One row per doc: the id (FK to docs.id) and its float32 vector blob. Created
# unconditionally in the schema (empty when the feature is off) so a default
# build needs no embedding dep; populated only when embeddings are enabled.
EMBEDDINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS doc_vectors (
    id TEXT PRIMARY KEY,
    vector BLOB NOT NULL
);
"""


# --- hybrid reranker ---------------------------------------------------------


def _normalise_bm25(hits: list[SearchHit]) -> dict[str, float]:
    """Map each hit id to a bm25 GOODNESS in [0, 1] (1 == best in this pool).

    search() orders bm25 ASCENDING (lower is better), so we invert: the minimum
    (best) score maps to 1.0 and the maximum (worst) to 0.0. When all scores are
    equal (single hit, or a synthetic neutral pool) every hit gets 1.0, so the
    blend is decided purely by cosine, and there is no divide-by-zero.
    """
    if not hits:
        return {}
    scores = [h.score for h in hits]
    lo = min(scores)
    hi = max(scores)
    spread = hi - lo
    if spread == 0.0:
        return {h.id: 1.0 for h in hits}
    # goodness = 1 when score == lo (best), 0 when score == hi (worst).
    return {h.id: (hi - h.score) / spread for h in hits}


def make_hybrid_reranker(
    *,
    embed_query: Callable[[str], list[float] | None],
    get_vector: Callable[[str], list[float] | None],
    weight: float,
) -> Callable[[str, list[SearchHit]], list[SearchHit]]:
    """Build a reranker that blends normalised bm25 with query-doc cosine.

    ``embed_query(query)`` returns the query vector (or ``None`` if it cannot be
    embedded); ``get_vector(id)`` returns a stored doc vector (or ``None`` when
    the doc has no vector). ``weight`` in [0, 1] is the cosine fraction of the
    blended score (``1 - weight`` is bm25).

    The blended score is::

        blended = (1 - weight) * bm25_goodness + weight * cosine_component

    where ``bm25_goodness`` is in [0, 1] (higher = better within this pool) and
    ``cosine_component`` maps cosine [-1, 1] to [0, 1]. Hits are re-sorted by
    blended score DESCENDING (higher = better) and the result is re-scored onto
    the search()-native ascending-bm25 convention (negated blended score) so a
    later stage that sorts ascending keeps the same order.

    Discipline: no hit is dropped. A doc with no stored vector, or a query that
    cannot be embedded, contributes a neutral cosine (the reranker degrades to
    bm25 ordering for that case). The re-sort is stable for equal blended scores.
    """

    def reranker(query: str, hits: list[SearchHit]) -> list[SearchHit]:
        if not hits:
            return hits
        from dataclasses import replace

        qvec = embed_query(query)
        bm25_goodness = _normalise_bm25(hits)

        def blended(h: SearchHit) -> float:
            good = bm25_goodness.get(h.id, 0.0)
            cos_component = 0.0
            if qvec is not None:
                dvec = get_vector(h.id)
                if dvec is not None and len(dvec) == len(qvec):
                    # Map cosine [-1, 1] -> [0, 1].
                    cos_component = (cosine(qvec, dvec) + 1.0) / 2.0
            return (1.0 - weight) * good + weight * cos_component

        scored = [(blended(h), i, h) for i, h in enumerate(hits)]
        # Sort by rank_class FIRST (primaries before backfill), then blended DESC;
        # ties keep incoming order via the enumerate index. The rank-class outer
        # key means the cosine blend can re-order WITHIN a class but never lift a
        # backfill (expansion/trigram) hit above a primary (finding (d)): the
        # blend re-normalises scores, so a score-only floor would not hold.
        scored.sort(key=lambda t: (t[2].rank_class, -t[0], t[1]))
        # Re-score onto the ascending-bm25 convention (lower == better) so a
        # downstream ascending sort agrees with this order: negate blended.
        return [replace(h, score=-b) for b, _i, h in scored]

    return reranker
