"""Retrieval metrics. Pure functions, dependency-free, deterministic.

`ranked` is the method's ranked list of concept ids (best first). `gold` is the
set of concept ids that correctly answer the query.
"""
from __future__ import annotations

import math
import random
import statistics
from typing import TYPE_CHECKING, NamedTuple

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


def serves_stale(ranked: Sequence[str], *, stale_id: str, k: int) -> int:
    """1 if the superseded concept appears ANYWHERE in the top-k payload, else 0.

    This is the unambiguous governance-harm metric: a method "serves stale" if a
    superseded rule reaches the agent at all, regardless of its rank relative to
    the current one. It is stricter and less rank-tiebreak-sensitive than
    ``staleness_error``: when the current and superseded docs are lexically
    identical (as in the de-leaked corpus), a status-blind ranker retrieves BOTH
    and thus serves the stale one even if the current happens to sort first, so
    ``serves_stale`` catches the failure that ``staleness_error`` can miss on a
    lucky tiebreak. A retriever with a status/in-force filter excludes the
    superseded doc before ranking, so it scores 0 by construction."""
    return 1 if stale_id in set(ranked[:k]) else 0


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


# ---------------------------------------------------------------------------
# Bootstrap confidence intervals
# ---------------------------------------------------------------------------

# Default resample count and seed. Fixed so the reported CI is reproducible:
# the SAME sample yields the SAME interval on every run.
BOOTSTRAP_ITERATIONS = 2000
BOOTSTRAP_SEED = 12345


class MeanCI(NamedTuple):
    """A mean estimate with a bootstrap confidence interval.

    ``lo``/``hi`` are the percentile bounds of the resampled-mean distribution.
    For a degenerate sample (n<=1) the interval collapses to the mean itself.
    """

    mean: float
    lo: float
    hi: float
    n: int


def bootstrap_mean_ci(
    values: Sequence[float],
    *,
    iterations: int = BOOTSTRAP_ITERATIONS,
    confidence: float = 0.95,
    seed: int = BOOTSTRAP_SEED,
) -> MeanCI:
    """Percentile bootstrap CI for the mean of ``values``.

    Resamples ``values`` with replacement ``iterations`` times, takes the mean of
    each resample, and returns the ``confidence``-level percentile interval of
    those means alongside the point estimate (the mean of the original sample).

    Deterministic: a fixed ``seed`` drives the resampling RNG, so the interval is
    stable across runs and safe to commit. This is a nonparametric CI: it makes
    no normality assumption, which matters here because per-query metrics like
    recall@k are bounded in [0, 1] and often heavily skewed (e.g. a 0/1 hit
    indicator). An empty sample returns all-zero; a singleton returns a
    zero-width interval at its own value.
    """
    n = len(values)
    if n == 0:
        return MeanCI(mean=0.0, lo=0.0, hi=0.0, n=0)
    point = statistics.mean(values)
    if n == 1:
        return MeanCI(mean=point, lo=point, hi=point, n=1)

    rng = random.Random(seed)
    vals = list(values)
    means: list[float] = []
    for _ in range(iterations):
        # Sum of n samples with replacement, divided by n == resample mean.
        total = 0.0
        for _ in range(n):
            total += vals[rng.randrange(n)]
        means.append(total / n)
    means.sort()

    alpha = (1.0 - confidence) / 2.0
    lo = _percentile(means, alpha)
    hi = _percentile(means, 1.0 - alpha)
    return MeanCI(mean=point, lo=lo, hi=hi, n=n)


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated ``q``-quantile of an already-sorted list (q in [0,1])."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo_idx = math.floor(pos)
    hi_idx = math.ceil(pos)
    if lo_idx == hi_idx:
        return sorted_vals[lo_idx]
    frac = pos - lo_idx
    return sorted_vals[lo_idx] * (1.0 - frac) + sorted_vals[hi_idx] * frac
