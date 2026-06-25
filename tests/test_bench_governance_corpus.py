from __future__ import annotations

from pathlib import Path

from benchmarks.governance_corpus import generate_governance_corpus
from data_olympus.index import Index


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
