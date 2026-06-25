from __future__ import annotations

from typing import TYPE_CHECKING

from benchmarks.corpus_gen import generate_corpus
from data_olympus.index import Index

if TYPE_CHECKING:
    from pathlib import Path


def test_generate_corpus_is_deterministic(tmp_path: Path) -> None:
    a = generate_corpus(tmp_path / "a", n=60, seed=7)
    b = generate_corpus(tmp_path / "b", n=60, seed=7)
    assert [c.id for c in a.concepts] == [c.id for c in b.concepts]


def test_generate_corpus_has_supersession_pairs(tmp_path: Path) -> None:
    manifest = generate_corpus(tmp_path / "kb", n=120, seed=1)
    pairs = [t for t in manifest.topics if t.stale_id is not None]
    assert pairs, "expected at least one supersession pair"
    for t in pairs:
        assert t.current_id != t.stale_id


def test_generated_corpus_lints_clean(tmp_path: Path) -> None:
    # lint_files returns dict[Path, list[Finding]]; Finding.severity is "error" | "warning"
    from data_olympus.format import discover_bundle_files, lint_files

    root = tmp_path / "kb"
    generate_corpus(root, n=120, seed=1)
    results = lint_files(discover_bundle_files(root))
    # results maps path -> list[Finding]; collect only error-severity ones
    error_findings = [
        (path, f)
        for path, findings in results.items()
        for f in findings
        if f.severity == "error"
    ]
    assert not error_findings, (
        f"generated corpus must have zero error-severity findings; got {error_findings}"
    )


def test_generated_corpus_indexes_without_duplicate_ids(tmp_path: Path) -> None:
    root = tmp_path / "kb"
    generate_corpus(root, n=120, seed=1)
    idx = Index(tmp_path / "idx.db")
    result = idx.build(root, source_commit="bench")
    assert result.docs_indexed >= 120


def test_supersession_pair_has_active_and_superseded_status(tmp_path: Path) -> None:
    root = tmp_path / "kb"
    manifest = generate_corpus(root, n=120, seed=1)
    idx = Index(tmp_path / "idx.db")
    idx.build(root, source_commit="bench")
    pair = next(t for t in manifest.topics if t.stale_id is not None)
    cur = idx.get(pair.current_id)
    old = idx.get(pair.stale_id)
    assert cur is not None and cur.status == "active"
    assert old is not None and old.status == "superseded"
