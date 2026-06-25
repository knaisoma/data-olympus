"""Retrieval metrics. Pure functions, dependency-free, deterministic.

`ranked` is the method's ranked list of concept ids (best first). `gold` is the
set of concept ids that correctly answer the query.
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


def recall_at_k(ranked: Sequence[str], gold: set[str], *, k: int) -> float:
    if not gold:
        return 0.0
    top = set(ranked[:k])
    return len(top & gold) / len(gold)


def precision_signal(*, payload_tokens: int, gold_tokens: int, gold_retrieved: bool) -> float:
    """Signal-to-noise: share of the payload that is the relevant concept.

    1.0 means the payload is essentially just the answer; tiny means the answer
    is buried in a large payload (e.g. whole-bundle dump). 0.0 if the gold
    concept was not retrieved at all.
    """
    if not gold_retrieved or payload_tokens <= 0:
        return 0.0
    return min(1.0, gold_tokens / payload_tokens)


def mrr(ranked: Sequence[str], gold: set[str]) -> float:
    for i, doc_id in enumerate(ranked, start=1):
        if doc_id in gold:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranked: Sequence[str], gold: set[str], *, k: int) -> float:
    dcg = 0.0
    for i, doc_id in enumerate(ranked[:k], start=1):
        if doc_id in gold:
            dcg += 1.0 / math.log2(i + 1)
    ideal_hits = min(len(gold), k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    return dcg / idcg if idcg > 0 else 0.0


def staleness_error(ranked: Sequence[str], *, current_id: str, stale_id: str) -> int:
    """1 if the method surfaces the superseded concept at or above the current
    one (or surfaces stale while current is absent), else 0."""
    inf = len(ranked) + 1
    pos_current = ranked.index(current_id) if current_id in ranked else inf
    pos_stale = ranked.index(stale_id) if stale_id in ranked else inf
    if pos_stale == inf:
        return 0
    return 1 if pos_stale <= pos_current else 0


def governance_miss_rate(
    ranked_per_query: list[Sequence[str]], gold_per_query: list[set[str]], *, k: int
) -> float:
    """Fraction of queries with NO gold concept in the top-k. The headline
    governance failure: the agent gets no governing rule."""
    if not ranked_per_query:
        return 0.0
    misses = 0
    for ranked, gold in zip(ranked_per_query, gold_per_query, strict=True):
        if not gold or not (set(ranked[:k]) & gold):
            misses += 1
    return misses / len(ranked_per_query)


def false_positive_rate(retrieved_counts_on_negatives: list[int]) -> float:
    """For negative queries (no governing rule exists), the fraction that
    returned anything at all. A governance tool should abstain on these."""
    if not retrieved_counts_on_negatives:
        return 0.0
    return sum(1 for c in retrieved_counts_on_negatives if c > 0) / len(
        retrieved_counts_on_negatives
    )
