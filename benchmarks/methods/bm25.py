"""BM25 retrieval method (dependency-free).

Chunks every document on init, builds an inverted BM25 index over the chunks,
and scores them at query time. Uses whitespace-lowercased terms throughout.
Represents a classic keyword-retrieval baseline without dense embeddings.

BM25 parameters: k1=1.5, b=0.75 (standard Robertson et al. defaults).

Two variants ship:
- ``Bm25Method``: plain BM25 over every doc, status-blind (the classic keyword
  baseline). It has NO notion of lifecycle, so it can and does rank a superseded
  doc first on lifecycle queries.
- ``StatusAwareBm25Method``: identical BM25 ranker, but it reads each doc's
  ``status`` frontmatter and drops superseded/deprecated docs before indexing.
  This isolates "the win comes from having governance metadata (status)" from
  "the engine is a better ranker": it is a keyword baseline that *also* has the
  metadata, so any residual data-olympus advantage over it is not attributable
  to the status filter alone.
"""
from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING

from benchmarks.chunking import chunk_text
from benchmarks.methods.base import RetrievalResult, dedupe
from data_olympus.markdown_parse import parse_file

if TYPE_CHECKING:
    from pathlib import Path

    from data_olympus.markdown_parse import ParsedDoc

# Status values excluded by the status-aware baseline (mirrors the engine's
# out-of-force classes: a superseded or deprecated doc must not be served).
_OUT_OF_FORCE_STATUSES = frozenset({"superseded", "deprecated"})

_CHUNK_SIZE = 512
_CHUNK_OVERLAP = 64
_K1 = 1.5
_B = 0.75
_NON_WORD = re.compile(r"\W+")


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _NON_WORD.split(text) if t]


class Bm25Method:
    """Retrieval method using BM25 over chunked corpus (no external deps)."""

    name = "bm25"
    ranks = True  # bm25 score order is a real ranking signal

    def __init__(self, root: Path, k: int = 5) -> None:
        self._k = k
        # (doc_id, chunk_text, term_freqs, chunk_len)
        self._chunks: list[tuple[str, str, dict[str, int], int]] = []
        self._df: dict[str, int] = {}  # document (chunk) frequency per term

        for md in sorted(root.rglob("*.md")):
            doc = parse_file(md)
            if not doc.id:
                continue
            if not self._include_doc(doc):
                continue
            body = md.read_text(encoding="utf-8")
            for chunk in chunk_text(body, size=_CHUNK_SIZE, overlap=_CHUNK_OVERLAP):
                terms = _tokenize(chunk)
                tf: dict[str, int] = {}
                for t in terms:
                    tf[t] = tf.get(t, 0) + 1
                self._chunks.append((doc.id, chunk, tf, len(terms)))
                for t in set(terms):
                    self._df[t] = self._df.get(t, 0) + 1

        self._n = len(self._chunks)
        self._avgdl = (
            sum(c[3] for c in self._chunks) / self._n if self._n else 1.0
        )

    def _include_doc(self, doc: ParsedDoc) -> bool:  # noqa: ARG002
        """Whether to index ``doc``. Base class indexes everything (status-blind)."""
        return True

    def _score(self, query_terms: list[str], tf: dict[str, int], chunk_len: int) -> float:
        score = 0.0
        for term in set(query_terms):
            if term not in tf:
                continue
            df = self._df.get(term, 0)
            idf = math.log((self._n - df + 0.5) / (df + 0.5) + 1.0)
            f = tf[term]
            denom = f + _K1 * (1 - _B + _B * chunk_len / self._avgdl)
            score += idf * (f * (_K1 + 1)) / denom
        return score

    def retrieve(self, query: str) -> RetrievalResult:
        query_terms = _tokenize(query)
        scored: list[tuple[float, str, str]] = []  # (score, doc_id, chunk_text)
        for doc_id, chunk, tf, chunk_len in self._chunks:
            s = self._score(query_terms, tf, chunk_len)
            if s > 0:
                scored.append((s, doc_id, chunk))
        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[: self._k]
        ranked = dedupe([doc_id for _, doc_id, _ in top])
        payload = "\n".join(chunk for _, _, chunk in top)
        return RetrievalResult(
            payload_text=payload,
            ranked_ids=ranked,
            retrieved_ids=set(ranked),
        )


class StatusAwareBm25Method(Bm25Method):
    """BM25 that reads ``status`` frontmatter and skips out-of-force docs.

    Same ranker as :class:`Bm25Method`; the only difference is that superseded /
    deprecated docs are dropped before indexing. This is the metadata-aware
    keyword baseline: it isolates the value of *having* governance status from
    the value of the retrieval engine. A superseded doc it never indexed can
    never be ranked, so its lifecycle staleness rate should be 0.000 like
    data-olympus, but it still lacks the outline/snippet payload shaping and the
    in-force filter's compositional behaviour on the FTS surface.
    """

    name = "bm25-status-aware"
    ranks = True

    def _include_doc(self, doc: ParsedDoc) -> bool:
        return doc.status.strip().lower() not in _OUT_OF_FORCE_STATUSES
