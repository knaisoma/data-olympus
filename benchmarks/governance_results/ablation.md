# Governance Retrieval Ablation

k=5. Four configs: fts-no-metadata, fts+description, fts+applies_when (production), bm25-baseline.

## Per-config x per-stratum metrics

| Config | Stratum | Recall@k | MRR | Miss Rate | FP Rate | Tokens | N |
|--------|---------|----------|-----|-----------|---------|--------|---|
| fts-no-metadata | trigger_covered | 0.867 | 0.719 | 0.133 | 0.000 | 295.9 | 15 |
| fts-no-metadata | paraphrase_uncovered | 0.429 | 0.306 | 0.571 | 0.000 | 308.0 | 42 |
| fts-no-metadata | supersession | 1.000 | 1.000 | 0.000 | 0.000 | 358.0 | 3 |
| fts-no-metadata | negative | 0.000 | 0.000 | 1.000 | 1.000 | 299.0 | 5 |
| fts-no-metadata | ALL | 0.523 | 0.410 | 0.477 | 0.000 | 306.9 | 65 |
| fts+description | trigger_covered | 0.867 | 0.719 | 0.133 | 0.000 | 295.9 | 15 |
| fts+description | paraphrase_uncovered | 0.452 | 0.311 | 0.548 | 0.000 | 306.4 | 42 |
| fts+description | supersession | 1.000 | 1.000 | 0.000 | 0.000 | 350.0 | 3 |
| fts+description | negative | 0.000 | 0.000 | 1.000 | 1.000 | 296.0 | 5 |
| fts+description | ALL | 0.538 | 0.413 | 0.462 | 0.000 | 305.2 | 65 |
| fts+applies_when | trigger_covered | 1.000 | 1.000 | 0.000 | 0.000 | 299.8 | 15 |
| fts+applies_when | paraphrase_uncovered | 0.452 | 0.311 | 0.548 | 0.000 | 306.6 | 42 |
| fts+applies_when | supersession | 1.000 | 1.000 | 0.000 | 0.000 | 350.0 | 3 |
| fts+applies_when | negative | 0.000 | 0.000 | 1.000 | 1.000 | 296.0 | 5 |
| fts+applies_when | ALL | 0.569 | 0.478 | 0.431 | 0.000 | 306.2 | 65 |
| bm25-baseline | trigger_covered | 1.000 | 1.000 | 0.000 | 0.000 | 663.1 | 15 |
| bm25-baseline | paraphrase_uncovered | 0.405 | 0.260 | 0.595 | 0.000 | 659.6 | 42 |
| bm25-baseline | supersession | 1.000 | 1.000 | 0.000 | 0.000 | 684.0 | 3 |
| bm25-baseline | negative | 0.000 | 0.000 | 1.000 | 1.000 | 688.0 | 5 |
| bm25-baseline | ALL | 0.538 | 0.445 | 0.462 | 0.000 | 663.7 | 65 |

## Marginal value of applies_when

**trigger_covered** recall@5: fts-no-metadata=0.867, fts+description=0.867, fts+applies_when=1.000. Marginal gain over +description: +0.133. Marginal gain over no-metadata: +0.133.
**paraphrase_uncovered** recall@5: fts-no-metadata=0.429, fts+description=0.452, fts+applies_when=0.452. Marginal gain over +description: +0.000. Marginal gain over no-metadata: +0.024.

## Held-out (paraphrase_uncovered) — honest limit

On `paraphrase_uncovered` queries (held-out intent phrasings with NO trigger term), fts+applies_when achieves recall=0.452, mrr=0.311. Curated `applies_when` metadata does not help here because the queries contain no lexical overlap with authored trigger terms. This stratum is the honest ceiling for keyword-based retrieval; dense/semantic methods would be expected to do better.

## Negative queries — false positive / abstention

- **fts-no-metadata**: 1.000 false-positive rate on 5 negative queries (returned anything at all when no governing rule exists).
- **fts+description**: 1.000 false-positive rate on 5 negative queries (returned anything at all when no governing rule exists).
- **fts+applies_when**: 1.000 false-positive rate on 5 negative queries (returned anything at all when no governing rule exists).
- **bm25-baseline**: 1.000 false-positive rate on 5 negative queries (returned anything at all when no governing rule exists).

_A governance tool should ideally abstain (return nothing) on queries with no governing rule. FP rate = 0.0 means perfect abstention; 1.0 means always returned results._
