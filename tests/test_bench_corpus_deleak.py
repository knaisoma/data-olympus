"""Tests that the synthetic corpus is de-leaked: lifecycle answer-vocabulary
does not appear in the documents, and old/new pairs are lexically identical."""
from __future__ import annotations

from typing import TYPE_CHECKING

from benchmarks.corpus_gen import generate_corpus

if TYPE_CHECKING:
    from pathlib import Path

# Words that used to leak from the query into the gold doc.
_LEAK_WORDS = {"previous", "current", "replaced", "(old)", "(current)"}


def test_bodies_do_not_contain_lifecycle_words(tmp_path: Path) -> None:
    m = generate_corpus(tmp_path / "kb", n=250, seed=0)
    for c in m.concepts:
        low = c.body.lower()
        for w in _LEAK_WORDS:
            assert w not in low, (
                f"lifecycle word {w!r} leaked into body of {c.id}; the corpus "
                "must carry lifecycle only in status + supersedes chain"
            )


def test_titles_do_not_carry_old_new_qualifiers(tmp_path: Path) -> None:
    m = generate_corpus(tmp_path / "kb", n=250, seed=0)
    for c in m.concepts:
        low = c.title.lower()
        assert "(old)" not in low and "(current)" not in low


def test_supersession_pair_bodies_are_identical(tmp_path: Path) -> None:
    # Old and new doc of a pair must be string-identical in searchable prose, so
    # only status/supersedes distinguishes them (no lexical way to prefer one).
    m = generate_corpus(tmp_path / "kb", n=250, seed=0)
    by_id = {c.id: c for c in m.concepts}
    pairs = [t for t in m.topics if t.stale_id is not None]
    assert pairs
    for t in pairs:
        old = by_id[t.stale_id]
        new = by_id[t.current_id]
        assert old.body == new.body, (
            f"old/new bodies differ for topic {t.topic}; de-leak requires "
            "identical prose so the lifecycle signal is metadata-only"
        )


def test_bodies_carry_shared_distractor_vocab(tmp_path: Path) -> None:
    # De-leak also sprinkles shared vocab so a body is not a near-unique bag of
    # its own topic terms. Assert at least some cross-doc word overlap exists.
    m = generate_corpus(tmp_path / "kb", n=60, seed=0)
    from collections import Counter

    words: Counter[str] = Counter()
    for c in m.concepts:
        words.update(set(c.body.lower().split()))
    # A shared-vocab word should appear in many docs (not just one topic).
    shared_hits = [w for w, n in words.items() if n >= 5 and w.isalpha()]
    assert shared_hits, "expected shared distractor vocabulary across docs"


def test_generation_is_deterministic(tmp_path: Path) -> None:
    a = generate_corpus(tmp_path / "a", n=120, seed=7)
    b = generate_corpus(tmp_path / "b", n=120, seed=7)
    assert [(c.id, c.body) for c in a.concepts] == [(c.id, c.body) for c in b.concepts]
