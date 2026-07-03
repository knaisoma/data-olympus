"""Optional vector-RAG retrieval method.

Requires `pip install -e '.[bench]'` (sentence-transformers + numpy).
Lazy-imported at __init__ time; raises a clear RuntimeError when missing.

Honesty: this method has access to dense semantic similarity that the
data-olympus Index does not currently use. It is expected to win on
semantic-paraphrase queries and lose on staleness awareness.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from benchmarks.chunking import chunk_text
from benchmarks.methods.base import RetrievalResult, dedupe

if TYPE_CHECKING:
    from pathlib import Path

    import numpy as np
    from sentence_transformers import SentenceTransformer


def _parse_id(text: str) -> str | None:
    """Extract 'id:' from frontmatter, or None if not found."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("id:"):
            return stripped[3:].strip()
    return None


def _strip_frontmatter(raw: str) -> str:
    """Remove YAML frontmatter fences from markdown text."""
    if raw.startswith("---"):
        end = raw.find("---", 3)
        if end != -1:
            return raw[end + 3:].strip()
    return raw


class VectorRagMethod:
    """Retrieval via dense semantic similarity (sentence-transformers).

    Chunks all corpus documents, embeds them with a pinned MiniLM model,
    and ranks by cosine similarity at query time.
    """

    name = "vector-rag"
    ranks = True  # cosine-similarity order is a real ranking signal

    def __init__(self, corpus_root: Path, k: int = 5) -> None:
        try:
            import numpy as _np
            from sentence_transformers import SentenceTransformer as _ST
        except ImportError as exc:
            raise RuntimeError(
                "vector_rag requires `pip install -e '.[bench]'` "
                "(sentence-transformers and numpy must be installed)"
            ) from exc

        self._np = _np
        self._k = k

        # Build the chunk index from all .md files in corpus_root.
        all_chunk_texts: list[str] = []
        all_chunk_doc_ids: list[str] = []

        for md_file in sorted(corpus_root.rglob("*.md")):
            raw = md_file.read_text(encoding="utf-8")
            doc_id = _parse_id(raw)
            if doc_id is None:
                continue
            body = _strip_frontmatter(raw)
            if not body:
                body = raw

            chunks = chunk_text(body, size=512, overlap=64)
            if not chunks:
                chunks = [body]
            for ch in chunks:
                all_chunk_texts.append(ch)
                all_chunk_doc_ids.append(doc_id)

        self._chunk_texts: list[str] = all_chunk_texts
        self._chunk_doc_ids: list[str] = all_chunk_doc_ids

        model: SentenceTransformer = _ST("sentence-transformers/all-MiniLM-L6-v2")
        embeddings: np.ndarray = model.encode(
            all_chunk_texts, convert_to_numpy=True, show_progress_bar=False
        )
        # Normalise rows for cosine similarity via dot product.
        norms = _np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms = _np.where(norms == 0, 1.0, norms)
        self._embeddings: np.ndarray = embeddings / norms
        self._model = model

    def retrieve(self, query: str) -> RetrievalResult:
        q_emb: np.ndarray = self._model.encode(
            [query], convert_to_numpy=True, show_progress_bar=False
        )[0]
        norm = self._np.linalg.norm(q_emb)
        if norm > 0:
            q_emb = q_emb / norm

        scores: np.ndarray = self._embeddings @ q_emb
        # Argsort descending.
        order = self._np.argsort(-scores).tolist()

        top_chunk_texts: list[str] = []
        ranked_doc_ids: list[str] = []

        for idx in order:
            doc_id = self._chunk_doc_ids[idx]
            ranked_doc_ids.append(doc_id)
            if len(top_chunk_texts) < self._k:
                top_chunk_texts.append(self._chunk_texts[idx])

        payload = "\n\n".join(top_chunk_texts)
        ranked_ids = dedupe(ranked_doc_ids)

        return RetrievalResult(
            payload_text=payload,
            ranked_ids=ranked_ids[: self._k],
            retrieved_ids=set(ranked_ids),
        )
