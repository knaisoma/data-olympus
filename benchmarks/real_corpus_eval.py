"""Real-corpus retrieval eval: lexical stack vs local-embedding hybrid.

Point this at YOUR own KB directory and a query set to measure what the optional
embedding hybrid (issue #42) adds over the lexical stack (FTS + synonym +
co-occurrence expansion) on your corpus. It is deliberately content-free: it
prints only aggregate metrics (recall@k, MRR, recovered/regressed counts), never
document text, so a run over a private KB leaks nothing.

    uv run --extra embeddings python -m benchmarks.real_corpus_eval \
        --corpus path/to/kb --queries queries.json [--k 5]

``queries.json`` is a list of ``{"text": "...", "gold_ids": ["ID", ...]}``. A
query with an empty ``gold_ids`` is skipped (unlabeled). See
``benchmarks/real_corpus_eval.md`` for a worked example and an honest reading of
what the hybrid does and does not buy you.

The ``embeddings`` extra (fastembed) is required; the first run fetches a small
local ONNX model (cached thereafter). No network at query time.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

from benchmarks.metrics import mrr, recall_at_k


def _build_expanders(index):  # noqa: ANN001 - Index, avoid import at module load
    from data_olympus.cooccurrence import (
        DEFAULT_MAX_TERMS,
        compose_expanders,
        cooccurrence_enabled,
    )
    from data_olympus.query_expansion import default_query_expander

    cooc = index.cooccurrence_expander() if cooccurrence_enabled() else None
    return compose_expanders(default_query_expander(), cooc, max_terms=DEFAULT_MAX_TERMS)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpus", required=True, help="KB directory to index")
    ap.add_argument("--queries", required=True, help="JSON list of {text, gold_ids}")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--model", default="BAAI/bge-small-en-v1.5")
    ap.add_argument("--weight", type=float, default=0.35, help="cosine fraction of the blend")
    ap.add_argument("--dense-count", type=int, default=10)
    ap.add_argument("--min-cosine", type=float, default=0.5)
    args = ap.parse_args()

    import tempfile

    from data_olympus.embeddings import EmbeddingsConfig, build_embedder
    from data_olympus.index import Index

    corpus = Path(args.corpus)
    raw = json.loads(Path(args.queries).read_text(encoding="utf-8"))
    queries = [q for q in raw if q.get("gold_ids")]
    print(f"corpus={corpus} queries={len(raw)} (labeled={len(queries)}) k={args.k}")
    if not queries:
        sys.exit("no labeled queries found: every entry has empty/missing gold_ids")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        lex = Index(tmp_path / "lex.db")
        n = lex.build(corpus, source_commit="real-corpus-eval").docs_indexed
        lex.query_expander = _build_expanders(lex)
        print(f"indexed {n} docs")

        cfg = EmbeddingsConfig(model_name=args.model, weight=args.weight)
        embedder = build_embedder(cfg)
        hyb = Index(
            tmp_path / "hyb.db", embeddings=cfg, embedder=embedder,
            dense_candidate_count=args.dense_count, dense_min_cosine=args.min_cosine,
        )
        hyb.build(corpus, source_commit="real-corpus-eval")
        hyb.query_expander = _build_expanders(hyb)
        hyb.reranker = hyb.make_hybrid_reranker(embedder, weight=cfg.weight)

        lex_recall: list[float] = []
        lex_mrr: list[float] = []
        hyb_recall: list[float] = []
        hyb_mrr: list[float] = []
        recovered = regressed = 0
        for q in queries:
            gold = set(q["gold_ids"])
            lr = [h.id for h in lex.search(q["text"], limit=args.k)]
            hr = [h.id for h in hyb.search(q["text"], limit=args.k)]
            lrec, hrec = recall_at_k(lr, gold, k=args.k), recall_at_k(hr, gold, k=args.k)
            lex_recall.append(lrec)
            hyb_recall.append(hrec)
            lex_mrr.append(mrr(lr, gold))
            hyb_mrr.append(mrr(hr, gold))
            recovered += int(hrec > lrec)
            regressed += int(hrec < lrec)

    lr_m, hr_m = statistics.mean(lex_recall), statistics.mean(hyb_recall)
    lm_m, hm_m = statistics.mean(lex_mrr), statistics.mean(hyb_mrr)
    print("\n== lexical stack vs hybrid (+embeddings), default config ==")
    print(f"  recall@{args.k}: {lr_m:.3f} -> {hr_m:.3f} ({hr_m - lr_m:+.3f})")
    # MRR is computed over the k-truncated ranking (limit=k above), i.e. MRR@k.
    print(f"  MRR@{args.k}:     {lm_m:.3f} -> {hm_m:.3f} ({hm_m - lm_m:+.3f})")
    print(f"  hybrid recovered: {recovered}/{len(queries)}   regressed: {regressed}/{len(queries)}")


if __name__ == "__main__":
    main()
