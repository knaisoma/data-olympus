from __future__ import annotations

from typing import TYPE_CHECKING

from benchmarks.governance_corpus import generate_governance_corpus
from data_olympus.index import Index

if TYPE_CHECKING:
    from pathlib import Path


def test_corpus_is_deterministic(tmp_path: Path) -> None:
    a = generate_governance_corpus(tmp_path / "a", n=60, seed=3)
    b = generate_governance_corpus(tmp_path / "b", n=60, seed=3)
    assert [c.id for c in a.concepts] == [c.id for c in b.concepts]


def test_docs_carry_applies_when_triggers(tmp_path: Path) -> None:
    m = generate_governance_corpus(tmp_path / "kb", n=80, seed=1)
    idx = Index(tmp_path / "i.db")
    idx.build(tmp_path / "kb", source_commit="x")
    topic = m.topics[0]
    doc = idx.get(topic.current_id)
    assert doc is not None
    assert doc.applies_when, "governing docs must carry applies_when triggers"


def test_covered_and_uncovered_terms_are_disjoint(tmp_path: Path) -> None:
    m = generate_governance_corpus(tmp_path / "kb", n=80, seed=1)
    for t in m.topics:
        assert not (set(t.covered_terms) & set(t.uncovered_terms)), (
            "trigger-covered and held-out terms must be disjoint by construction"
        )


def test_corpus_lints_clean(tmp_path: Path) -> None:
    from data_olympus.format import discover_bundle_files, lint_files
    root = tmp_path / "kb"
    generate_governance_corpus(root, n=80, seed=1)
    results = lint_files(discover_bundle_files(root))
    errors = [(p, f) for p, fs in results.items() for f in fs if f.severity == "error"]
    assert not errors, f"governance corpus must lint clean; got {errors}"


def test_has_supersession_pairs(tmp_path: Path) -> None:
    m = generate_governance_corpus(tmp_path / "kb", n=120, seed=2)
    assert any(t.stale_id is not None for t in m.topics)


def test_trigger_terms_absent_from_doc_body(tmp_path: Path) -> None:
    """Fair-test invariant: the doc body must NOT contain the applies_when
    trigger terms, so the benchmark measures the marginal value of indexing
    applies_when (vs body-only FTS) rather than crediting the body."""
    m = generate_governance_corpus(tmp_path / "kb", n=120, seed=0)
    by_topic = {t.current_id: t for t in m.topics}
    for concept in m.concepts:
        topic = by_topic.get(concept.id)
        if topic is None:
            continue  # superseded predecessor; same triggers, same invariant below
        body_lower = concept.body.lower()
        topic_name = concept.topic.lower()
        for term in topic.covered_terms:
            t = term.lower()
            # A trigger that IS the topic identifier may legitimately appear in
            # the body via the topic name (realistic: some docs name the trigger).
            # The aggregate ablation reflects that honestly. The invariant we
            # enforce is that the body does not ENUMERATE the trigger list.
            if t in topic_name or topic_name in t:
                continue
            assert t not in body_lower, (
                f"trigger term {term!r} leaked into the body of {concept.id}; "
                "the fair test requires non-name triggers to live only in applies_when"
            )
