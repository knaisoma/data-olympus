"""Tokenizer-based regression guard for the compact read-tool defaults (issue #65).

Runs the committed measurement harness (:func:`benchmarks.token_compact.measure`)
and asserts the compact default stays under a measured budget WITH HEADROOM, so a
future change that re-bloats a response (re-adds a dropped field, uncaps snippets)
fails loudly here. Budgets are set ~15% above the measured compact counts at the
time of writing; the point is to catch regressions, not to pin exact numbers.

Measured compact totals (simple tokenizer, example-bundle) at authoring time:
  kb_search(20)=921  kb_search(100)=1451  kb_get(STD-U-004)=541  kb_get(ADR-002)=489
  kb_get(MEM...)=667  kb_list(T1)=192  kb_outline=369  kb_health=81  aggregate=4711
The compact aggregate saved 24.7% (simple) / 26.1% (tiktoken cl100k) vs verbose.
"""
from __future__ import annotations

import pytest

from benchmarks.token_compact import measure

# Per-scenario compact budgets (simple tokenizer). ~15% headroom over measured.
_BUDGETS = {
    "kb_search (limit 20, 'commit message format')": 1060,
    "kb_search (limit 100, 'module standard commit secret')": 1670,
    "kb_get (STD-U-004)": 625,
    "kb_get (ADR-002)": 565,
    "kb_get (MEM-2026-06-20-nestjs-module-naming-collision)": 770,
    "kb_list (T1)": 225,
    "kb_outline": 425,
    "kb_health": 95,
}
_AGGREGATE_BUDGET = 5420  # ~15% over 4711


def test_compact_defaults_under_budget() -> None:
    report = measure(tokenizer="simple")
    over = [
        f"{r.label}: {r.compact_tokens} > {_BUDGETS[r.label]}"
        for r in report.rows
        if r.label in _BUDGETS and r.compact_tokens > _BUDGETS[r.label]
    ]
    assert not over, "compact response(s) exceed token budget (re-bloat?): " + "; ".join(over)
    assert report.total_compact <= _AGGREGATE_BUDGET, (
        f"aggregate compact tokens {report.total_compact} exceed budget {_AGGREGATE_BUDGET}"
    )


def test_compact_saves_meaningfully_vs_verbose() -> None:
    """The adopt-decision bar from issue #65: kb_search must save >= 25% and the
    aggregate must save a meaningful fraction. Guards against a change that
    quietly makes compact ~= verbose (defeating the point)."""
    report = measure(tokenizer="simple")
    by_label = {r.label: r for r in report.rows}

    search20 = by_label["kb_search (limit 20, 'commit message format')"]
    search100 = by_label["kb_search (limit 100, 'module standard commit secret')"]
    assert search20.pct_saved >= 25.0, f"kb_search(20) only saved {search20.pct_saved:.1f}%"
    assert search100.pct_saved >= 25.0, f"kb_search(100) only saved {search100.pct_saved:.1f}%"

    assert report.total_pct_saved >= 20.0, (
        f"aggregate only saved {report.total_pct_saved:.1f}%"
    )


def _tiktoken_available() -> bool:
    try:
        import tiktoken  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(not _tiktoken_available(), reason="tiktoken not installed")
def test_tiktoken_aggregate_savings_hold() -> None:
    """Real-tokenizer (tiktoken cl100k) guard for the headline PR/CHANGELOG claim.

    The changelog quotes tiktoken-cl100k percentages; this asserts they do not
    silently regress (the simple-tokenizer budget above could pass while the real
    model-token savings evaporate). Skipped with a clear reason when tiktoken is
    absent so it never blocks a dependency-free environment."""
    report = measure(tokenizer="tiktoken")
    assert report.tokenizer_name == "tiktoken-cl100k"
    by_label = {r.label: r for r in report.rows}
    assert by_label["kb_search (limit 20, 'commit message format')"].pct_saved >= 30.0
    assert by_label["kb_search (limit 100, 'module standard commit secret')"].pct_saved >= 30.0
    assert report.total_pct_saved >= 20.0, (
        f"tiktoken aggregate only saved {report.total_pct_saved:.1f}%"
    )


def test_verbose_matches_legacy_model_dump() -> None:
    """Verbose token counts must equal the pre-compact (model_dump) shape exactly:
    the harness measures verbose via shape_response(..., verbose=True), which is
    model_dump. This asserts verbose is never smaller than compact (a sanity floor
    that the modes are not swapped)."""
    report = measure(tokenizer="simple")
    for r in report.rows:
        assert r.verbose_tokens >= r.compact_tokens, (
            f"{r.label}: verbose {r.verbose_tokens} < compact {r.compact_tokens} (modes swapped?)"
        )
