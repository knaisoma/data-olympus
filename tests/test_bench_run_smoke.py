"""Smoke tests for the benchmark harness (dep-free)."""
from __future__ import annotations


def test_benchmarks_package_imports() -> None:
    import benchmarks  # noqa: F401
