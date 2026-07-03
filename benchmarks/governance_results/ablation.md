# Governance Retrieval Ablation

k=5. Configs: fts-no-metadata, fts+description, fts+applies_when, fts+applies_when+abstain, bm25-baseline.

Recall@k carries a 95% bootstrap CI (deterministic, seeded; see `metrics.bootstrap_mean_ci`) so a reader can tell a real gap from sampling noise at each stratum size.

## Per-config x per-stratum metrics

| Config | Stratum | Recall@k [95% CI] | MRR | Miss Rate | FP Rate | Tokens | N |
|--------|---------|-------------------|-----|-----------|---------|--------|---|
| fts-no-metadata | trigger_covered | 0.667 [0.500, 0.833] | 0.607 | 0.333 | 0.000 | 297.3 | 30 |
| fts-no-metadata | paraphrase_uncovered | 0.333 [0.241, 0.437] | 0.251 | 0.667 | 0.000 | 307.8 | 87 |
| fts-no-metadata | supersession | 1.000 [1.000, 1.000] | 1.000 | 0.000 | 0.000 | 360.8 | 10 |
| fts-no-metadata | negative | 0.000 [0.000, 0.000] | 0.000 | 1.000 | 1.000 | 296.2 | 31 |
| fts-no-metadata | ALL | 0.373 [0.297, 0.449] | 0.317 | 0.627 | 0.000 | 306.9 | 158 |
| fts+description | trigger_covered | 0.667 [0.500, 0.833] | 0.607 | 0.333 | 0.000 | 297.3 | 30 |
| fts+description | paraphrase_uncovered | 0.345 [0.241, 0.448] | 0.254 | 0.655 | 0.000 | 307.3 | 87 |
| fts+description | supersession | 1.000 [1.000, 1.000] | 1.000 | 0.000 | 0.000 | 347.6 | 10 |
| fts+description | negative | 0.000 [0.000, 0.000] | 0.000 | 1.000 | 1.000 | 295.0 | 31 |
| fts+description | ALL | 0.380 [0.304, 0.456] | 0.318 | 0.620 | 0.000 | 305.5 | 158 |
| fts+applies_when | trigger_covered | 1.000 [1.000, 1.000] | 1.000 | 0.000 | 0.000 | 302.6 | 30 |
| fts+applies_when | paraphrase_uncovered | 0.414 [0.310, 0.517] | 0.266 | 0.586 | 0.000 | 309.3 | 87 |
| fts+applies_when | supersession | 1.000 [1.000, 1.000] | 1.000 | 0.000 | 0.000 | 347.6 | 10 |
| fts+applies_when | negative | 0.000 [0.000, 0.000] | 0.000 | 1.000 | 1.000 | 295.3 | 31 |
| fts+applies_when | ALL | 0.481 [0.405, 0.557] | 0.399 | 0.519 | 0.000 | 307.7 | 158 |
| fts+applies_when+abstain | trigger_covered | 1.000 [1.000, 1.000] | 1.000 | 0.000 | 0.000 | 302.6 | 30 |
| fts+applies_when+abstain | paraphrase_uncovered | 0.379 [0.276, 0.483] | 0.257 | 0.621 | 0.000 | 224.4 | 87 |
| fts+applies_when+abstain | supersession | 1.000 [1.000, 1.000] | 1.000 | 0.000 | 0.000 | 347.6 | 10 |
| fts+applies_when+abstain | negative | 0.000 [0.000, 0.000] | 0.000 | 1.000 | 0.097 | 29.9 | 31 |
| fts+applies_when+abstain | ALL | 0.462 [0.380, 0.544] | 0.395 | 0.538 | 0.000 | 208.9 | 158 |
| bm25-baseline | trigger_covered | 1.000 [1.000, 1.000] | 1.000 | 0.000 | 0.000 | 657.1 | 30 |
| bm25-baseline | paraphrase_uncovered | 0.356 [0.253, 0.460] | 0.245 | 0.644 | 0.000 | 647.6 | 87 |
| bm25-baseline | supersession | 1.000 [1.000, 1.000] | 1.000 | 0.000 | 0.000 | 690.5 | 10 |
| bm25-baseline | negative | 0.000 [0.000, 0.000] | 0.000 | 1.000 | 1.000 | 675.2 | 31 |
| bm25-baseline | ALL | 0.449 [0.373, 0.532] | 0.388 | 0.551 | 0.000 | 657.5 | 158 |

## Marginal value of applies_when

**trigger_covered** recall@5: fts-no-metadata=0.667, fts+description=0.667, fts+applies_when=1.000. Marginal gain over +description: +0.333. Marginal gain over no-metadata: +0.333.
**paraphrase_uncovered** recall@5: fts-no-metadata=0.333, fts+description=0.345, fts+applies_when=0.414. Marginal gain over +description: +0.069. Marginal gain over no-metadata: +0.080.

## Held-out (paraphrase_uncovered) — honest limit

On `paraphrase_uncovered` queries (held-out intent phrasings with NO trigger term), fts+applies_when achieves recall=0.414, mrr=0.266. Curated `applies_when` metadata does not help here because the queries contain no lexical overlap with authored trigger terms. This stratum is the honest ceiling for keyword-based retrieval; dense/semantic methods would be expected to do better.

## Negative queries — false positive / abstention

- **fts-no-metadata**: 1.000 false-positive rate on 31 negative queries (returned anything at all when no governing rule exists).
- **fts+description**: 1.000 false-positive rate on 31 negative queries (returned anything at all when no governing rule exists).
- **fts+applies_when**: 1.000 false-positive rate on 31 negative queries (returned anything at all when no governing rule exists).
- **fts+applies_when+abstain**: 0.097 false-positive rate on 31 negative queries (returned anything at all when no governing rule exists).
- **bm25-baseline**: 1.000 false-positive rate on 31 negative queries (returned anything at all when no governing rule exists).

_A governance tool should ideally abstain (return nothing) on queries with no governing rule. FP rate = 0.0 means perfect abstention; 1.0 means always returned results._
