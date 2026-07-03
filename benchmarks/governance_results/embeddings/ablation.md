# Governance Retrieval Ablation

k=5. Configs: fts-no-metadata, fts+description, fts+applies_when, fts+applies_when+abstain, bm25-baseline, fts+applies_when+embeddings, lexical-stack, lexical-stack+embeddings.

Recall@k carries a 95% bootstrap CI (deterministic, seeded; see `metrics.bootstrap_mean_ci`) so a reader can tell a real gap from sampling noise at each stratum size.

## Per-config x per-stratum metrics

| Config | Stratum | Recall@k [95% CI] | MRR | Miss Rate | FP Rate | Tokens | N |
|--------|---------|-------------------|-----|-----------|---------|--------|---|
| fts-no-metadata | trigger_covered | 0.667 [0.500, 0.833] | 0.607 | 0.333 | 0.000 | 297.3 | 30 |
| fts-no-metadata | paraphrase_uncovered | 0.270 [0.176, 0.378] | 0.189 | 0.730 | 0.000 | 306.9 | 74 |
| fts-no-metadata | supersession | 1.000 [1.000, 1.000] | 1.000 | 0.000 | 0.000 | 360.8 | 10 |
| fts-no-metadata | negative | 0.000 [0.000, 0.000] | 0.000 | 1.000 | 1.000 | 296.2 | 31 |
| fts-no-metadata | ALL | 0.345 [0.269, 0.421] | 0.291 | 0.655 | 0.000 | 306.3 | 145 |
| fts+description | trigger_covered | 0.667 [0.500, 0.833] | 0.607 | 0.333 | 0.000 | 297.3 | 30 |
| fts+description | paraphrase_uncovered | 0.284 [0.176, 0.392] | 0.195 | 0.716 | 0.000 | 306.4 | 74 |
| fts+description | supersession | 1.000 [1.000, 1.000] | 1.000 | 0.000 | 0.000 | 347.6 | 10 |
| fts+description | negative | 0.000 [0.000, 0.000] | 0.000 | 1.000 | 1.000 | 295.0 | 31 |
| fts+description | ALL | 0.352 [0.276, 0.428] | 0.294 | 0.648 | 0.000 | 304.9 | 145 |
| fts+applies_when | trigger_covered | 1.000 [1.000, 1.000] | 1.000 | 0.000 | 0.000 | 302.6 | 30 |
| fts+applies_when | paraphrase_uncovered | 0.311 [0.203, 0.419] | 0.176 | 0.689 | 0.000 | 308.0 | 74 |
| fts+applies_when | supersession | 1.000 [1.000, 1.000] | 1.000 | 0.000 | 0.000 | 347.6 | 10 |
| fts+applies_when | negative | 0.000 [0.000, 0.000] | 0.000 | 1.000 | 1.000 | 295.3 | 31 |
| fts+applies_when | ALL | 0.434 [0.352, 0.517] | 0.366 | 0.566 | 0.000 | 306.9 | 145 |
| fts+applies_when+abstain | trigger_covered | 1.000 [1.000, 1.000] | 1.000 | 0.000 | 0.000 | 302.6 | 30 |
| fts+applies_when+abstain | paraphrase_uncovered | 0.270 [0.176, 0.378] | 0.166 | 0.730 | 0.000 | 208.2 | 74 |
| fts+applies_when+abstain | supersession | 1.000 [1.000, 1.000] | 1.000 | 0.000 | 0.000 | 347.6 | 10 |
| fts+applies_when+abstain | negative | 0.000 [0.000, 0.000] | 0.000 | 1.000 | 0.097 | 29.9 | 31 |
| fts+applies_when+abstain | ALL | 0.414 [0.331, 0.497] | 0.360 | 0.586 | 0.000 | 199.2 | 145 |
| bm25-baseline | trigger_covered | 1.000 [1.000, 1.000] | 1.000 | 0.000 | 0.000 | 657.1 | 30 |
| bm25-baseline | paraphrase_uncovered | 0.243 [0.149, 0.338] | 0.126 | 0.757 | 0.000 | 645.9 | 74 |
| bm25-baseline | supersession | 1.000 [1.000, 1.000] | 1.000 | 0.000 | 0.000 | 690.5 | 10 |
| bm25-baseline | negative | 0.000 [0.000, 0.000] | 0.000 | 1.000 | 1.000 | 675.2 | 31 |
| bm25-baseline | ALL | 0.400 [0.317, 0.483] | 0.340 | 0.600 | 0.000 | 657.6 | 145 |
| fts+applies_when+embeddings | trigger_covered | 1.000 [1.000, 1.000] | 0.950 | 0.000 | 0.000 | 310.4 | 30 |
| fts+applies_when+embeddings | paraphrase_uncovered | 0.527 [0.419, 0.635] | 0.273 | 0.473 | 0.000 | 315.0 | 74 |
| fts+applies_when+embeddings | supersession | 1.000 [1.000, 1.000] | 1.000 | 0.000 | 0.000 | 355.0 | 10 |
| fts+applies_when+embeddings | negative | 0.000 [0.000, 0.000] | 0.000 | 1.000 | 1.000 | 296.5 | 31 |
| fts+applies_when+embeddings | ALL | 0.545 [0.455, 0.621] | 0.405 | 0.455 | 0.000 | 312.8 | 145 |
| lexical-stack | trigger_covered | 1.000 [1.000, 1.000] | 1.000 | 0.000 | 0.000 | 302.6 | 30 |
| lexical-stack | paraphrase_uncovered | 0.311 [0.203, 0.419] | 0.176 | 0.689 | 0.000 | 308.0 | 74 |
| lexical-stack | supersession | 1.000 [1.000, 1.000] | 1.000 | 0.000 | 0.000 | 347.6 | 10 |
| lexical-stack | negative | 0.000 [0.000, 0.000] | 0.000 | 1.000 | 1.000 | 295.3 | 31 |
| lexical-stack | ALL | 0.434 [0.352, 0.517] | 0.366 | 0.566 | 0.000 | 306.9 | 145 |
| lexical-stack+embeddings | trigger_covered | 1.000 [1.000, 1.000] | 0.950 | 0.000 | 0.000 | 310.4 | 30 |
| lexical-stack+embeddings | paraphrase_uncovered | 0.527 [0.419, 0.635] | 0.273 | 0.473 | 0.000 | 315.0 | 74 |
| lexical-stack+embeddings | supersession | 1.000 [1.000, 1.000] | 1.000 | 0.000 | 0.000 | 355.0 | 10 |
| lexical-stack+embeddings | negative | 0.000 [0.000, 0.000] | 0.000 | 1.000 | 1.000 | 296.5 | 31 |
| lexical-stack+embeddings | ALL | 0.545 [0.455, 0.621] | 0.405 | 0.455 | 0.000 | 312.8 | 145 |

