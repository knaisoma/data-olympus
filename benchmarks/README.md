# Retrieval Benchmark Harness

A reproducible, retrieval-only benchmark that measures token cost and retrieval
accuracy of five methods over a synthetic committed corpus, plus a governance
ablation and one reproducible run on a real (non-templated) corpus.

## What it measures

For each (method, query) pair the harness records:

- **Mean tokens:** as-shipped payload size sent to the agent (lower is cheaper).
- **Norm tokens:** token cost under a NORMALIZED payload policy — every method is
  charged the cost of its top-1 full document body only. This removes the
  per-method response-shaping convention (bm25 returns 5 chunks, data-olympus
  returns outline+snippets+1 doc, whole-dump dumps everything) so token cost can
  be compared for *retrieval*, not payload style. Reported alongside Mean Tokens,
  never instead of it.
- **Recall@k:** fraction of gold concepts in the top-k ranked results. Reported
  ONLY for methods that produce a query-dependent ranking (see below).
- **Contains-Gold:** order-free — is a gold concept present ANYWHERE in the
  payload? This is the only recall-like axis fair to a non-ranking method.
- **Precision:** share of the payload that is the relevant concept.
- **NDCG@k and MRR:** ranking quality metrics (ranking methods only).
- **Serves-Stale rate (headline lifecycle metric):** fraction of supersession-
  topic queries where the superseded doc reached the payload at all. This is the
  real governance harm and is tiebreak-independent.
- **Staleness rate (secondary):** fraction of queries where the superseded
  concept ranked at-or-above its replacement. In the de-leaked corpus the old and
  new doc are lexically identical, so a status-blind ranker ties them and this
  number depends on an arbitrary tiebreak; **Serves-Stale is the honest signal.**

Every reported mean carries a **95% bootstrap confidence interval** (percentile
bootstrap, deterministic and seeded via `metrics.bootstrap_mean_ci`), so a reader
can tell a real gap from sampling noise at each stratum size.

Queries are grouped into four categories:

- `exact` — literal topic term (e.g. "caching")
- `semantic` — paraphrase with low literal overlap (e.g. "storing computed results to avoid recomputation")
- `status` — "current rule for \<topic\>" for topics that have a superseded predecessor
- `graph` — "what replaced the previous \<topic\> guidance"

## Honesty caveats

**The corpus is SYNTHETIC.** It is deterministically generated (`n=250, seed=0`)
and does not represent any real knowledge base. It exists to exercise scale,
supersession chains, and type/status diversity under controlled, reproducible
conditions. For one number that is NOT templated, see "Real-corpus example" below.

**De-leaking (0.3.0).** The earlier corpus wrote the lifecycle words a query
searched for straight into the doc it was meant to retrieve: stale docs said
"previous", current docs said "current", and titles carried "(old)"/"(current)"
qualifiers — the exact words the `status`/`graph` queries used. A keyword method
could then "win" those categories by echoing a string. That leak is removed: the
lifecycle signal now lives ONLY in the `status` frontmatter and the
`supersedes`/`superseded_by` chain, the body prose is lifecycle-neutral and
identical across the old/new pair, and every body mixes in a pool of shared
distractor vocabulary so a query term is not a near-unique fingerprint of its gold
doc. **Known remaining leak (documented on purpose):** the `exact` category still
echoes the topic word, which also appears in the doc title and body. That is
intentional — `exact` is the literal-lookup category and is not claimed to measure
anything harder than keyword matching. No other category shares answer vocabulary
with its gold doc.

**Non-ranking methods are not scored on ranking metrics.** `whole-dump` returns
every doc in fixed file-walk order for every query — it does not rank — so scoring
it on recall@k/NDCG/MRR is meaningless. It carries `ranks = False` and its ranking
cells read `n/a`; it is reported only on token cost, precision, and Contains-Gold.
`grep-read` now ranks by descending query-term match count (a real, if crude,
grep-hit-count signal) instead of the alphabetical file order it emitted before,
which had made its ranking metrics meaningless.

