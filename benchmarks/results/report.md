# Retrieval Benchmark Report

**Tokenizer:** simple  
**RAG included:** False  
**Corpus:** synthetic (see `benchmarks/corpus/`)  

## Quantified Comparison

Per-category aggregate metrics across all benchmark queries.

Ranking metrics (Recall@k / NDCG@k / MRR) are reported only for methods that produce a query-dependent ranking. **whole-dump** does not rank (it returns every doc in fixed file order for every query), so its ranking cells read `n/a`; it is honestly comparable only on token cost, precision, and Contains-Gold (order-free: is a gold doc anywhere in the payload). Means carry a 95% bootstrap CI (deterministic, seeded; see `metrics.bootstrap_mean_ci`).

| Method | Category | Mean Tokens | Norm Tokens | Recall@k [95% CI] | Contains-Gold | Precision | NDCG@k | MRR | Staleness | N |
|---|---|---|---|---|---|---|---|---|---|---|
| bm25 | exact | 542.0 | 106.6 | 1.000 [1.000, 1.000] | 1.000 | 0.197 | 1.000 | 1.000 | 0.000 | 217 |
| bm25 | semantic | 272.1 | 52.2 | 0.014 [0.000, 0.032] | 0.014 | 0.003 | 0.009 | 0.007 | 0.000 | 217 |
| bm25 | status | 577.2 | 121.9 | 1.000 [1.000, 1.000] | 1.000 | 0.211 | 1.000 | 1.000 | 0.000 | 33 |
| bm25 | graph | 577.2 | 121.9 | 1.000 [1.000, 1.000] | 1.000 | 0.211 | 1.000 | 1.000 | 0.000 | 33 |
| bm25 | ALL | 429.5 | 85.0 | 0.572 [0.530, 0.614] | 0.572 | 0.115 | 0.570 | 0.569 | 0.000 | 500 |
| bm25-status-aware | exact | 533.5 | 106.6 | 1.000 [1.000, 1.000] | 1.000 | 0.200 | 1.000 | 1.000 | 0.000 | 217 |
| bm25-status-aware | semantic | 271.9 | 52.2 | 0.014 [0.000, 0.032] | 0.014 | 0.003 | 0.009 | 0.007 | 0.000 | 217 |
| bm25-status-aware | status | 561.2 | 121.9 | 1.000 [1.000, 1.000] | 1.000 | 0.217 | 1.000 | 1.000 | 0.000 | 33 |
| bm25-status-aware | graph | 561.2 | 121.9 | 1.000 [1.000, 1.000] | 1.000 | 0.217 | 1.000 | 1.000 | 0.000 | 33 |
| bm25-status-aware | ALL | 423.6 | 85.0 | 0.572 [0.530, 0.614] | 0.572 | 0.117 | 0.570 | 0.569 | 0.000 | 500 |
| data-olympus | exact | 351.6 | 106.6 | 1.000 [1.000, 1.000] | 1.000 | 0.303 | 1.000 | 1.000 | 0.000 | 217 |
| data-olympus | semantic | 239.0 | 63.7 | 0.037 [0.014, 0.065] | 0.037 | 0.012 | 0.021 | 0.015 | 0.000 | 217 |
| data-olympus | status | 411.8 | 121.9 | 1.000 [1.000, 1.000] | 1.000 | 0.296 | 1.000 | 1.000 | 0.000 | 33 |
| data-olympus | graph | 384.2 | 121.9 | 1.000 [1.000, 1.000] | 1.000 | 0.317 | 1.000 | 1.000 | 0.000 | 33 |
| data-olympus | ALL | 308.9 | 90.0 | 0.582 [0.538, 0.626] | 0.582 | 0.177 | 0.575 | 0.573 | 0.000 | 500 |
| grep-read | exact | 1233.5 | 115.5 | 0.442 [0.378, 0.507] | 1.000 | 0.097 | 0.273 | 0.289 | 0.000 | 217 |
| grep-read | semantic | 10454.2 | 66.8 | 0.032 [0.009, 0.055] | 0.369 | 0.002 | 0.018 | 0.022 | 0.000 | 217 |
| grep-read | status | 27166.0 | 122.5 | 1.000 [1.000, 1.000] | 1.000 | 0.004 | 0.828 | 0.768 | 0.000 | 33 |
| grep-read | graph | 27166.0 | 122.5 | 1.000 [1.000, 1.000] | 1.000 | 0.004 | 0.828 | 0.768 | 0.000 | 33 |
| grep-read | ALL | 8658.4 | 95.3 | 0.338 [0.296, 0.378] | 0.726 | 0.043 | 0.236 | 0.236 | 0.000 | 500 |
| whole-dump | exact | 27166.0 | 110.0 | n/a | 1.000 | 0.004 | n/a | n/a | 0.000 | 217 |
| whole-dump | semantic | 27166.0 | 110.0 | n/a | 1.000 | 0.004 | n/a | n/a | 0.000 | 217 |
| whole-dump | status | 27166.0 | 110.0 | n/a | 1.000 | 0.004 | n/a | n/a | 0.000 | 33 |
| whole-dump | graph | 27166.0 | 110.0 | n/a | 1.000 | 0.004 | n/a | n/a | 0.000 | 33 |
| whole-dump | ALL | 27166.0 | 110.0 | n/a | 1.000 | 0.004 | n/a | n/a | 0.000 | 500 |

