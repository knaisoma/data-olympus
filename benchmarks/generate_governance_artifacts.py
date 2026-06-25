"""One-shot writer: generate the committed governance-benchmark artifacts.

Produces (or overwrites):
- benchmarks/governance/           — governance corpus, n=120, seed=0
- benchmarks/governance_queries.yaml — stratified scenario queries + gold labels
- benchmarks/governance_results/   — ablation.json + ablation.md

Run as:
    uv run python -m benchmarks.generate_governance_artifacts

Honesty labels:
- Corpus is SYNTHETIC and generated; it does not represent any real KB.
- The corpus authors applies_when triggers; queries are drawn from a DISJOINT
  held-out vocabulary, so the `paraphrase_uncovered` stratum tests bridging the
  benchmark cannot win by construction. Negative queries have no governing rule.
- Tokenizer is the dep-free SimpleTokenizer; token figures are simple-tokenizer
  counts.
"""
from __future__ import annotations

import shutil
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
_CORPUS_DIR = _REPO_ROOT / "benchmarks" / "governance"
_QUERIES_PATH = _REPO_ROOT / "benchmarks" / "governance_queries.yaml"
_RESULTS_DIR = _REPO_ROOT / "benchmarks" / "governance_results"
_INDEX_PATH = _REPO_ROOT / "benchmarks" / "bench.db"


def main() -> None:
    from benchmarks.ablate import run_ablation, write_ablation
    from benchmarks.governance_corpus import generate_governance_corpus
    from benchmarks.governance_queries import build_governance_queries, write_governance_queries
    from benchmarks.tokenizer import SimpleTokenizer
    from data_olympus.index import Index

    if _CORPUS_DIR.exists():
        shutil.rmtree(_CORPUS_DIR)  # start clean so stale docs never accumulate

    print("Generating governance corpus (n=120, seed=0) ...")
    manifest = generate_governance_corpus(_CORPUS_DIR, n=120, seed=0)
    print(f"  {len(manifest.concepts)} concepts written to {_CORPUS_DIR}")

    print("Writing governance_queries.yaml ...")
    queries = build_governance_queries(manifest)
    write_governance_queries(queries, _QUERIES_PATH)
    print(f"  {len(queries)} queries written to {_QUERIES_PATH}")

    print("Building index ...")
    idx = Index(_INDEX_PATH)
    result = idx.build(_CORPUS_DIR, source_commit="generated-governance-artifacts")
    print(f"  {result.docs_indexed} docs indexed at {_INDEX_PATH}")

    print("Running ablation ...")
    report = run_ablation(
        corpus_root=_CORPUS_DIR,
        idx=idx,
        queries=queries,
        tokenizer=SimpleTokenizer(),
        k=5,
    )

    print(f"Writing ablation results to {_RESULTS_DIR} ...")
    write_ablation(report, _RESULTS_DIR)
    print(f"  {len(report.rows)} ablation rows written.")
    print("Done.")


if __name__ == "__main__":
    main()
