from __future__ import annotations

from typing import TYPE_CHECKING

from benchmarks.governance_corpus import generate_governance_corpus
from benchmarks.governance_queries import build_governance_queries

if TYPE_CHECKING:
    from pathlib import Path


def test_covers_all_strata(tmp_path: Path) -> None:
    m = generate_governance_corpus(tmp_path / "kb", n=120, seed=2)
    qs = build_governance_queries(m)
    strata = {q.stratum for q in qs}
    assert {"trigger_covered", "paraphrase_uncovered", "supersession", "negative"} <= strata


def test_negative_queries_have_empty_gold(tmp_path: Path) -> None:
    m = generate_governance_corpus(tmp_path / "kb", n=120, seed=2)
    negs = [q for q in build_governance_queries(m) if q.stratum == "negative"]
    assert negs
    assert all(q.gold_ids == [] for q in negs)


def test_uncovered_queries_use_no_trigger_term(tmp_path: Path) -> None:
    m = generate_governance_corpus(tmp_path / "kb", n=120, seed=2)
    triggers = {term for t in m.topics for term in t.covered_terms}
    uncovered = [q for q in build_governance_queries(m) if q.stratum == "paraphrase_uncovered"]
    assert uncovered
    for q in uncovered:
        assert not (set(q.text.lower().split()) & {x.lower() for x in triggers}), (
            "uncovered queries must not contain any trigger term"
        )
