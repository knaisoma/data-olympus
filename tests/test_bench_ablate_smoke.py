from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def test_run_ablation_smoke(tmp_path: Path) -> None:
    from benchmarks.ablate import run_ablation
    from benchmarks.governance_corpus import generate_governance_corpus
    from benchmarks.governance_queries import build_governance_queries
    from benchmarks.tokenizer import SimpleTokenizer
    from data_olympus.index import Index

    root = tmp_path / "kb"
    m = generate_governance_corpus(root, n=80, seed=1)
    idx = Index(tmp_path / "i.db")
    idx.build(root, source_commit="x")
    qs = build_governance_queries(m)
    report = run_ablation(corpus_root=root, idx=idx, queries=qs,
                          tokenizer=SimpleTokenizer(), k=5)
    labels = {r.config for r in report.rows}
    assert {"fts-no-metadata", "fts+applies_when", "bm25-baseline"} <= labels
    # applies_when config should not do WORSE than no-metadata on trigger_covered recall
    cov_no = next(r for r in report.rows
                  if r.config == "fts-no-metadata" and r.stratum == "trigger_covered")
    cov_aw = next(r for r in report.rows
                  if r.config == "fts+applies_when" and r.stratum == "trigger_covered")
    assert cov_aw.recall >= cov_no.recall


def test_write_ablation(tmp_path: Path) -> None:
    from benchmarks.ablate import run_ablation, write_ablation
    from benchmarks.governance_corpus import generate_governance_corpus
    from benchmarks.governance_queries import build_governance_queries
    from benchmarks.tokenizer import SimpleTokenizer
    from data_olympus.index import Index
    root = tmp_path / "kb"
    m = generate_governance_corpus(root, n=60, seed=1)
    idx = Index(tmp_path / "i.db")
    idx.build(root, source_commit="x")
    report = run_ablation(corpus_root=root, idx=idx,
                          queries=build_governance_queries(m),
                          tokenizer=SimpleTokenizer(), k=5)
    out = tmp_path / "res"
    write_ablation(report, out)
    assert (out / "ablation.json").exists()
    assert (out / "ablation.md").exists()