## Marginal value of applies_when

**trigger_covered** recall@5: fts-no-metadata=0.667, fts+description=0.667, fts+applies_when=1.000. Marginal gain over +description: +0.333. Marginal gain over no-metadata: +0.333.
**paraphrase_uncovered** recall@5: fts-no-metadata=0.270, fts+description=0.284, fts+applies_when=0.311. Marginal gain over +description: +0.027. Marginal gain over no-metadata: +0.041.

## Held-out (paraphrase_uncovered) — honest limit

On `paraphrase_uncovered` queries (held-out intent phrasings with NO trigger term), fts+applies_when achieves recall=0.311, mrr=0.176. Curated `applies_when` metadata does not help here because the queries contain no lexical overlap with authored trigger terms. This stratum is the honest ceiling for keyword-based retrieval; dense/semantic methods would be expected to do better.

## Marginal value of embeddings: fts+applies_when+embeddings vs fts+applies_when

**trigger_covered** recall@5: 1.000 -> 1.000 (+0.000); mrr 1.000 -> 0.950 (-0.050).
**paraphrase_uncovered** recall@5: 0.311 -> 0.527 (+0.216); mrr 0.176 -> 0.273 (+0.097).
**supersession** recall@5: 1.000 -> 1.000 (+0.000); mrr 1.000 -> 1.000 (+0.000).
**negative** false-positive rate: 1.000 -> 1.000 (a dense blend can cost abstention by always having a nearest neighbour).
**ALL** recall@5: 0.434 -> 0.545 (+0.110); mrr 0.366 -> 0.405 (+0.039).

## Marginal value of embeddings: lexical-stack+embeddings vs lexical-stack

**trigger_covered** recall@5: 1.000 -> 1.000 (+0.000); mrr 1.000 -> 0.950 (-0.050).
**paraphrase_uncovered** recall@5: 0.311 -> 0.527 (+0.216); mrr 0.176 -> 0.273 (+0.097).
**supersession** recall@5: 1.000 -> 1.000 (+0.000); mrr 1.000 -> 1.000 (+0.000).
**negative** false-positive rate: 1.000 -> 1.000 (a dense blend can cost abstention by always having a nearest neighbour).
**ALL** recall@5: 0.434 -> 0.545 (+0.110); mrr 0.366 -> 0.405 (+0.039).

## Negative queries — false positive / abstention

- **fts-no-metadata**: 1.000 false-positive rate on 31 negative queries (returned anything at all when no governing rule exists).
- **fts+description**: 1.000 false-positive rate on 31 negative queries (returned anything at all when no governing rule exists).
- **fts+applies_when**: 1.000 false-positive rate on 31 negative queries (returned anything at all when no governing rule exists).
- **fts+applies_when+abstain**: 0.097 false-positive rate on 31 negative queries (returned anything at all when no governing rule exists).
- **bm25-baseline**: 1.000 false-positive rate on 31 negative queries (returned anything at all when no governing rule exists).
- **fts+applies_when+embeddings**: 1.000 false-positive rate on 31 negative queries (returned anything at all when no governing rule exists).
- **lexical-stack**: 1.000 false-positive rate on 31 negative queries (returned anything at all when no governing rule exists).
- **lexical-stack+embeddings**: 1.000 false-positive rate on 31 negative queries (returned anything at all when no governing rule exists).

_A governance tool should ideally abstain (return nothing) on queries with no governing rule. FP rate = 0.0 means perfect abstention; 1.0 means always returned results._
