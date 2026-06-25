"""Benchmark runner, aggregation, and report writer.

Usage (CLI):
    python -m benchmarks.run [--tokenizer simple|tiktoken] [--n 250] [--with-rag]

`run_benchmark(...)` runs every method over every query, computes per-query
metrics (token cost, recall@k, precision_signal, ndcg@k, mrr, staleness), and
aggregates per (method, category) plus a synthetic "ALL" category.

`write_report(report, out_dir)` writes:
- results.json: machine-readable aggregate rows + curve data.
- report.md: human-readable table, staleness rates, token curve, and an
  explicit "Where data-olympus loses" subsection for the semantic category.
"""
from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from benchmarks.metrics import (
    mrr,
    ndcg_at_k,
    precision_signal,
    recall_at_k,
    staleness_error,
)

if TYPE_CHECKING:
    from benchmarks.methods.base import RetrievalResult
    from benchmarks.query_gen import BenchQuery
    from benchmarks.tokenizer import Tokenizer


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class AggRow:
    """Aggregated metrics for one (method, category) pair."""

    method: str
    category: str       # "exact" | "semantic" | "status" | "graph" | "ALL"
    mean_tokens: float
    recall: float
    precision: float
    ndcg: float
    mrr: float
    staleness: float    # fraction of queries where staleness_error == 1
    n: int              # number of queries in this cell


@dataclass
class BenchReport:
    """Full benchmark report: aggregate rows + token-vs-size curve."""

    rows: list[AggRow]
    # curve: method_name -> list of (corpus_size, mean_tokens) pairs
    curve: dict[str, list[tuple[int, float]]]
    tokenizer_name: str
    rag_included: bool


# ---------------------------------------------------------------------------
# Per-query metric computation
# ---------------------------------------------------------------------------


@dataclass
class _QueryRow:
    method: str
    category: str
    tokens: float
    recall: float
    precision: float
    ndcg: float
    mrr_val: float
    staleness: int


