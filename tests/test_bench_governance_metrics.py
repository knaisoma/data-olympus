from __future__ import annotations

from benchmarks.metrics import false_positive_rate, governance_miss_rate


def test_governance_miss_rate_counts_queries_with_no_gold_in_topk() -> None:
    # ranked lists per query, gold sets per query, k=3
    ranked = [["a", "b"], ["x", "y", "z"], ["c"]]
    golds = [{"a"}, {"GONE"}, {"c"}]
    # query 2 misses -> miss rate 1/3
    assert governance_miss_rate(ranked, golds, k=3) == 1 / 3


def test_governance_miss_rate_empty_inputs_is_zero() -> None:
    assert governance_miss_rate([], [], k=3) == 0.0


def test_false_positive_rate_on_negative_queries() -> None:
    # For negative queries (no governing rule), any non-empty retrieval is a
    # false positive. retrieved counts per negative query:
    retrieved_counts = [0, 2, 0, 5]
    # 2 of 4 returned something -> 0.5
    assert false_positive_rate(retrieved_counts) == 0.5


def test_false_positive_rate_no_negatives_is_zero() -> None:
    assert false_positive_rate([]) == 0.0
