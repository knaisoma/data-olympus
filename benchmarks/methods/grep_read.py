"""Grep-then-read retrieval method.

Performs a case-insensitive substring search over all .md files under the
corpus root. Files whose text contains at least one query term (words >= 3
chars) are included in the payload. Represents a simple "grep for keywords,
read matched files" workflow.

Ranking: matched files are ordered by total query-term match count (descending;
sum of per-term occurrence counts), with the file path as a stable tiebreak.
This is the honest ranking a real "grep -c then read most-hit files first"
workflow would produce. It is a weak ranker (no idf, no length normalisation),
but it is a *real* signal derived from the query, not the alphabetical file
order the earlier version emitted (which made recall@k/NDCG/MRR meaningless for
this method). ``ranks = True`` so the harness scores it on ranking metrics.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from benchmarks.methods.base import RetrievalResult, dedupe
from data_olympus.markdown_parse import parse_file

if TYPE_CHECKING:
    from pathlib import Path

_NON_WORD = re.compile(r"\W+")


def _query_terms(query: str) -> list[str]:
    """Split query into lowercase terms of at least 3 characters."""
    return [t.lower() for t in _NON_WORD.split(query) if len(t) >= 3]


class GrepReadMethod:
    """Retrieval method that greps for query terms in document text.

    Ranks matched documents by descending total term-match count, so the
    ranking metrics measure a genuine (if crude) grep-hit-count ordering rather
    than filename order.
    """

    name = "grep-read"
    ranks = True  # match-count order is a real ranking signal

    def __init__(self, root: Path) -> None:
        self._root = root

    def retrieve(self, query: str) -> RetrievalResult:
        terms = _query_terms(query)
        # (match_count, path_str, doc_id, content) per matched file.
        matched: list[tuple[int, str, str, str]] = []
        for md in sorted(self._root.rglob("*.md")):
            content = md.read_text(encoding="utf-8")
            lower = content.lower()
            if not terms:
                continue
            count = sum(lower.count(term) for term in terms)
            if count > 0:
                doc = parse_file(md)
                if doc.id:
                    matched.append((count, str(md), doc.id, content))
        # Highest match count first; stable tiebreak on path for determinism.
        matched.sort(key=lambda x: (-x[0], x[1]))
        ids = [doc_id for _, _, doc_id, _ in matched]
        texts = [content for _, _, _, content in matched]
        ranked = dedupe(ids)
        return RetrievalResult(
            payload_text="\n".join(texts),
            ranked_ids=ranked,
            retrieved_ids=set(ranked),
        )
