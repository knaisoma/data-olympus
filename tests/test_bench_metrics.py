from __future__ import annotations

import math

from benchmarks.metrics import (
    mrr,
    ndcg_at_k,
    precision_signal,
    recall_at_k,
    staleness_error,
)


def test_recall_hit_when_gold_in_top_k() -> None:
    assert recall_at_k(["a", "b", "c"], {"b"}, k=2) == 1.0


def test_recall_miss_when_gold_below_k() -> None:
    assert recall_at_k(["a", "b", "c"], {"c"}, k=2) == 0.0


def test_recall_fraction_of_multiple_gold() -> None:
    assert recall_at_k(["a", "b", "c"], {"a", "c"}, k=3) == 1.0
    assert recall_at_k(["a", "b", "d"], {"a", "c"}, k=3) == 0.5


def test_precision_signal_full_when_payload_is_just_gold() -> None:
    # payload tokens == gold tokens -> ratio 1.0
    assert precision_signal(payload_tokens=20, gold_tokens=20, gold_retrieved=True) == 1.0


def test_precision_signal_low_for_huge_payload() -> None:
    p = precision_signal(payload_tokens=1000, gold_tokens=20, gold_retrieved=True)
    assert math.isclose(p, 0.02)


def test_precision_zero_when_gold_not_retrieved() -> None:
    assert precision_signal(payload_tokens=1000, gold_tokens=20, gold_retrieved=False) == 0.0


def test_mrr_first_gold_rank() -> None:
    assert mrr(["a", "b", "c"], {"b"}) == 0.5
    assert mrr(["a", "b", "c"], {"a"}) == 1.0
    assert mrr(["a", "b", "c"], {"z"}) == 0.0


def test_ndcg_perfect_when_gold_first() -> None:
    assert ndcg_at_k(["a", "b", "c"], {"a"}, k=3) == 1.0


def test_ndcg_discounts_lower_rank() -> None:
    # single gold at rank 2: DCG = 1/log2(3); IDCG = 1/log2(2) = 1
    expected = (1 / math.log2(3)) / 1.0
    assert math.isclose(ndcg_at_k(["a", "b", "c"], {"b"}, k=3), expected)


def test_staleness_error_when_stale_ranked_above_current() -> None:
    assert staleness_error(["STALE", "CURRENT"], current_id="CURRENT", stale_id="STALE") == 1


def test_no_staleness_when_current_above_stale() -> None:
    assert staleness_error(["CURRENT", "STALE"], current_id="CURRENT", stale_id="STALE") == 0


def test_no_staleness_when_stale_absent() -> None:
    assert staleness_error(["CURRENT", "OTHER"], current_id="CURRENT", stale_id="STALE") == 0


def test_staleness_error_when_only_stale_present() -> None:
    assert staleness_error(["STALE", "OTHER"], current_id="CURRENT", stale_id="STALE") == 1
