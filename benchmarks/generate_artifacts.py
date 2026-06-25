"""One-shot writer: generate the committed benchmark artifacts.

Produces (or overwrites):
- benchmarks/corpus/         — synthetic corpus, n=250, seed=0
- benchmarks/queries.yaml    — benchmark query set with gold labels
- benchmarks/results/        — results.json + report.md

Run as:
    uv run python -m benchmarks.generate_artifacts

Honesty labels:
- Corpus is SYNTHETIC and generated; it does not represent any real KB.
- Tokenizer is the dep-free SimpleTokenizer (word runs + punctuation marks).
  Token ratios across methods are tokenizer-robust; absolute counts are
  simple-tokenizer-specific.
- Vector-RAG is NOT included in this committed run because the [bench] optional
  dependencies (sentence-transformers, tiktoken, numpy) are absent from the CI
  install. Vector-RAG is expected to win on the 'semantic' query category when
  [bench] deps are present.
"""
from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
_CORPUS_DIR = _REPO_ROOT / "benchmarks" / "corpus"
_QUERIES_PATH = _REPO_ROOT / "benchmarks" / "queries.yaml"
_RESULTS_DIR = _REPO_ROOT / "benchmarks" / "results"
_INDEX_PATH = _REPO_ROOT / "benchmarks" / "bench.db"


def main() -> None:
    from benchmarks.corpus_gen import generate_corpus
    from benchmarks.methods.bm25 import Bm25Method
    from benchmarks.methods.data_olympus import DataOlympusMethod
    from benchmarks.methods.grep_read import GrepReadMethod
    from benchmarks.methods.whole_dump import WholeDumpMethod
    from benchmarks.query_gen import build_queries, write_queries
    from benchmarks.run import run_benchmark, write_report
    from benchmarks.tokenizer import SimpleTokenizer
    from data_olympus.index import Index

    print("Generating corpus (n=250, seed=0) ...")
    manifest = generate_corpus(_CORPUS_DIR, n=250, seed=0)
    print(f"  {len(manifest.concepts)} concepts written to {_CORPUS_DIR}")

    print("Writing queries.yaml ...")
    queries = build_queries(manifest)
    write_queries(queries, _QUERIES_PATH)
    print(f"  {len(queries)} queries written to {_QUERIES_PATH}")

    print("Building index ...")
    idx = Index(_INDEX_PATH)
    result = idx.build(_CORPUS_DIR, source_commit="generated-artifacts")
    print(f"  {result.docs_indexed} docs indexed at {_INDEX_PATH}")

    print("Running benchmark ...")
    methods: list[object] = [
        DataOlympusMethod(idx),
        WholeDumpMethod(_CORPUS_DIR),
        GrepReadMethod(_CORPUS_DIR),
        Bm25Method(_CORPUS_DIR, k=5),
    ]
    report = run_benchmark(
        corpus_root=_CORPUS_DIR,
        idx=idx,
        queries=queries,
        methods=methods,
        tokenizer=SimpleTokenizer(),
        k=5,
        curve_sizes=(25, 50, 100, 250),
    )

    print(f"Writing results to {_RESULTS_DIR} ...")
    write_report(report, _RESULTS_DIR)
    print(f"  {len(report.rows)} aggregate rows written.")
    print("Done.")


if __name__ == "__main__":
    main()