### Token cost: as-shipped payload vs normalized policy

**Mean Tokens** is each method's as-shipped payload (bm25: top-5 chunks; data-olympus: outline + snippets + 1 full doc; whole-dump: the whole corpus). **Norm Tokens** charges every method the SAME normalized policy — the token cost of its top-1 retrieved document body only — so the column isolates retrieval quality from response-shaping convention. Compare methods on Norm Tokens to remove the payload-policy confound; compare on Mean Tokens to see the real per-call cost a caller pays today.

## Staleness avoidance

Two metrics, over lifecycle queries that target a supersession topic:

- **Serves-Stale rate** (headline): fraction of those queries where the superseded doc appeared ANYWHERE in the top-k payload. This is the real governance harm (a retired rule reaching the agent) and is tiebreak-independent. A retriever with a status/in-force filter is 0.000 here by construction; a status-blind keyword method serves the stale doc whenever it retrieves the topic.
- **Staleness rate** (secondary): fraction where the superseded doc ranked at-or-above its replacement. In the de-leaked corpus the old and new doc are lexically identical, so a status-blind ranker ties them and this number depends on an arbitrary tiebreak; Serves-Stale is the honest signal.

- **bm25**: serves-stale = 0.750 (n=132 lifecycle queries), staleness = 0.000
- **bm25-status-aware**: serves-stale = 0.000 (n=132 lifecycle queries), staleness = 0.000
- **data-olympus**: serves-stale = 0.000 (n=132 lifecycle queries), staleness = 0.000
- **grep-read**: serves-stale = 0.833 (n=132 lifecycle queries), staleness = 0.000
- **whole-dump**: serves-stale = 1.000 (n=132 lifecycle queries), staleness = 0.000

## Token Cost vs Corpus Size

| Corpus Size | bm25 | bm25-status-aware | data-olympus | grep-read | whole-dump |
|-------------|------|-------------------|--------------|-----------|------------|
| 25 | 150.5 | 139.6 | 163.2 | 53.0 | 2278.0 |
| 50 | 199.5 | 188.6 | 181.5 | 102.0 | 4958.0 |
| 100 | 301.6 | 289.4 | 218.0 | 229.0 | 10436.0 |
| 250 | 342.1 | 340.0 | 236.2 | 502.8 | 26848.0 |

### Where data-olympus loses

On **semantic** (paraphrase) queries, data-olympus achieves recall=0.037, ndcg=0.021. This is the category where dense vector search has the largest advantage, because paraphrases lack the keyword overlap that the BM25-based index relies on.
- **bm25** semantic: recall=0.014, ndcg=0.009
- **bm25-status-aware** semantic: recall=0.014, ndcg=0.009
- **grep-read** semantic: recall=0.032, ndcg=0.018
