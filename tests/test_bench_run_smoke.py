"""Smoke tests for the benchmark harness (dep-free)."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def test_benchmarks_package_imports() -> None:
    import benchmarks  # noqa: F401


def test_run_benchmark_smoke_dep_free(tmp_path: Path) -> None:
    from benchmarks.corpus_gen import generate_corpus
    from benchmarks.methods.bm25 import Bm25Method
    from benchmarks.methods.data_olympus import DataOlympusMethod
    from benchmarks.methods.grep_read import GrepReadMethod
    from benchmarks.methods.whole_dump import WholeDumpMethod
    from benchmarks.query_gen import build_queries
    from benchmarks.run import run_benchmark
    from benchmarks.tokenizer import SimpleTokenizer
    from data_olympus.index import Index

    root = tmp_path / "kb"
    manifest = generate_corpus(root, n=60, seed=4)
    idx = Index(tmp_path / "idx.db")
    idx.build(root, source_commit="bench")

    methods = [
        DataOlympusMethod(idx),
        WholeDumpMethod(root),
        GrepReadMethod(root),
        Bm25Method(root, k=5),
    ]
    queries = build_queries(manifest)[:12]
    report = run_benchmark(
        corpus_root=root, idx=idx, queries=queries, methods=methods,
        tokenizer=SimpleTokenizer(), k=5, curve_sizes=(25, 50),
    )
    rows = {r.method for r in report.rows}
    assert "data-olympus" in rows
    # data-olympus mean tokens should beat whole-bundle dump
    do = next(r for r in report.rows if r.method == "data-olympus" and r.category == "ALL")
    dump = next(r for r in report.rows if r.method == "whole-dump" and r.category == "ALL")
    assert do.mean_tokens < dump.mean_tokens


def test_run_writes_report(tmp_path: Path) -> None:
    from benchmarks.corpus_gen import generate_corpus
    from benchmarks.methods.data_olympus import DataOlympusMethod
    from benchmarks.methods.whole_dump import WholeDumpMethod
    from benchmarks.query_gen import build_queries
    from benchmarks.run import run_benchmark, write_report
    from benchmarks.tokenizer import SimpleTokenizer
    from data_olympus.index import Index

    root = tmp_path / "kb"
    manifest = generate_corpus(root, n=40, seed=4)
    idx = Index(tmp_path / "idx.db")
    idx.build(root, source_commit="bench")
    report = run_benchmark(
        corpus_root=root, idx=idx, queries=build_queries(manifest)[:8],
        methods=[DataOlympusMethod(idx), WholeDumpMethod(root)],
        tokenizer=SimpleTokenizer(), k=5, curve_sizes=(25,),
    )
    out = tmp_path / "results"
    write_report(report, out)
    assert (out / "results.json").exists()
    assert (out / "report.md").exists()
    md_text = (out / "report.md").read_text()
    assert "Quantified" in md_text or "data-olympus" in md_text
