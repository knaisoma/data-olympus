"""Tests for the docs-drift guard: it must pass in-sync and catch mutations."""
from __future__ import annotations

from benchmarks import docs_tables


def test_committed_docs_are_in_sync() -> None:
    # The committed docs must already match the committed results.
    problems = docs_tables.check_or_write(write=False)
    assert problems == [], f"committed docs drifted from results: {problems}"


def test_replace_and_extract_roundtrip() -> None:
    text = (
        "intro\n<!-- BENCH:headline START -->\nOLD\n<!-- BENCH:headline END -->\nend"
    )
    replaced = docs_tables.replace_block(text, "headline", "NEW BODY")
    assert docs_tables.extract_block(replaced, "headline") == "NEW BODY"
    assert "intro" in replaced and "end" in replaced


def test_missing_markers_raise() -> None:
    import pytest

    with pytest.raises(ValueError):
        docs_tables.extract_block("no markers here", "headline")
    with pytest.raises(ValueError):
        docs_tables.replace_block("no markers here", "headline", "x")


def test_check_detects_a_mutated_number(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    # Copy comparison.md into a temp repo layout, mutate one number inside a
    # marked block, and assert the checker reports drift for that block.
    import shutil

    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "benchmarks").mkdir()
    real_root = docs_tables._REPO_ROOT
    # Bring the four artifacts and the two docs over.
    for rel in (
        "benchmarks/results/results.json",
        "benchmarks/governance_results/ablation.json",
        "benchmarks/real_corpus/example_bundle_result.json",
        "docs/comparison.md",
        "WHY.md",
    ):
        src = real_root / rel
        dst = repo / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst)

    # Mutate a digit inside the comparison per-category block.
    comp = repo / "docs" / "comparison.md"
    text = comp.read_text(encoding="utf-8")
    body = docs_tables.extract_block(text, "comparison_per_category")
    assert "0.582" in body
    mutated_body = body.replace("0.582", "0.999", 1)  # a wrong recall value
    comp.write_text(
        docs_tables.replace_block(text, "comparison_per_category", mutated_body),
        encoding="utf-8",
    )

    bm = repo / "benchmarks"
    monkeypatch.setattr(docs_tables, "_REPO_ROOT", repo)
    monkeypatch.setattr(docs_tables, "_RESULTS", bm / "results/results.json")
    monkeypatch.setattr(docs_tables, "_GOV", bm / "governance_results/ablation.json")
    monkeypatch.setattr(
        docs_tables, "_REAL", bm / "real_corpus/example_bundle_result.json"
    )

    problems = docs_tables.check_or_write(write=False)
    assert any("comparison_per_category" in p for p in problems), (
        f"drift check failed to catch a mutated number: {problems}"
    )


def test_prose_number_claims_all_present() -> None:
    # Every curated prose figure must currently appear in its doc.
    for rel, literal, label in docs_tables._prose_number_claims():
        text = (docs_tables._REPO_ROOT / rel).read_text(encoding="utf-8")
        assert literal in text, f"prose figure {literal!r} for {label!r} missing from {rel}"


def test_prose_guard_catches_a_mutated_prose_number(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    import shutil

    repo = tmp_path / "repo"
    real_root = docs_tables._REPO_ROOT
    for rel in (
        "benchmarks/results/results.json",
        "benchmarks/governance_results/ablation.json",
        "benchmarks/real_corpus/example_bundle_result.json",
        "docs/comparison.md",
        "WHY.md",
    ):
        dst = repo / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(real_root / rel, dst)

    # Mutate the applies_when paraphrase recall figure (0.311) in PROSE only,
    # leaving the generated table intact, so only the prose guard can catch it.
    comp = repo / "docs" / "comparison.md"
    text = comp.read_text(encoding="utf-8")
    body = docs_tables.extract_block(text, "comparison_per_category")
    # Replace 0.311 everywhere EXCEPT inside the generated tables.
    protected = body
    mutated_full = text.replace("0.311", "0.321")
    # Restore the table body so the drift is prose-only.
    mutated_full = docs_tables.replace_block(mutated_full, "comparison_per_category", protected)
    comp.write_text(mutated_full, encoding="utf-8")

    bm = repo / "benchmarks"
    monkeypatch.setattr(docs_tables, "_REPO_ROOT", repo)
    monkeypatch.setattr(docs_tables, "_RESULTS", bm / "results/results.json")
    monkeypatch.setattr(docs_tables, "_GOV", bm / "governance_results/ablation.json")
    monkeypatch.setattr(
        docs_tables, "_REAL", bm / "real_corpus/example_bundle_result.json"
    )

    problems = docs_tables.check_or_write(write=False)
    assert any("0.311" in p for p in problems), (
        f"prose guard failed to catch a mutated prose number: {problems}"
    )