def _score_query(
    method_name: str,
    query: BenchQuery,
    result: RetrievalResult,
    tokenizer: Tokenizer,
    idx: object,
    k: int,
) -> _QueryRow:
    """Compute all metrics for a single (method, query) pair."""
    from data_olympus.index import Index  # type: ignore[attr-defined]

    assert isinstance(idx, Index)

    payload_toks = tokenizer.count(result.payload_text)
    gold_set = set(query.gold_ids)

    # Gold tokens: count tokens in the gold concept body from the index.
    gold_toks = 0
    for gid in query.gold_ids:
        doc = idx.get(gid)
        if doc is not None:
            gold_toks += tokenizer.count(doc.content_markdown)

    gold_retrieved = bool(gold_set & result.retrieved_ids)
    prec = precision_signal(
        payload_tokens=payload_toks,
        gold_tokens=gold_toks if gold_toks > 0 else 1,
        gold_retrieved=gold_retrieved,
    )
    rec = recall_at_k(result.ranked_ids, gold_set, k=k)
    ndcg = ndcg_at_k(result.ranked_ids, gold_set, k=k)
    mrr_val = mrr(result.ranked_ids, gold_set)

    stale = 0
    if query.stale_id is not None:
        stale = staleness_error(
            result.ranked_ids, current_id=query.current_id, stale_id=query.stale_id
        )

    return _QueryRow(
        method=method_name,
        category=query.category,
        tokens=payload_toks,
        recall=rec,
        precision=prec,
        ndcg=ndcg,
        mrr_val=mrr_val,
        staleness=stale,
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _aggregate(query_rows: list[_QueryRow]) -> list[AggRow]:
    """Aggregate per-query rows into (method, category) + (method, ALL) rows."""
    from collections import defaultdict

    # Group by (method, category).
    groups: dict[tuple[str, str], list[_QueryRow]] = defaultdict(list)
    for row in query_rows:
        groups[(row.method, row.category)].append(row)
        groups[(row.method, "ALL")].append(row)

    agg_rows: list[AggRow] = []
    for (method, category), rows in sorted(groups.items()):
        agg_rows.append(
            AggRow(
                method=method,
                category=category,
                mean_tokens=statistics.mean(r.tokens for r in rows),
                recall=statistics.mean(r.recall for r in rows),
                precision=statistics.mean(r.precision for r in rows),
                ndcg=statistics.mean(r.ndcg for r in rows),
                mrr=statistics.mean(r.mrr_val for r in rows),
                staleness=statistics.mean(r.staleness for r in rows),
                n=len(rows),
            )
        )
    return agg_rows


# ---------------------------------------------------------------------------
# Token-vs-size curve
# ---------------------------------------------------------------------------


def _compute_curve(
    queries: list[BenchQuery],
    methods: list[object],
    tokenizer: Tokenizer,
    curve_sizes: tuple[int, ...],
) -> dict[str, list[tuple[int, float]]]:
    """Re-generate sub-corpora and record mean payload tokens per method."""
    import tempfile

    from benchmarks.corpus_gen import generate_corpus
    from data_olympus.index import Index  # type: ignore[attr-defined]

    curve: dict[str, list[tuple[int, float]]] = {
        m.name: [] for m in methods  # type: ignore[union-attr]
    }

    for size in curve_sizes:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sub_root = tmp / "kb"
            generate_corpus(sub_root, n=size, seed=42)
            sub_idx = Index(tmp / "idx.db")
            sub_idx.build(sub_root, source_commit="curve")

            # Rebuild each method against the sub-corpus.
            sub_methods: list[object] = []
            for m in methods:  # type: ignore[union-attr]
                cls_name = type(m).__name__
                if cls_name == "DataOlympusMethod":
                    from benchmarks.methods.data_olympus import DataOlympusMethod
                    sub_methods.append(DataOlympusMethod(sub_idx))
                elif cls_name == "WholeDumpMethod":
                    from benchmarks.methods.whole_dump import WholeDumpMethod
                    sub_methods.append(WholeDumpMethod(sub_root))
                elif cls_name == "GrepReadMethod":
                    from benchmarks.methods.grep_read import GrepReadMethod
                    sub_methods.append(GrepReadMethod(sub_root))
                elif cls_name == "Bm25Method":
                    from benchmarks.methods.bm25 import Bm25Method
                    sub_methods.append(Bm25Method(sub_root, k=5))
                elif cls_name == "VectorRagMethod":
                    try:
                        from benchmarks.methods.vector_rag import VectorRagMethod
                        sub_methods.append(VectorRagMethod(sub_root, k=5))
                    except RuntimeError:
                        sub_methods.append(m)  # keep original as sentinel
                else:
                    sub_methods.append(m)

            # Limit to a small query sample to keep curve fast.
            sample_queries = queries[:min(8, len(queries))]
            for sm, orig_m in zip(sub_methods, methods, strict=True):
                name = orig_m.name  # type: ignore[union-attr]
                tok_counts = []
                for q in sample_queries:
                    try:
                        res = sm.retrieve(q.text)  # type: ignore[union-attr]
                        tok_counts.append(tokenizer.count(res.payload_text))
                    except Exception:  # noqa: BLE001
                        pass
                if tok_counts:
                    curve[name].append((size, statistics.mean(tok_counts)))

    return curve


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------


def run_benchmark(
    *,
    corpus_root: Path,  # noqa: ARG001
    idx: object,
    queries: list[BenchQuery],
    methods: list[object],
    tokenizer: Tokenizer,
    k: int = 5,
    curve_sizes: tuple[int, ...] = (25, 50, 100, 250),
) -> BenchReport:
    """Run every method over every query and return an aggregated BenchReport."""
    query_rows: list[_QueryRow] = []

    for method in methods:
        for query in queries:
            result = method.retrieve(query.text)  # type: ignore[union-attr]
            row = _score_query(
                method_name=method.name,  # type: ignore[union-attr]
                query=query,
                result=result,
                tokenizer=tokenizer,
                idx=idx,
                k=k,
            )
            query_rows.append(row)

    agg = _aggregate(query_rows)

    curve = _compute_curve(
        queries=queries,
        methods=methods,
        tokenizer=tokenizer,
        curve_sizes=curve_sizes,
    )

    rag_included = any(
        type(m).__name__ == "VectorRagMethod" for m in methods  # type: ignore[union-attr]
    )

    return BenchReport(
        rows=agg,
        curve=curve,
        tokenizer_name=tokenizer.name,
        rag_included=rag_included,
    )


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------

_CATS = ["exact", "semantic", "status", "graph", "ALL"]


def write_report(report: BenchReport, out_dir: Path) -> None:
    """Write results.json and report.md to out_dir (created if absent)."""
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- results.json ---
    json_data = {
        "tokenizer": report.tokenizer_name,
        "rag_included": report.rag_included,
        "rows": [asdict(r) for r in report.rows],
        "curve": {
            name: [[size, mean] for size, mean in pts]
            for name, pts in report.curve.items()
        },
    }
    (out_dir / "results.json").write_text(
        json.dumps(json_data, indent=2), encoding="utf-8"
    )

    # --- report.md ---
    methods = sorted({r.method for r in report.rows})

    lines: list[str] = [
        "# Retrieval Benchmark Report",
        "",
        f"**Tokenizer:** {report.tokenizer_name}  ",
        f"**RAG included:** {report.rag_included}  ",
        "**Corpus:** synthetic (see `benchmarks/corpus/`)  ",
        "",
        "## Quantified Comparison",
        "",
        "Per-category aggregate metrics across all benchmark queries.",
        "",
    ]

    # Per-category table — break long header/separator into variables.
    hdr_cols = "Method | Category | Mean Tokens | Recall@k | Precision | NDCG@k | MRR"
    sep_cols = "--------|----------|-------------|----------|-----------|--------|-----"
    header = f"| {hdr_cols} | Staleness Rate | N |"
    separator = f"| {sep_cols} |----------------|---|"
    lines += [header, separator]

    def _cat_order(r: AggRow) -> int:
        return _CATS.index(r.category) if r.category in _CATS else 99

    for r in sorted(report.rows, key=lambda x: (x.method, _cat_order(x))):
        lines.append(
            f"| {r.method} | {r.category} | {r.mean_tokens:.1f} | "
            f"{r.recall:.3f} | {r.precision:.3f} | {r.ndcg:.3f} | "
            f"{r.mrr:.3f} | {r.staleness:.3f} | {r.n} |"
        )
    lines.append("")

    # Per-method staleness rate lines.
    lines += ["## Staleness Rates", ""]
    for method in methods:
        all_row = next(
            (r for r in report.rows if r.method == method and r.category == "ALL"),
            None,
        )
        if all_row:
            lines.append(
                f"- **{method}**: staleness rate = {all_row.staleness:.3f} "
                "(fraction of queries where a superseded concept ranked "
                "above its replacement)"
            )
    lines.append("")

    # Token-vs-size curve.
    lines += ["## Token Cost vs Corpus Size", ""]
    if report.curve:
        curve_methods = sorted(report.curve.keys())
        sizes = sorted({size for pts in report.curve.values() for size, _ in pts})
        if sizes:
            curve_header = "| Corpus Size | " + " | ".join(curve_methods) + " |"
            curve_sep = (
                "|-------------|"
                + "|".join("-" * (len(m) + 2) for m in curve_methods)
                + "|"
            )
            lines += [curve_header, curve_sep]
            for size in sizes:
                row_vals = []
                for m in curve_methods:
                    pts_dict = dict(report.curve.get(m, []))
                    val = pts_dict.get(size)
                    row_vals.append(f"{val:.1f}" if val is not None else "n/a")
                lines.append(f"| {size} | " + " | ".join(row_vals) + " |")
            lines.append("")

    # Where data-olympus loses.
    lines += ["### Where data-olympus loses", ""]
    sem_rows = [r for r in report.rows if r.category == "semantic"]
    do_sem = next((r for r in sem_rows if r.method == "data-olympus"), None)
    if do_sem:
        lines.append(
            f"On **semantic** (paraphrase) queries, data-olympus achieves "
            f"recall={do_sem.recall:.3f}, ndcg={do_sem.ndcg:.3f}. "
            "This is the category where dense vector search has the largest "
            "advantage, because paraphrases lack the keyword overlap that "
            "the BM25-based index relies on."
        )
        for other in sem_rows:
            if other.method != "data-olympus":
                lines.append(
                    f"- **{other.method}** semantic: "
                    f"recall={other.recall:.3f}, ndcg={other.ndcg:.3f}"
                )
    else:
        lines.append("No semantic-category rows found in this run.")
    lines.append("")

    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI: generate corpus, run benchmark, write results."""
    parser = argparse.ArgumentParser(
        description="Run the data-olympus retrieval benchmark"
    )
    parser.add_argument("--tokenizer", default="simple", choices=["simple", "tiktoken"])
    parser.add_argument("--n", type=int, default=250, help="corpus size")
    parser.add_argument("--with-rag", action="store_true", help="include vector-RAG method")
    args = parser.parse_args()

    from benchmarks.corpus_gen import generate_corpus
    from benchmarks.methods.bm25 import Bm25Method
    from benchmarks.methods.data_olympus import DataOlympusMethod
    from benchmarks.methods.grep_read import GrepReadMethod
    from benchmarks.methods.whole_dump import WholeDumpMethod
    from benchmarks.query_gen import build_queries
    from benchmarks.tokenizer import get_tokenizer
    from data_olympus.index import Index

    corpus_root = Path("benchmarks/corpus")
    manifest = generate_corpus(corpus_root, n=args.n, seed=0)

    idx = Index(Path("benchmarks/bench.db"))
    idx.build(corpus_root, source_commit="manual-run")

    tokenizer = get_tokenizer(args.tokenizer)
    methods: list[object] = [
        DataOlympusMethod(idx),
        WholeDumpMethod(corpus_root),
        GrepReadMethod(corpus_root),
        Bm25Method(corpus_root, k=5),
    ]
    if args.with_rag:
        from benchmarks.methods.vector_rag import VectorRagMethod
        methods.append(VectorRagMethod(corpus_root, k=5))

    queries = build_queries(manifest)
    report = run_benchmark(
        corpus_root=corpus_root,
        idx=idx,
        queries=queries,
        methods=methods,
        tokenizer=tokenizer,
        k=5,
        curve_sizes=(25, 50, 100, 250),
    )
    write_report(report, Path("benchmarks/results"))
    print(f"Results written to benchmarks/results/ ({len(report.rows)} aggregate rows)")


if __name__ == "__main__":
    main()
