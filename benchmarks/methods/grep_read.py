"""Grep-then-read retrieval method.

Performs a case-insensitive substring search over all .md files under the
corpus root. Files whose text contains at least one query term (words >= 3
chars) are included in the payload. Represents a simple "grep for keywords,
read matched files" workflow.
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
    """Retrieval method that greps for query terms in document text."""

    name = "grep-read"

    def __init__(self, root: Path) -> None:
        self._root = root

    def retrieve(self, query: str) -> RetrievalResult:
        terms = _query_terms(query)
        texts: list[str] = []
        ids: list[str] = []
        for md in sorted(self._root.rglob("*.md")):
            content = md.read_text(encoding="utf-8")
            lower = content.lower()
            if terms and any(term in lower for term in terms):
                doc = parse_file(md)
                if doc.id:
                    ids.append(doc.id)
                    texts.append(content)
        ranked = dedupe(ids)
        return RetrievalResult(
            payload_text="\n".join(texts),
            ranked_ids=ranked,
            retrieved_ids=set(ranked),
        )
