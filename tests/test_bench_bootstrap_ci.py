"""Tests for the bootstrap confidence-interval math in benchmarks.metrics."""
from __future__ import annotations

import statistics

from benchmarks.metrics import bootstrap_mean_ci


def test_empty_sample_is_all_zero() -> None:
    ci = bootstrap_mean_ci([])
    assert ci == (0.0, 0.0, 0.0, 0)


def test_singleton_is_zero_width_at_its_value() -> None:
    ci = bootstrap_mean_ci([0.7])
    assert ci.mean == 0.7
    assert ci.lo == 0.7
    assert ci.hi == 0.7
    assert ci.n == 1


def test_constant_sample_has_zero_width_interval() -> None:
    # Every resample of a constant sample has the same mean, so lo == hi == mean.
    ci = bootstrap_mean_ci([1.0, 1.0, 1.0, 1.0, 1.0])
    assert ci.mean == 1.0
    assert ci.lo == 1.0
    assert ci.hi == 1.0


def test_point_estimate_is_the_sample_mean() -> None:
    vals = [0.0, 1.0, 0.0, 1.0, 1.0, 0.0, 1.0, 0.0]
    ci = bootstrap_mean_ci(vals)
    assert ci.mean == statistics.mean(vals)


def test_interval_brackets_the_mean_and_is_ordered() -> None:
    vals = [0.0, 1.0] * 25  # mean 0.5, high variance
    ci = bootstrap_mean_ci(vals)
    assert ci.lo <= ci.mean <= ci.hi
    assert ci.lo < ci.hi  # non-degenerate for a spread sample


def test_deterministic_across_calls() -> None:
    # Fixed seed => identical interval on repeated calls (committable numbers).
    vals = [0.0, 1.0, 1.0, 0.0, 1.0, 0.0, 0.0, 1.0, 1.0, 0.0]
    a = bootstrap_mean_ci(vals)
    b = bootstrap_mean_ci(vals)
    assert a == b


def test_known_distribution_ci_is_near_true_mean() -> None:
    # A 0/1 sample with p=0.5, n=200: the 95% CI half-width should be modest
    # (~ +/-0.07 for the mean) and must contain 0.5. This is a sanity check that
    # the interval width is in the right ballpark for a known distribution.
    vals = ([1.0] * 100) + ([0.0] * 100)
    ci = bootstrap_mean_ci(vals)
    assert abs(ci.mean - 0.5) < 1e-9
    assert ci.lo < 0.5 < ci.hi
    half_width = (ci.hi - ci.lo) / 2
    # Normal-approx SE = sqrt(0.25/200) ~= 0.035; 95% half-width ~= 0.069.
    assert 0.03 < half_width < 0.12


def test_larger_sample_gives_tighter_interval() -> None:
    small = bootstrap_mean_ci([0.0, 1.0] * 5)   # n=10
    large = bootstrap_mean_ci([0.0, 1.0] * 100)  # n=200, same mean
    small_w = small.hi - small.lo
    large_w = large.hi - large.lo
    assert large_w < small_w
