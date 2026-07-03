"""Tests that the governance strata are large enough for non-degenerate CIs."""
from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from benchmarks.governance_corpus import generate_governance_corpus
from benchmarks.governance_queries import build_governance_queries

if TYPE_CHECKING:
    from pathlib import Path


def test_stratum_sizes_meet_minimums(tmp_path: Path) -> None:
    m = generate_governance_corpus(tmp_path / "kb", n=120, seed=0)
    qs = build_governance_queries(m)
    counts = Counter(q.stratum for q in qs)
    # Targets from the 0.3.0 methodology audit.
    assert counts["trigger_covered"] >= 30
    assert counts["negative"] >= 30
    assert counts["supersession"] >= 10
    assert counts["paraphrase_uncovered"] >= 30


def test_supersession_pair_count(tmp_path: Path) -> None:
    m = generate_governance_corpus(tmp_path / "kb", n=120, seed=0)
    pairs = sum(1 for t in m.topics if t.stale_id is not None)
    assert pairs >= 10


def test_distractor_count(tmp_path: Path) -> None:
    m = generate_governance_corpus(tmp_path / "kb", n=120, seed=0)
    assert len(m.distractor_topics) >= 30


def test_no_trigger_term_is_a_query_template_stopword(tmp_path: Path) -> None:
    # Regression guard: a trigger term made only of query-template words (e.g.
    # "who did what") would match every negative query and destroy abstention.
    m = generate_governance_corpus(tmp_path / "kb", n=120, seed=0)
    stop = {
        "what", "is", "the", "team", "standard", "for", "how", "should", "i",
        "a", "an", "our", "needs", "to", "best", "practice", "question",
        "about", "looking", "guidance", "on", "in", "project", "approved",
        "approach", "of",
    }
    for t in m.topics:
        for term in t.covered_terms:
            words = term.lower().split()
            assert not all(w in stop for w in words), (
                f"trigger {term!r} is entirely query-template stopwords; it will "
                "match every negative query and break abstention"
            )
