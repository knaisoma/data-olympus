"""Whole-bundle dump retrieval method.

Reads every .md file under the corpus root, concatenates all bodies, and
returns all concept ids. This is the maximum-token, maximum-recall baseline
that represents loading the entire knowledge bundle into context.

It performs NO query-dependent ranking: the returned id order is the
(alphabetical) file-walk order, identical for every query. Scoring an unranked
dump on recall@k/NDCG/MRR is meaningless (the "ranking" ignores the query
entirely), so ``ranks = False`` and the harness reports whole-dump only on the
axes where it is honest: token cost, and whether the payload *contains* the gold
concept at all (contains-gold / set recall). By construction it always contains
every gold concept, so contains-gold is 1.0; the point it makes is the token
price of that guaranteed containment.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from benchmarks.methods.base import RetrievalResult, dedupe
from data_olympus.markdown_parse import parse_file

if TYPE_CHECKING:
    from pathlib import Path


class WholeDumpMethod:
    """Retrieval method that returns every document in the corpus (no ranking)."""

    name = "whole-dump"
    ranks = False  # id order is query-independent; ranking metrics are undefined

    def __init__(self, root: Path) -> None:
        self._root = root

    def retrieve(self, query: str) -> RetrievalResult:  # noqa: ARG002
        texts: list[str] = []
        ids: list[str] = []
        for md in sorted(self._root.rglob("*.md")):
            doc = parse_file(md)
            if doc.id:
                ids.append(doc.id)
                texts.append(md.read_text(encoding="utf-8"))
        ranked = dedupe(ids)
        return RetrievalResult(
            payload_text="\n".join(texts),
            ranked_ids=ranked,
            retrieved_ids=set(ranked),
        )
