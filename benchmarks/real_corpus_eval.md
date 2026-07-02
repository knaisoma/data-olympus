# Real-corpus retrieval eval: lexical vs embedding-hybrid

`real_corpus_eval.py` measures what the optional local-embedding hybrid (issue
#42) adds over the lexical stack (FTS + synonym + co-occurrence expansion) **on
your own KB**. It prints only aggregate metrics, never document text, so it is
safe to run over a private corpus.

## Run it on your KB

```bash
uv run --extra embeddings python -m benchmarks.real_corpus_eval \
    --corpus /path/to/your/kb --queries queries.json --k 5
```

`queries.json` is a list of labeled queries:

```json
[
  {"text": "how do we encrypt config secrets at rest", "gold_ids": ["GDEC-004"]},
  {"text": "which role owns the tenant-isolation helpers", "gold_ids": ["STD-BN-201"]}
]
```

`gold_ids` are the document ids the query should retrieve (the same ids
`kb_search`/`kb_get` return). Build the set however you like; the more your
queries paraphrase the target docs (different words, same intent) the more they
test semantic retrieval rather than keyword matching.

## Worked example (our internal KB)

An illustrative run on our own knowledge base. This is a **private-corpus
example**, not an independently reproducible benchmark: the corpus cannot be
shared and the queries are LLM-authored. Run the harness on your KB for a number
that reflects your content.

- Corpus: 243 indexed governance docs.
- Queries: 215, one per doc, each a natural-language **paraphrase** written to
  avoid the doc's distinctive title terms (to create a lexical gap), applied
  uniformly with no retriever-in-the-loop tuning.
- Config: defaults (`--weight 0.35 --dense-count 10 --min-cosine 0.5`), no
  hyperparameter tuning.

| Metric | Lexical stack | Hybrid (+embeddings) | Delta |
|---|---|---|---|
| recall@5 | 0.898 | 0.916 | +0.019 |
| recall@10 | 0.953 | 0.967 | +0.014 |
| MRR | 0.756 | 0.770 | +0.013 |

Hybrid **recovered 4 / 215** queries at k=5 and **regressed 0 / 215**. Three of
the four recoveries were token-disjoint queries (zero title-token overlap with
their gold doc) — the case dense retrieval is meant to help.

## Honest reading

- **The lexical stack is already strong** (~0.90 recall@5, ~0.95 recall@10) on
  this corpus, even against deliberately paraphrased queries, because the target
  concepts still surface in document bodies. That leaves little headroom.
- **The embedding hybrid is a real but small, strictly non-harmful add** here:
  it fixes the hardest token-disjoint queries and regressed nothing, but the
  aggregate lift is ~2 points of recall (4 queries out of 215).
- **So it ships as an opt-in, not a default.** On a corpus like this, the model
  dependency, build-time embedding, and reduced interpretability are not worth a
  ~2-point lift. Enable it (`KB_EMBEDDINGS_MODE`) when your corpus has genuine
  semantic gaps — short, jargon-light, or heavily-paraphrased queries where the
  target words rarely appear in the docs — and measure with this harness first.

Caveats: n and the number of recoveries are small (4 events); the queries are
LLM-authored paraphrases, not real user traffic; a larger local model or tuned
threshold might move the number. The reproducible synthetic ablation lives in
`benchmarks/governance_results/` and `benchmarks/generate_embeddings_ablation.py`.
