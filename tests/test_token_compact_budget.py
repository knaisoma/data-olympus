"""Tokenizer-based regression guard for the compact read-tool defaults (issue #65).

Runs the committed measurement harness (:func:`benchmarks.token_compact.measure`)
and asserts the compact default stays under a measured budget WITH HEADROOM, so a
future change that re-bloats a response (re-adds a dropped field, uncaps snippets)
fails loudly here. Budgets are set ~15% above the measured compact counts at the
time of writing; the point is to catch regressions, not to pin exact numbers.

Measured compact totals (simple tokenizer, example-bundle) at authoring time:
  kb_search(20)=921  kb_search(100)=1451  kb_get(STD-U-004)=533  kb_get(ADR-002)=481
  kb_get(MEM...)=659  kb_list(T1)=192  kb_outline=369  kb_health=81  aggregate=4687
The compact aggregate saved 25.1% (simple) / 26.6% (tiktoken cl100k) vs verbose.
"""
from __future__ import annotations

from benchmarks.token_compact import measure

# Per-scenario compact budgets (simple tokenizer). ~15% headroom over measured.
_BUDGETS = {
    "kb_search (limit 20, 'commit message format')": 1060,
    "kb_search (limit 100, 'module standard commit secret')": 1670,
    "kb_get (STD-U-004)": 615,
    "kb_get (ADR-002)": 555,
    "kb_get (MEM-2026-06-20-nestjs-module-naming-collision)": 760,
    "kb_list (T1)": 225,
    "kb_outline": 425,
    "kb_health": 95,
}
_AGGREGATE_BUDGET = 5400  # ~15% over 4687


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