**The tokenizer in the committed run is the dep-free `SimpleTokenizer`** (word
runs and individual punctuation marks; `re.compile(r"\w+|[^\w\s]")`). This
tokenizer is not a BPE tokenizer. Token *ratios* across methods are
tokenizer-robust (each method's payload is counted with the same tokenizer);
absolute token counts are specific to this simple tokenizer and will differ
from `tiktoken`/`cl100k_base` counts.

**Vector-RAG was NOT included in the committed baseline run** because the
`[bench]` optional dependencies are absent from the CI install (`.[dev]` only).
Vector-RAG is expected to outperform all other methods on the `semantic` query
category. The optional local-embedding hybrid IS measured separately in the
governance ablation (see `generate_embeddings_ablation.py`), where it lifts
held-out paraphrase recall materially over the lexical stack.

## Methods

| Name | Ranks? | Description |
|---|---|---|
| `data-olympus` | yes | Real `Index.search(query, in_force=True)` (in-force status class: active/accepted/approved) with outline + snippet payload |
| `bm25` | yes | BM25 over 512-token whitespace chunks; top-5 chunks as payload; status-blind |
| `bm25-status-aware` | yes | Same BM25 ranker, but reads `status` frontmatter and skips superseded/deprecated docs. Isolates "the win is the governance metadata" from "the engine is better" |
| `grep-read` | yes | Match files by keyword; ranked by descending match count |
| `whole-dump` | no | Concatenate every file in the corpus; no query-dependent ranking |
| `vector-rag` | yes | Dense cosine retrieval over embedded chunks (requires `[bench]`) |

## How to run

### Baseline run (no extra deps)

```bash
# Regenerate committed artifacts (overwrites benchmarks/corpus/, queries.yaml, results/)
uv run python -m benchmarks.generate_artifacts
```

### Richer run with tiktoken and optional vector-RAG

```bash
uv pip install -e '.[bench]'   # enables tiktoken + sentence-transformers + numpy
uv run python -m benchmarks.run --tokenizer tiktoken --with-rag
```

### CLI options for benchmarks.run

```
--tokenizer simple|tiktoken   tokenizer to use (default: simple)
--n 250                       corpus size (default: 250)
--with-rag                    include the vector-RAG method (requires [bench])
```

Results are written to `benchmarks/results/results.json` (machine-readable) and
`benchmarks/results/report.md` (human-readable).

## Governance ablation

A separate ablation measures whether curated `applies_when` trigger metadata
earns its keep, over a governance corpus of 30 governing topics (each with a
distinct held-out trigger vocabulary), 10 supersession pairs, and 31 distractor
topics with no governing rule, across 158 stratified queries. Strata are grown so
each has a non-degenerate bootstrap CI (trigger_covered n=30, negative n=31,
supersession n=10, paraphrase_uncovered n≈87).

```bash
uv run python -m benchmarks.generate_governance_artifacts       # lexical ablation
KB_EMBEDDINGS_MODE=on uv run --extra embeddings \
    python -m benchmarks.generate_embeddings_ablation           # + embedding hybrid
```

## Real-corpus example (not templated)

Every synthetic number exists to exercise scale under control, not to stand in
for real content. For one reproducible number from a real, hand-authored corpus,
the lexical stack is run against the committed `example-bundle` (18 governance
docs) with 9 labeled paraphrase queries (both committed):

```bash
uv run python -m benchmarks.real_corpus_eval \
    --corpus example-bundle \
    --queries benchmarks/real_corpus/example_bundle_queries.json \
    --lexical-only --out benchmarks/real_corpus/example_bundle_result.json
```

Provenance: hand-authored paraphrase queries that avoid each doc's distinctive
title terms; embeddings off; illustrative, not user traffic. The committed result
is `benchmarks/real_corpus/example_bundle_result.json`.

## Docs stay in sync automatically

The benchmark numbers quoted in `docs/comparison.md` and `WHY.md` are GENERATED
from the committed result JSONs by `benchmarks/docs_tables.py`, between
`<!-- BENCH:<name> START/END -->` markers. A CI guard fails the build if any
doc number drifts from the results:

```bash
uv run python -m benchmarks.docs_tables --write   # refresh doc tables
uv run python scripts/check_benchmark_docs.py     # CI drift guard (fails on drift)
```

## Reproducing the committed results

```bash
uv run python -m benchmarks.generate_artifacts
uv run python -m benchmarks.generate_governance_artifacts
uv run python -m benchmarks.real_corpus_eval --corpus example-bundle \
    --queries benchmarks/real_corpus/example_bundle_queries.json \
    --lexical-only --out benchmarks/real_corpus/example_bundle_result.json
uv run python -m benchmarks.docs_tables --write
# Committed results were produced with:
#   synthetic: n=250, seed=0, tokenizer=simple, no vector-RAG, curve_sizes=(25,50,100,250)
#   governance: n=120 (caps at 30 topics), seed=0, tokenizer=simple
```

The committed `benchmarks/results/report.md` contains the actual numbers cited
in `docs/comparison.md § Quantified comparison`.
