# Retrieval Benchmark Report

**Tokenizer:** simple  
**RAG included:** False  
**Corpus:** synthetic (see `benchmarks/corpus/`)  

## Quantified Comparison

Per-category aggregate metrics across all benchmark queries.

| Method | Category | Mean Tokens | Recall@k | Precision | NDCG@k | MRR | Staleness Rate | N |
| --------|----------|-------------|----------|-----------|--------|----- |----------------|---|
| bm25 | exact | 513.8 | 1.000 | 0.196 | 1.000 | 1.000 | 0.000 | 225 |
| bm25 | semantic | 214.6 | 0.009 | 0.002 | 0.007 | 0.006 | 0.000 | 225 |
| bm25 | status | 529.4 | 1.000 | 0.219 | 1.000 | 1.000 | 0.000 | 25 |
| bm25 | graph | 553.8 | 1.000 | 0.209 | 0.631 | 0.500 | 1.000 | 25 |
| bm25 | ALL | 382.0 | 0.554 | 0.110 | 0.535 | 0.528 | 0.050 | 500 |
| data-olympus | exact | 181.1 | 0.858 | 0.452 | 0.858 | 0.858 | 0.000 | 225 |
| data-olympus | semantic | 44.0 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 225 |
| data-olympus | status | 44.0 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 25 |
| data-olympus | graph | 44.0 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 25 |
| data-olympus | ALL | 105.7 | 0.386 | 0.204 | 0.386 | 0.386 | 0.000 | 500 |
| grep-read | exact | 1022.4 | 0.520 | 0.100 | 0.308 | 0.304 | 0.000 | 225 |
| grep-read | semantic | 9570.6 | 0.022 | 0.001 | 0.013 | 0.016 | 0.000 | 225 |
| grep-read | status | 25560.0 | 0.000 | 0.005 | 0.000 | 0.013 | 0.000 | 25 |
| grep-read | graph | 25560.0 | 0.000 | 0.005 | 0.000 | 0.013 | 0.000 | 25 |
| grep-read | ALL | 7322.8 | 0.244 | 0.046 | 0.145 | 0.145 | 0.000 | 500 |
| whole-dump | exact | 25560.0 | 0.022 | 0.004 | 0.013 | 0.026 | 0.000 | 225 |
| whole-dump | semantic | 25560.0 | 0.022 | 0.004 | 0.013 | 0.026 | 0.000 | 225 |
| whole-dump | status | 25560.0 | 0.000 | 0.005 | 0.000 | 0.013 | 0.000 | 25 |
| whole-dump | graph | 25560.0 | 0.000 | 0.005 | 0.000 | 0.013 | 0.000 | 25 |
| whole-dump | ALL | 25560.0 | 0.020 | 0.004 | 0.012 | 0.025 | 0.000 | 500 |

## Staleness Rates

- **bm25**: staleness rate = 0.050 (fraction of queries where a superseded concept ranked above its replacement)
- **data-olympus**: staleness rate = 0.000 (fraction of queries where a superseded concept ranked above its replacement)
- **grep-read**: staleness rate = 0.000 (fraction of queries where a superseded concept ranked above its replacement)
- **whole-dump**: staleness rate = 0.000 (fraction of queries where a superseded concept ranked above its replacement)

## Token Cost vs Corpus Size

| Corpus Size | bm25 | data-olympus | grep-read | whole-dump |
|-------------|------|--------------|-----------|------------|
| 25 | 140.0 | 100.2 | 50.0 | 2208.0 |
| 50 | 215.0 | 114.4 | 125.0 | 4684.0 |
| 100 | 296.0 | 149.9 | 217.0 | 9836.0 |
| 250 | 325.2 | 171.2 | 490.5 | 25560.0 |

### Where data-olympus loses

On **semantic** (paraphrase) queries, data-olympus achieves recall=0.000, ndcg=0.000. This is the category where dense vector search has the largest advantage, because paraphrases lack the keyword overlap that the BM25-based index relies on.
- **bm25** semantic: recall=0.009, ndcg=0.007
- **grep-read** semantic: recall=0.022, ndcg=0.013
- **whole-dump** semantic: recall=0.022, ndcg=0.013
