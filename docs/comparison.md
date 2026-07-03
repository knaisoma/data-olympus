# Comparison: data-olympus and its neighbours

## What data-olympus is

data-olympus is a governance-grade knowledge-base **format** (a profile designed to be readable by Open Knowledge Format consumers, with governance extensions layered on top) plus a single-writer MCP server and a CLI. The format adds a controlled vocabulary on top of the Open Knowledge Format's minimal `type` field: a stable `id`, required `status` and `tier` fields, a `supersedes`/`superseded_by` chain for decisions, and a normative cross-linking convention. The result is a git-native, human and agent readable document graph that can be served directly to an agent workforce.

The system is optimized for one specific job: a small team of agents (and humans) curating engineering standards, architectural decisions, and project knowledge as version-controlled markdown. It is not a general data catalog, not a vector store, and not a wiki platform. It is also deliberately not a code-search, reference-finding, or "where is X used" tool (LSP, grep, and Sourcegraph own that, and it does not compete with them). The retrieval task it targets is **coding-intent to governing-rule**: surfacing the established standard or decision that should govern a choice the model is about to make. Understanding that scope is the clearest lens for reading the comparisons below.

---

## Summary table

| Tool / category | Portability / lock-in | Human + agent readable (no SDK) | Governance and multi-agent write-safety | Structured queryability | Concurrency model | Taxonomy | Hosting model | Interop |
|---|---|---|---|---|---|---|---|---|
| **data-olympus** | git-native / none | yes, plain markdown | single-writer MCP pipeline | FTS + filter by status/tier/type | single-writer, advisory locks | controlled vocabulary (type, status, tier) | self-hosted, streamable HTTP | designed to be OKF-readable (conformance test not yet built, [issue #82](https://github.com/knaisoma/data-olympus/issues/82)) |
| Google OKF | git-native / none | yes | FTS only, no write governance | FTS only | none specified | minimal (type only) | any | OKF native |
| Enterprise data catalogs (Dataplex, Unity Catalog, Collibra, DataHub, Amundsen) | vendor / high | partial (UI-centric) | strong (RBAC, lineage) | deep (column-level, lineage graphs) | multi-writer | rich, auto-generated | SaaS / self-hosted | APIs, connectors |
| Markdown KB tools (Obsidian, Notion, MkDocs, Backstage TechDocs) | varies / medium | yes | none | FTS or plugin-based | multi-writer | user-defined | SaaS / local | plugin ecosystem |
| Agent-context conventions (llms.txt, .cursorrules, AGENTS.md) | file-based / none | yes | none | none | none | none | none (static files) | any reader |
| Memory / RAG (vector DB, MCP memory servers, graph RAG) | embedding-dependent / medium-high | no (embedding layer) | none to partial | semantic, high recall | varies | auto-generated embeddings | self-hosted or cloud | varies |
| ADR tooling (adr-tools, Log4brains) | git-native / low | yes | none | ADR-scoped only | none | ADR-specific | static site or local | markdown |

---

## Quantified comparison

Methodology: a synthetic corpus of 250 concepts (deterministic, `seed=0`) generated across all tiers and types, including supersession pairs. Five retrieval methods run over 500 queries in four categories (`exact`, `semantic`, `status`, `graph`). Token counts use the dep-free `SimpleTokenizer`; token *ratios* across methods are tokenizer-robust, absolute counts are tokenizer-specific. See [`benchmarks/README.md`](../benchmarks/README.md) for the full methodology, the de-leaking done to the corpus, and remaining known leaks.

The numbers below are generated directly from [`benchmarks/results/results.json`](../benchmarks/results/results.json) by `benchmarks/docs_tables.py`; a CI guard (`scripts/check_benchmark_docs.py`) fails the build if any quoted number drifts from the committed results, so this section cannot go stale by hand. Regenerate everything with `python -m benchmarks.generate_artifacts && python -m benchmarks.docs_tables --write`.

**Corpus: SYNTHETIC (generated). Tokenizer: SimpleTokenizer (dep-free).**

### What changed in this re-cut (0.3.0), and why

This section was re-derived under a stricter methodology than the 0.2.0 numbers. Three changes materially moved the numbers, and honesty is this project's credibility strategy, so we call each out:

1. **The `in_force` filter fix (benchmark bug B1).** The harness previously filtered data-olympus results to `status="active"`, which silently excluded `accepted` gold decision docs and produced a deflated exact recall of **0.858**. The deployable `in_force` filter (active/accepted/approved) is now used, and exact recall rises to **1.000**. This is a genuine improvement from fixing a measurement bug, not a change to the engine.
2. **Two honest baselines added.** A **status-aware BM25** (reads `status` frontmatter, skips superseded/deprecated docs) isolates "the win is having governance metadata" from "the engine is better". And **whole-dump / grep-read are no longer scored on ranking metrics they cannot support** — whole-dump does not rank at all (it returns every doc in fixed order for every query), so it is reported only on token cost and the order-free Contains-Gold axis; grep-read now ranks by real match-count order instead of the alphabetical file order it used before (which made its old recall/NDCG/MRR meaningless).
3. **The corpus was de-leaked.** The old corpus wrote the lifecycle words the query searched for straight into the gold doc ("previous"/"current" in bodies, "(old)"/"(current)" in titles). That let keyword methods win lifecycle categories by echoing a string. The lifecycle signal now lives only in `status` + the supersedes chain; the old and new doc of a pair are lexically identical. A direct consequence: **the old "BM25 staleness 0.050" was partly a leakage artifact** — with identical bodies, plain BM25 no longer ranks the stale doc *above* the current one by lexical luck. The real, un-lucky governance harm is measured instead by **Serves-Stale** (below).

### Per-category metrics

Recall/NDCG/MRR appear only for methods that produce a query-dependent ranking (whole-dump reads `n/a`). **Mean Tokens** is the as-shipped payload; **Norm Tokens** charges every method the same normalized policy (its top-1 full document body) so token cost reflects retrieval, not response-shaping convention. **Contains-Gold** is order-free (gold present anywhere in the payload). **Serves-Stale** is the fraction of supersession-topic queries where the retired doc reached the payload.

<!-- BENCH:comparison_per_category START -->
| Method | Category | Mean Tokens | Norm Tokens | Recall@k | Contains-Gold | Serves-Stale | NDCG@k | MRR | N |
|---|---|---|---|---|---|---|---|---|---|
| data-olympus | exact | 352 | 107 | 1.000 | 1.000 | 0.000 | 1.000 | 1.000 | 217 |
| data-olympus | semantic | 239 | 64 | 0.037 | 0.037 | 0.000 | 0.021 | 0.015 | 217 |
| data-olympus | status | 412 | 122 | 1.000 | 1.000 | 0.000 | 1.000 | 1.000 | 33 |
| data-olympus | graph | 384 | 122 | 1.000 | 1.000 | 0.000 | 1.000 | 1.000 | 33 |
| data-olympus | ALL | 309 | 90 | 0.582 | 0.582 | 0.000 | 0.575 | 0.573 | 500 |
| bm25 | exact | 542 | 107 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 217 |
| bm25 | semantic | 272 | 52 | 0.014 | 0.014 | 0.000 | 0.009 | 0.007 | 217 |
| bm25 | status | 577 | 122 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 33 |
| bm25 | graph | 577 | 122 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 33 |
| bm25 | ALL | 430 | 85 | 0.572 | 0.572 | 0.750 | 0.570 | 0.569 | 500 |
| bm25-status-aware | exact | 534 | 107 | 1.000 | 1.000 | 0.000 | 1.000 | 1.000 | 217 |
| bm25-status-aware | semantic | 272 | 52 | 0.014 | 0.014 | 0.000 | 0.009 | 0.007 | 217 |
| bm25-status-aware | status | 561 | 122 | 1.000 | 1.000 | 0.000 | 1.000 | 1.000 | 33 |
| bm25-status-aware | graph | 561 | 122 | 1.000 | 1.000 | 0.000 | 1.000 | 1.000 | 33 |
| bm25-status-aware | ALL | 424 | 85 | 0.572 | 0.572 | 0.000 | 0.570 | 0.569 | 500 |
| grep-read | exact | 1234 | 116 | 0.442 | 1.000 | 1.000 | 0.273 | 0.289 | 217 |
| grep-read | semantic | 10454 | 67 | 0.032 | 0.369 | 0.333 | 0.018 | 0.022 | 217 |
| grep-read | status | 27166 | 122 | 1.000 | 1.000 | 1.000 | 0.828 | 0.768 | 33 |
| grep-read | graph | 27166 | 122 | 1.000 | 1.000 | 1.000 | 0.828 | 0.768 | 33 |
| grep-read | ALL | 8658 | 95 | 0.338 | 0.726 | 0.833 | 0.236 | 0.236 | 500 |
| whole-dump | exact | 27166 | 110 | n/a | 1.000 | 1.000 | n/a | n/a | 217 |
| whole-dump | semantic | 27166 | 110 | n/a | 1.000 | 1.000 | n/a | n/a | 217 |
| whole-dump | status | 27166 | 110 | n/a | 1.000 | 1.000 | n/a | n/a | 33 |
| whole-dump | graph | 27166 | 110 | n/a | 1.000 | 1.000 | n/a | n/a | 33 |
| whole-dump | ALL | 27166 | 110 | n/a | 1.000 | 1.000 | n/a | n/a | 500 |
<!-- BENCH:comparison_per_category END -->

### Token cost: as-shipped vs normalized payload policy

The as-shipped **Mean Tokens** mixes retrieval with payload convention: bm25 returns 5 chunks, data-olympus returns outline + snippets + 1 full doc, whole-dump dumps everything. The **Norm Tokens** column removes that confound by charging every method the cost of surfacing exactly one full document — and there the methods are close (data-olympus ~90, bm25 ~85), which is the honest reading: **the ~28% as-shipped token advantage (309 vs 430) is mostly the lighter payload policy, not a retrieval-side miracle.** data-olympus's payload also scales sub-linearly (outline + a few snippets + one doc, independent of corpus size), whereas whole-dump grows linearly with every file added.

### Staleness avoidance (the real result)

With the de-leaked corpus, the honest governance-harm metric is **Serves-Stale**: does a superseded rule reach the agent at all? It is tiebreak-independent and unambiguous.

- **data-olympus serves-stale = 0.000** and **status-aware BM25 serves-stale = 0.000.** Both carry a status filter (in-force / status frontmatter) that excludes the superseded doc *before* ranking, so a retired rule never reaches the agent. That status-aware BM25 also scores 0.000 is the point of the baseline: **the staleness win is attributable to having the status metadata, not to the data-olympus engine.**
- **Plain BM25 serves-stale = 0.750** and **grep-read = 0.833.** A status-blind keyword method retrieves the superseded doc alongside the current one (they are lexically identical) 75-83% of the time it touches a supersession topic. **whole-dump serves-stale = 1.000** by definition.
- The older **staleness rate** metric (stale ranked at-or-above current) is now ~0.000 for every method, because with identical bodies a status-blind ranker ties the two and the result depends on an arbitrary tiebreak. That is exactly why Serves-Stale is the headline: it does not depend on tiebreak luck.

### Where data-olympus loses

On **semantic** (paraphrase) queries, data-olympus achieves recall=0.037, ndcg=0.021. Paraphrases lack the keyword overlap the FTS index relies on, so every keyword method does poorly here (BM25 0.014, grep-read 0.032); none is practically useful. This is the category where dense retrieval has the largest advantage. It remains data-olympus's genuine weakness: a curated synonym/acronym expansion layer bridges known lexical variants (`k8s` -> `kubernetes`, `rls` -> `row level security`), and the optional local-embedding hybrid (off by default) narrows it further, but the default full-text stack cannot follow arbitrary paraphrases the way dense retrieval does. The governance ablation below quantifies exactly how much embeddings buy on held-out paraphrases.

Overall, data-olympus recall (0.582) is competitive with, and here edges out, BM25 (0.572) — a small, real gap now that the de-leaked corpus removed the string-echo advantage — while never serving a superseded rule (serves-stale 0.000 vs BM25 0.750). It does not beat dense retrieval on paraphrase queries, and does not claim to.

### Governance ablation: does `applies_when` trigger metadata help?

This is the question that matters for the governance use case (coding-intent → governing-rule). A separate ablation runs a synthetic governance corpus (30 governing topics with distinct curated `applies_when` triggers, 10 supersession pairs, plus 31 distractor topics with no governing rule) through 158 stratified scenario queries, toggling one lever at a time. The corpus is built so the trigger terms do **not** appear in the doc body, and a held-out `paraphrase_uncovered` stratum uses intent phrasings that share **no** term with any trigger, so the test cannot flatter the metadata. Every recall figure in [`benchmarks/governance_results/ablation.md`](../benchmarks/governance_results/ablation.md) now carries a 95% bootstrap CI; regenerate with `python -m benchmarks.generate_governance_artifacts`.

<!-- BENCH:governance START -->
| Config | trigger_covered recall | paraphrase_uncovered (held-out) | negative FP rate | ALL recall | tokens/query |
|---|---|---|---|---|---|
| fts-no-metadata | 0.667 | 0.333 | 1.000 | 0.373 | 307 |
| fts+description | 0.667 | 0.345 | 1.000 | 0.380 | 306 |
| fts+applies_when | 1.000 | 0.414 | 1.000 | 0.481 | 308 |
| fts+applies_when+abstain | 1.000 | 0.379 | 0.097 | 0.462 | 209 |
| bm25-baseline | 1.000 | 0.356 | 1.000 | 0.449 | 658 |
<!-- BENCH:governance END -->

What the ablation honestly shows:

- **`applies_when` helps, and more than in the old 18-doc run.** On trigger-covered intents it lifts recall from 0.667 to **1.000** (+0.333), fixing cases where the model's tool/intent term is *not* already in the doc's prose; overall recall rises to **0.481** over 0.373 for body-only FTS, at roughly half BM25's tokens. The gain is larger than the old benchmark reported because the corpus grew (30 topics, so top-5 is no longer trivial) and the trigger terms are strictly held out of the body.
- **It cannot bridge true paraphrases.** On the held-out `paraphrase_uncovered` stratum, `applies_when` reaches only 0.414. When the query shares no term with any trigger, curated metadata alone cannot help; that is dense/semantic-retrieval territory. The optional local-embedding hybrid (measured in [`benchmarks/governance_results/embeddings/ablation.md`](../benchmarks/governance_results/embeddings/ablation.md)) lifts this stratum from 0.414 to ~0.598, which is exactly the gap it is meant to close.
- **`description` alone barely moves recall** (+0.007 overall); the trigger list is the lever that matters.
- **Abstention is solvable, with a recall trade-off.** Plain FTS (and BM25) have a **100% false-positive rate on negative queries** (queries with no governing rule): they always return something, because OR-matching hits generic words. The `fts+applies_when+abstain` config adds a **signal gate** — it returns nothing unless the query matches a discriminating column (title / tags / `applies_when`, deliberately not the prose `description`). That drops the negative false-positive rate from **1.000 to 0.097**: on the larger, more varied distractor set, 3 of 31 negatives still leak because their wording ("travel **budget**", "swag **order**", "celebration **policy**") shares a real word with a governing rule's triggers — an honest residual, not a perfect zero. The gate keeps full recall on trigger-covered and supersession intents, at a modest cost on hard paraphrases. For a governance tool this is usually the right trade: abstaining beats surfacing a rule that does not govern. The gate is built on the existing `columns` search parameter, so it is a deployable mode, not new machinery.

The honest summary: curated `applies_when` triggers are the right primary mechanism for governance retrieval (deterministic, auditable, no drift), they help and never hurt, and they pair with the token and staleness advantages above; abstention on out-of-scope queries is available as a signal-gated mode (near-zero false positives, at the cost of some paraphrase recall); the one thing curated metadata cannot do is bridge truly unanticipated phrasings, which remains dense/semantic-retrieval territory (and is where the optional embedding hybrid earns its keep).

### One committed result on a real (non-templated) corpus

Every number above is from the deterministic synthetic corpus, which exists to exercise scale and lifecycle under control, not to stand in for real content. For at least one number that is **not** templated, the lexical stack is run against the committed [`example-bundle`](../example-bundle) (18 hand-authored governance docs) with 9 labeled queries that are deliberate paraphrases avoiding each doc's distinctive title terms. Both corpus and queries are in the repo, so this is fully reproducible: `python -m benchmarks.real_corpus_eval --corpus example-bundle --queries benchmarks/real_corpus/example_bundle_queries.json --lexical-only --out benchmarks/real_corpus/example_bundle_result.json`.

<!-- BENCH:real_corpus START -->
| Metric | Lexical stack (embeddings off) |
|---|---|
| Corpus | 18 committed example-bundle docs |
| Labeled queries | 9 paraphrased intents |
| recall@5 | 0.778 |
| recall@10 | 0.889 |
| MRR@5 | 0.778 |
<!-- BENCH:real_corpus END -->

The two misses at k=5 are the token-disjoint paraphrases (e.g. "why did the team choose this markdown knowledge system" vs the doc titled "Adopt data-olympus for the knowledge base") — the exact semantic gap the optional embedding hybrid targets and the default lexical stack does not close. Provenance: hand-authored paraphrase queries over a committed corpus, embeddings off; it is an illustrative reproducible example, not user traffic.

---

## Per-tool comparison

### Google Open Knowledge Format (OKF)

The Open Knowledge Format is the parent specification. data-olympus is designed to be readable by OKF consumers: it inherits OKF's directory structure, frontmatter conventions, reserved filenames, and link model. That claim is not yet backed by an executable conformance test against OKF reference tooling ([issue #82](https://github.com/knaisoma/data-olympus/issues/82) tracks adding one); today it rests on shared structure by construction.

**Where data-olympus is better (and why):** OKF defines a minimal required set (`id`, `type`, `spec_version`) with no governance fields. data-olympus adds `status`, `tier`, a `supersedes` chain, and controlled vocabularies for each field, making it possible to query "show me all accepted T1 standards" or "what superseded this decision" without post-processing. data-olympus also ships a validated write pipeline (proposed edits, pending queue, advisory locks, commit-and-push) and an MCP server; OKF specifies no serving or write model.

**Where it is weaker:** OKF ships an automatic producer agent. The reference implementation can pull structured data from BigQuery and enrich it with web sources to populate a bundle with minimal human authoring. data-olympus has no equivalent; every concept is hand-authored or agent-proposed but still human-reviewed before commit.

**Where that is a deliberate decision:** data-olympus targets curated, reviewed knowledge where accuracy and governance matter more than coverage. Auto-ingestion without review is a deliberate non-goal for the v0.1 scope.

**Where they are complementary:** Because data-olympus is designed to share OKF's baseline structure, bundles produced by the OKF producer should be importable and governable by data-olympus tools, and data-olympus bundles should be consumable by an OKF-aware tool without conversion. Neither direction has an automated test yet ([issue #82](https://github.com/knaisoma/data-olympus/issues/82)).

---

### Enterprise data catalogs (Dataplex, Unity Catalog, Collibra, DataHub, Amundsen)

These platforms are metadata management systems for data assets: schemas, pipelines, columns, data products, and access policies at warehouse or lakehouse scale.

**Where data-olympus is better (and why):** data-olympus is fully portable: the entire knowledge base is a directory of markdown files in git. There is no proprietary service to maintain, no connector to keep authenticated, and no vendor to negotiate with. Diffs, reviews, and history are plain git operations. A freshly provisioned machine with Python and `uv` can serve the entire KB in under a minute. Catalog platforms require standing infrastructure, service accounts, and often significant licensing cost.

**Where it is weaker:** Enterprise catalogs offer capabilities data-olympus does not attempt: column-level data lineage, automated metadata harvesting from live data sources, fine-grained RBAC with data-access governance, and integrations with hundreds of data platform connectors. For anything involving discovering or governing data assets at scale, these tools are the right choice.

**Where that is a deliberate decision:** data-olympus is a curated knowledge layer, not a metadata harvesting platform. It references domain schemas and standards rather than subsuming the data plane. Adding automated profiling or connector management would expand the scope far beyond the target use case (agent workforce knowledge curation) and introduce the operational burden data-olympus is designed to avoid.

**Where they are complementary:** Catalog metadata (data product descriptions, schema documentation, ownership) can be exported as markdown and managed in a data-olympus bundle as the human-and-agent-facing knowledge layer. The catalog governs the data; data-olympus governs the engineering knowledge about that data.

---

### Markdown KB tools (Obsidian, Notion, MkDocs, Backstage TechDocs)

These tools provide editing, navigation, and publishing for markdown-based knowledge bases.

**Where data-olympus is better (and why):** These tools impose no format specification. A team using Obsidian or Notion can write any frontmatter they like (or none), which means there is no interoperability guarantee between instances and no way to programmatically query "all accepted standards." data-olympus specifies the minimum rule set needed for interop: required frontmatter fields, a controlled vocabulary, and cross-linking conventions. The MCP server and write pipeline are also absent from these tools; they have no concept of a governance queue for agent-proposed edits.

**Where it is weaker:** Obsidian, Notion, and Backstage have rich editing UIs, plugin ecosystems, rendering pipelines, and publishing workflows that data-olympus does not provide. Notion in particular offers real-time collaboration features with a familiar database-style view that is very productive for human teams. MkDocs and Backstage produce polished, navigable documentation sites.

**Where that is a deliberate decision:** data-olympus standardizes only the small rule set needed for agent interop. Adding a rendering pipeline or editing UI would replicate work these tools do well and would add maintenance burden with no benefit to the target use case. Teams that want a polished publishing layer can use MkDocs or similar on top of their data-olympus bundle.

**Where they are complementary:** data-olympus bundles are plain markdown with YAML frontmatter. They render correctly in Obsidian (vault), MkDocs (docs source), and Backstage TechDocs (docs-as-code) without any conversion. A team can author in Obsidian and validate/serve with data-olympus tools, or publish to MkDocs from a governed bundle.

---

### Agent-context conventions (llms.txt, Cursor .cursorrules, AGENTS.md / CLAUDE.md)

These are single-file or few-file conventions for injecting context into an AI agent at session start.

**Where data-olympus is better (and why):** A single context file cannot express a structured graph of hundreds of interconnected standards, decisions, and project rules, nor can it support querying by status, tier, or type. data-olympus provides a multi-document graph with tiers, full-text search, structured filters, and a write pipeline for proposing and reviewing updates, which is necessary when the knowledge corpus grows beyond what fits in a few context files.

**Where it is weaker:** llms.txt, `.cursorrules`, and AGENTS.md are near-zero-infrastructure conventions. Any project can adopt them in minutes with no tooling, no server, and no schema to conform to. For small projects or early exploration, that simplicity is a genuine advantage that data-olympus cannot match.

**Where that is a deliberate decision:** data-olympus targets larger, evolving corpora where the structure pays for itself through queryability and governed updates. For a 10-file project, a single AGENTS.md is the right tool. data-olympus becomes worthwhile when the corpus has hundreds of concepts across multiple teams and tiers.

**Where they are complementary:** An AGENTS.md file can point agents at a running data-olympus MCP endpoint as the authoritative source of truth, using the context file as a session bootstrap that explains how to query the KB rather than embedding the KB content directly.

---

### Memory and RAG (vector-DB RAG, MCP memory servers, graph RAG)

These systems provide semantic recall over large or unstructured corpora: embed documents into a vector space and retrieve the most relevant chunks at query time.

**Where data-olympus is better (and why):** data-olympus knowledge is deterministic, human-curated, and diffable. Every concept has a stable `id`, a `status`, and a `tier`. An agent retrieving a standard gets the exact reviewed text, not a chunk that may have drifted due to re-embedding or chunking artifacts. Version history is git history; changes require a proposed edit and a commit. This makes data-olympus appropriate for authoritative governance documents where correctness matters more than broad recall.

**Where it is weaker:** Vector RAG excels at semantic recall over large unstructured corpora. If the knowledge base contains thousands of prose documents with no consistent schema (engineering blog posts, support tickets, unstructured notes), semantic search returns useful results that full-text search would miss. data-olympus search is full-text with metadata filters by default; it also ships an optional local-embedding hybrid (`KB_EMBEDDINGS_MODE`, off by default, no external API or query-time network) that blends BM25 with cosine similarity over a local ONNX model, but it is not the default search mode and does not aim to match a dedicated vector-RAG stack's recall over large unstructured corpora.

**Where that is a deliberate decision:** The design prioritizes curated, reviewed knowledge over auto-ingested recall. Semantic drift, hallucinated provenance, and chunk-boundary artifacts are non-issues when content is hand-authored and reviewed before commit. The tradeoff is accepted: broader semantic recall is sacrificed for governance confidence.

**Where they are complementary:** A data-olympus bundle is a high-signal, well-structured source for a RAG or vector pipeline. Indexing a governed bundle into a vector store gives semantic search over content that has already been reviewed for accuracy. The bundle provides the quality guarantee; the vector store provides the semantic recall on top.

---

### ADR tooling (adr-tools, Log4brains)

adr-tools is a shell-script convention for creating and linking Markdown ADRs. Log4brains extends this with a web UI and richer cross-linking.

**Where data-olympus is better (and why):** In data-olympus, decisions are first-class concepts with a `type: decision` field, a `status` field (proposed / accepted / deprecated / superseded), and explicit `supersedes`/`superseded_by` chains. They live in the same governed bundle as standards, workflows, and project knowledge, so an agent can trace "this standard was adopted in ADR-005, which supersedes ADR-002, which was motivated by STD-U-001" in a single query graph. adr-tools and Log4brains are ADR-only silos with no connection to the surrounding knowledge corpus.

**Where it is weaker:** Log4brains ships a polished ADR-specific web UI with timeline view, tag filtering, and a readable published site. adr-tools is a zero-dependency shell convention that any developer can drop into any project in 30 seconds. data-olympus has no equivalent lightweight entry point and no publishing-ready ADR site.

**Where that is a deliberate decision:** ADRs living alongside standards and project knowledge is the point. Keeping them in a separate silo (even a well-polished one) breaks the cross-tier query graph that makes a governed KB useful to agents. The lack of a dedicated ADR publishing UI is a scope decision, not an oversight.

**Where they are complementary:** Existing ADR repos managed by adr-tools or Log4brains can be imported into a data-olympus bundle by adding the required frontmatter fields (`id`, `type: decision`, `status`, `tier: meta`). The import is a one-time migration; existing ADR filenames and content remain unchanged.

---

## Comparison with 2026 agent-memory and spec-driven tools

Two cohorts of products were heavily marketed through late 2025 and early 2026 in the "get the right context/governance in front of the agent" space. The general categories (RAG/vector memory, agent-context conventions) are covered above; this section drills into the specific trending products. It is dated on purpose: the landscape moves fast, figures below are as of mid-2026, and several rest on vendor primary sources (flagged inline).

### Agent memory / knowledge layers (Cognee, Zep/Graphiti, and peers)

[Cognee](https://github.com/topoteretes/cognee) (Apache-2.0; a [$7.5M seed led by Pebblebed, early 2026](https://www.cognee.ai/blog/cognee-news/cognee-raises-seven-million-five-hundred-thousand-dollars-seed)) unifies relational, vector, and graph storage into one engine via an ECL (Extract, Cognify, Load) pipeline, and its "memify" layer self-tunes the knowledge graph through feedback loops that reweight edges with use. [Zep/Graphiti](https://github.com/getzep/graphiti) (Apache-2.0, ~28k stars) is a temporal knowledge-graph engine over graph databases (Neo4j / FalkorDB / Neptune) using embeddings and hybrid semantic + BM25 + graph retrieval, consumable via SDK, MCP, and REST. [Mem0](https://github.com/mem0ai/mem0) (Apache-2.0; [$24M, Series A led by Basis Set Ventures, late 2025](https://mem0.ai/series-a)) is an embedding/vector memory engine that auto-extracts memories from interactions via an LLM and self-updates on conflicting facts (ADD/UPDATE/DELETE/NOOP); it ships [OpenMemory MCP](https://mem0.ai/blog/introducing-openmemory-mcp), a local-first shared memory layer for MCP clients. [Letta](https://github.com/letta-ai/letta) (formerly MemGPT, a UC Berkeley spinout; [$10M seed led by Felicis, 2024](https://www.felicis.com/blog/letta)) builds stateful agents whose memory lives in editable "blocks" the agent self-edits (Postgres + pgvector), and ships Letta Code, a memory-first coding agent. [Supermemory](https://github.com/supermemoryai/supermemory) (founder Dhravya Shah; [$2.6M seed, angels incl. Google's Jeff Dean, late 2025](https://techcrunch.com/2025/10/06/a-19-year-old-nabs-backing-from-google-execs-for-his-ai-memory-startup-supermemory/)) is a vector+graph memory API (SDK/MCP/REST) that applies decay/recency forgetting.

**Where data-olympus is better (and why):** these systems are self-mutating — Cognee's memify reweights the graph with use, Graphiti continuously integrates new interactions, Mem0 has an LLM auto-extract and self-update memories, Letta's agent self-edits its own memory blocks, and Supermemory applies decay/forgetting. data-olympus knowledge is the opposite: curated, human-reviewed, and changed only through a propose/pending/commit pipeline, so retrieval is reproducible and auditable, with controlled `status`/`tier`/`type` vocabularies and `supersedes` chains a self-tuning store does not provide. To be precise: some of these tools do offer access controls and audit logging (e.g. Mem0 documents access logs and inclusion/exclusion rules), so the accurate contrast is not "no governance" but "no human curation, no controlled vocabulary, and no review-before-commit gate." An independent review notes Supermemory's decay/forgetting makes it ["unsuitable for governance-grade applications requiring consistent, auditable decision-making"](https://betterstack.com/community/guides/ai/memory-with-supermemory/) — which is exactly data-olympus's target.

**Where they are better:** semantic and temporal recall over large, heterogeneous, unstructured data, and memory that adapts to usage automatically. data-olympus search is full-text by default (it does share a BM25 component with Graphiti); an optional local-embedding hybrid (off by default) adds a lightweight semantic component, but data-olympus deliberately omits the self-mutating graph layers these tools are built on.

**Where that is a deliberate decision:** data-olympus positions semantic recall as complementary RAG's job; these products are essentially that layer.

**Where they are complementary:** run a memory layer for evolving, semantic, episodic recall and data-olympus for the stable, governed engineering rules that must not drift. Different category, largely complementary.

### Spec-driven coding tools (AWS Kiro, GitHub Spec Kit, Tessl)

These drive code forward from authored specs (spec → plan → tasks → code) rather than retrieving a governing rule for a coding intent, and each persists its governance as versioned, git-native markdown — the closest structural match to data-olympus. [AWS Kiro](https://kiro.dev/) is an agentic IDE/CLI whose [steering files](https://kiro.dev/docs/steering/) are reviewed markdown with a four-mode inclusion model (Always / conditional `fileMatch` / Manual / Auto-matched against the request) — the nearest analog to data-olympus's `applies_when` triggers. [GitHub Spec Kit](https://github.com/github/spec-kit/blob/main/spec-driven.md) runs a Specify → Plan → Tasks pipeline and keeps a versioned [`constitution.md`](https://github.com/github/spec-kit/blob/main/templates/commands/constitution.md) of engineering principles enforced through a "Constitution Check" gate. [Tessl](https://tessl.io/blog/tessl-launches-spec-driven-framework-and-registry/) (Snyk founder Guy Podjarny; ~$125M raised) makes the spec the durable primary artifact and ships a Spec Registry of (vendor-claimed) 10,000+ library specs.

**Where data-olympus is better (and why):** its write path is a system-enforced single-writer propose/pending/commit pipeline, whereas Kiro and Spec Kit treat human review as a recommended practice, not an enforced gate (Kiro's CLI docs note no mandatory approval gate, and spec-kit issue #2459 shows `/implement` does not even load `constitution.md`). data-olympus also provides coding-intent → governing-rule full-text retrieval with signal-gated abstention, controlled vocabularies, `supersedes` chains, and cross-project scope; the spec tools' governance is per-feature/per-workspace and, for Kiro, vendor-hosted.

**Where they are better:** forward enforcement and generation. Spec Kit's constitution actively gates spec generation, and Kiro and Tessl drive end-to-end build-to-code (Tessl regenerates code from the spec). data-olympus retrieves the governing rule but does not generate or gate code — that forward pipeline is out of scope.

**Where they are complementary:** data-olympus standards can populate Kiro steering or a Spec Kit constitution; those tools gate generation while data-olympus answers "what rule governs this choice" at coding time. They are the nearest neighbors to data-olympus, but overlap only on the narrow governing-standards sliver and are otherwise a different workflow.

### Architecture-decision governance tools (Archgate, mcp-adr-analysis-server)

This is the nearest-adjacent category: tools that treat engineering decisions (ADRs) as rules an AI coding agent must follow. [Archgate](https://archgate.dev/) (Apache-2.0 CLI) turns ADRs (markdown + YAML in `.archgate/adrs/`, with companion `.rules.ts` checks) into **executable rules enforced in CI and pre-commit**, integrating with Claude Code, Cursor, Copilot, and VS Code so the agent reads them as "architectural guardrails" before generating code. [mcp-adr-analysis-server](https://github.com/tosin2013/mcp-adr-analysis-server) is an MCP server that **generates, discovers, and validates ADRs** from a codebase to give agents architectural intelligence.

**Where data-olympus is better (and why):** a human propose/pending/commit write gate, controlled `status`/`tier`/`type` vocabularies, `supersedes` chains, and `applies_when` triggers over a single-writer MCP retrieval surface. Archgate's ADRs are hand-authored with no equivalent review gate claimed; mcp-adr-analysis-server's content is AI-generated/discovered, with no human curation, controlled vocabulary, or supersedes chains.

**Where they are better:** Archgate **actively enforces** decisions — it verifies rules in CI/pre-commit (down to file and line) and blocks non-compliant code, an enforcement step data-olympus (a retrieval KB) does not perform. mcp-adr-analysis-server **auto-discovers** unrecorded decisions from the codebase, which data-olympus (which depends on humans to author rules) cannot.

**Where they are complementary:** data-olympus curates and governs the decision corpus; Archgate enforces those decisions in CI; mcp-adr-analysis-server surfaces candidate decisions from code for humans to curate into the governed store. (Both are small projects as of mid-2026 — roughly 48 and 29 GitHub stars.)

**A note on the niche, and on the word "governance".** data-olympus's exact position — human-curated, git-native, deterministic governing-rule retrieval with controlled vocabularies and supersedes chains — is sparsely populated. The closest design-level peer found, [`mori`](https://github.com/fjwood69/mori), independently arrives at the same choices (relational + full-text, no embeddings, a human "promote-to-canon" review gate), but it is an early ~20-star solo project whose decisions-vs-general-memory scope could not be verified. Three other products marketed with "governance" were checked and excluded because each uses the word differently: [Oracle AI Agent Memory](https://www.oracle.com/database/ai-agent-memory/) ("governed" means database RBAC/audit over a vector+graph episodic-memory store), [Microsoft's Agent Governance Toolkit](https://opensource.microsoft.com/blog/2026/04/02/introducing-the-agent-governance-toolkit-open-source-runtime-security-for-ai-agents/) (runtime action-policy enforcement / OWASP security, with no knowledge retrieval), and Memori (automatic conversational memory, the already-covered category).

**Synthesis.** The memory layers are a different category and almost entirely complementary (semantic, mutating recall vs governed, deterministic standards). The spec-driven tools are the nearest neighbors and partly competitive on the governing-standards sliver (Kiro steering, Spec Kit constitution), but their enforcement is forward spec-generation/gating, not coding-intent → governing-rule retrieval, so they too are mostly complementary. The closest *category* is ADR-as-executable-rules (Archgate, mcp-adr-analysis-server), which enforces or generates decisions rather than governing a curated decision corpus. The net is that data-olympus's exact niche — a vendor-neutral, deterministic, auditable, cross-project KB that answers "what did we already decide that governs this choice" with a human review gate — is genuinely sparsely populated; no surveyed tool occupies it directly.

---

## Honest weaknesses

These are the areas where data-olympus is currently weakest, separate from deliberate scope decisions:

- **Search is full-text by default, with curated lexical expansion; semantic retrieval is optional and off by default.** A curated, bidirectional synonym/acronym map expands the query before matching (so `k8s`/`kubernetes` and `rls`/`row level security` find each other), configurable via `KB_SYNONYMS` / `KB_SYNONYMS_MODE`. An optional local-embedding hybrid (`KB_EMBEDDINGS_MODE`, default off) blends BM25 with cosine similarity over a local ONNX model to bridge paraphrases with no lexical overlap and no external API or query-time network; with it enabled, the lexical stack is unchanged for exact/status/graph-style queries and paraphrase recall improves. With embeddings left off (the default), queries that depend on conceptual proximity or paraphrases outside the curated synonym map will still miss results.
- **No automatic producer or ingestion agent.** Every concept must be authored or proposed by an agent, then reviewed and committed. There is no crawler, connector, or auto-enrichment pipeline.
- **Pre-release specification (v0.1).** The SPEC is not yet frozen. Field names, required fields, and serving contracts may change before a stable release.
- **Single-writer deployment required for writes.** The write pipeline assumes one server instance owns the git working tree. Horizontal write scaling requires a redesign of the lock and worktree model.
- **No publishing pipeline.** Bundles are served over MCP to agents; there is no built-in rendered documentation site for human browsing at scale.
