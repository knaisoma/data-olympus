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


def test_uncovered_queries_share_no_trigger_token(tmp_path: Path) -> None:
    """Strict token-level invariant (regression guard for the FTS-token leak the
    whole-string check missed): a paraphrase_uncovered query must share NO FTS
    token with its OWN gold doc's trigger terms, so applies_when cannot help it.
    """
    from benchmarks.governance_queries import _fts_tokens, _trigger_tokens

    m = generate_governance_corpus(tmp_path / "kb", n=120, seed=0)
    by_id = {t.current_id: t for t in m.topics}
    uncovered = [q for q in build_governance_queries(m) if q.stratum == "paraphrase_uncovered"]
    assert uncovered
    for q in uncovered:
        topic = by_id[q.current_id]
        overlap = _fts_tokens(q.text) & _trigger_tokens(topic.covered_terms)
        assert not overlap, (
            f"uncovered query {q.text!r} shares FTS token(s) {overlap} with its "
            "gold doc's triggers; the held-out stratum must be token-disjoint"
        )
