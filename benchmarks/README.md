# Retrieval Benchmark Harness

A reproducible, retrieval-only benchmark that measures token cost and retrieval
accuracy of four methods over a synthetic committed corpus.

## What it measures

For each (method, query) pair the harness records:

- **Mean tokens:** payload size sent to the agent (lower is cheaper per call)
- **Recall@k:** fraction of gold concepts found in the top-k ranked results
- **Precision:** share of the payload that is the relevant concept (signal-to-noise)
- **NDCG@k and MRR:** ranking quality metrics
- **Staleness rate:** fraction of queries where a superseded concept ranked
  above its current replacement (staleness avoidance)

Queries are grouped into four categories:

- `exact` — literal topic term (e.g. "caching")
- `semantic` — paraphrase with low literal overlap (e.g. "storing computed results to avoid recomputation")
- `status` — "current rule for \<topic\>" for topics that have a superseded predecessor
- `graph` — "what replaced the previous \<topic\> guidance"

## Honesty caveats

**The corpus is SYNTHETIC.** It is deterministically generated (`n=250, seed=0`)
and does not represent any real knowledge base. It exists to exercise scale,
supersession chains, and type/status diversity under controlled, reproducible
conditions.

**The tokenizer in the committed run is the dep-free `SimpleTokenizer`** (word
runs and individual punctuation marks; `re.compile(r"\w+|[^\w\s]")`). This
tokenizer is not a BPE tokenizer. Token *ratios* across methods are
tokenizer-robust (each method's payload is counted with the same tokenizer);
absolute token counts are specific to this simple tokenizer and will differ
from `tiktoken`/`cl100k_base` counts.

**Vector-RAG was NOT included in the committed baseline run** because the
`[bench]` optional dependencies (`sentence-transformers`, `tiktoken`, `numpy`)
are absent from the CI install (`.[dev]` only). Vector-RAG is expected to
outperform all other methods on the `semantic` query category because paraphrase
queries lack keyword overlap, which is what the BM25-based and FTS indexes rely
on. The `vector_rag.py` adapter is implemented and available behind `[bench]`.

## Methods

| Name | Description |
|---|---|
| `data-olympus` | Real `Index.search(query, status="active")` with outline + snippet payload |
| `whole-dump` | Concatenate every file in the corpus; no ranking |
| `grep-read` | Match files by keyword; concatenate matched files |
| `bm25` | BM25 over 512-token whitespace chunks; top-5 chunks as payload |
| `vector-rag` | Dense cosine retrieval over embedded chunks (requires `[bench]`) |

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

## Reproducing the committed results

```bash
uv run python -m benchmarks.generate_artifacts
# Committed results were produced with:
#   n=250, seed=0, tokenizer=simple, no vector-RAG, curve_sizes=(25,50,100,250)
```

The committed `benchmarks/results/report.md` contains the actual numbers cited
in `docs/comparison.md § Quantified comparison`.
