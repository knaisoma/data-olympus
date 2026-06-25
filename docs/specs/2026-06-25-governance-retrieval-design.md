# Design: metadata-driven governance retrieval (Part D)

**Date:** 2026-06-25
**Status:** Draft (design approved, scope = D1 + D2; embeddings deferred)
**Topic:** Improve "coding-intent → governing rule" retrieval using curated
document metadata (not embeddings), and measure each lever honestly with a
rebuilt governance benchmark.

This design is the synthesis of a Claude analysis and an independent Codex
companion review (CLI consult, 2026-06-25; the MCP transport failed twice so the
documented CLI fallback was used). Where they differed it is noted.

---

## 1. Positioning (the frame that drives the design)

data-olympus is **not** a code-search, reference-finding, or "where is X used"
tool. LSP, grep, and Sourcegraph already do that well and it will not compete
there. It is a **decision and instruction governance layer**: when a model is
vibe-coding and about to make a choice, surface the established standard /
decision / pattern that should govern that choice.

The retrieval task that matters is therefore **coding-intent → governing rule**,
e.g. the intent "I'll write this Excel export with `openpyxl.insert_cols`" must
retrieve the standard "prefer `xlsxwriter`; avoid `openpyxl` `insert_cols`". This
is not open-domain paraphrase search: the corpus is curated, human-reviewed, and
finite, so the bridging vocabulary can be **authored**, not inferred.

The Part B/C benchmark exposed the gap: full-text search scores recall 0.036 on
paraphrase queries because the model's phrasing rarely matches the rule's prose.

## 2. Core thesis

For curated governance docs, **author-supplied trigger metadata bridges most
intent→rule gaps deterministically**, which is the right primary mechanism, not
embeddings. Both reviewers agree. The architecture is:

> governed lexical + metadata retrieval first, with status/tier/supersession
> guardrails always applied, and an optional vector fallback **only** if measured
> metadata-covered recall plateaus.

A governance tool punishes plausible-but-wrong matches harder than ordinary
search: surfacing the *wrong* standard is worse than surfacing none. So every
recall lever here is curated and deterministic, never a loose global thesaurus.

## 3. Non-goals (this design)

- **No embeddings/vector backend implemented.** D1/D2 build and measure the
  metadata path. The retrieval seam is left pluggable so a vector fallback tier
  can be added later, but it is not built now. (Claude: defer the design too;
  Codex: design the seam now. Resolution: make retrieval pluggable, do not build
  the embedding backend.)
- **No intent-classification / LLM-in-the-loop facet routing in D1.** Soft facet
  boosting is sketched as future work; D1 ships the highest-leverage levers only.
- **Not competing with code search.** Retrieval targets governing concepts, not
  code symbols.

## 4. D1: the metadata retrieval mechanism

Ranked by leverage (Claude + Codex converged on this order):

1. **First-class `applies_when` trigger metadata.** A new frontmatter field: a
   curated list of the intents, tools, symbols, libraries, and synonyms a rule
   governs (e.g. `applies_when: [openpyxl, excel, xlsx, insert_cols, spreadsheet]`).
   Distinct from descriptive `tags`. Indexed as its own high-weight FTS column,
   so a query term that matches a trigger retrieves the governing doc directly.
   This *is* the "doc-scoped, provenance-preserving query expansion": the alias
   lives on the doc, so matching it is deterministic and auditable (you can see
   which trigger fired). `kb lint` recommends it on `standard`/`decision` docs;
   it is agent-proposable through the existing write pipeline and human-reviewed.
2. **Parse + index `description`.** A curated "when does this apply" summary.
   Indexed as its own column, weighted above body, below title/triggers.
3. **Column-weighted ranking.** Replace flat `bm25(fts)` with
   `bm25(fts, w_title, w_tags, w_applies, w_desc, w_body)`, weighting
   title/`applies_when` highest and body lowest.
4. **Parser upgrade (prerequisite).** `markdown_parse.py` hand-rolls a line
   parser that cannot reliably handle YAML lists or multiline values, yet
   `pyyaml` is already a runtime dependency. Replace its frontmatter parse with
   `yaml.safe_load`, keeping lenient failure (malformed → empty frontmatter, as
   today). Required before multi-item `applies_when` / multiline `description`
   are usable. This is the riskiest change (it touches the index's parse path
   for every doc); the full 333-test suite plus new tests are the safety net.
5. **Positioning doc fix.** Sharpen `README.md` and `docs/comparison.md` to state
   data-olympus is a governance layer, not a code-search tool, and frame
   retrieval as intent→governing-rule.

Deferred to later (noted, not built in D1): soft facet boosting (boost, never
hard-filter, on inferred tier/type, with low-confidence fallback to global
active docs), and two-stage retrieve→rerank with governance features
(alias hit, title hit, status freshness, tier proximity).

### Anti-drift guardrails (Codex, load-bearing)

- Query expansion is **doc-scoped only**: terms expand against the corpus's own
  `applies_when` aliases (alias → doc id), never a global synonym list.
- Facet inference (when added) **boosts, never hard-filters**, so a wrong guess
  cannot hide a valid rule.
- status/tier/supersession filters remain available and are applied as guardrails.

## 5. D2: the governance benchmark (honest, non-rigged)

The Part B/C benchmark cannot measure metadata levers (its synthetic corpus has
no tags/descriptions/triggers), and naively enriching it would rig the result.
The rebuilt benchmark:

- **Corpus:** 80-150 governing docs across `T1`-`T4`, with near-miss distractors,
  supersession pairs, and authored `applies_when` triggers. Generated
  deterministically and committed; documented synthetic.
- **Queries: coding-intent scenarios**, not topic words ("I'm adding an Excel
  export and plan to use openpyxl insert_cols"; "I'm writing a Fastify
  endpoint"). Include tool/library names and code snippets. **Authored
  independently of the triggers** (separate "roles": the doc's triggers are
  written without seeing the queries and vice versa, modeled in the generator by
  drawing query phrasings from a vocabulary disjoint-by-construction from a
  controlled fraction of triggers).
- **Held-out alias discipline:** a stratum of intent phrasings deliberately uses
  terms appearing in **no** trigger. data-olympus is expected to **lose** there
  (that is embeddings territory); reporting it proves the benchmark is not
  teaching-to-the-test.
- **Stratified categories:** exact-overlap, alias-covered intent,
  paraphrase-not-covered intent, facet-heavy, stale/supersession, ambiguous, and
  **negative** (no governing rule exists).
- **Frozen gold labels** independent of any method's output; allow multiple
  correct docs where hierarchy matters (T1 + T2), and score current docs above
  superseded.
- **Metrics beyond recall:** recall@5, MRR, stale-hit rate, **false-positive rate
  on negative queries**, **governance-miss-rate** (no governing rule in top-k),
  abstention quality, and token cost. The negative/abstention metrics matter
  most for a governance tool: it must know when no rule applies and not invent
  one.
- **Ablation (the point):** run the same corpus/queries through incremental
  system configs and report the marginal contribution of each lever:
  - current FTS (flat weights, no metadata)
  - + `description` + column weights
  - + `applies_when` indexing
  - + doc-scoped alias expansion
  - BM25 baseline
  - (optional, later) + vector fallback
  This is what honestly answers "does metadata slicing help, and by how much".

## 6. Sequencing

- **D1** (this spec §4): mechanism + parser fix + positioning doc. Planned and
  built first. Its retrieval features must be **toggleable** (a config/params
  surface) so D2's ablation can switch them on and off.
- **D2** (this spec §5): the governance benchmark + ablation, planned against
  D1's landed, toggleable retrieval API.
- **D3** (out of scope here): embedding fallback tier, only if D2 shows recall
  plateauing below an acceptable bar despite metadata.

## 7. Risks

- **Parser swap regression.** Moving to `yaml.safe_load` changes frontmatter
  parsing semantics (id sanitization, list handling). Mitigation: keep lenient
  failure, run the full suite, add targeted tests for the previously hand-handled
  edge cases (id with embedded `:`, scalar-vs-list `tags`).
- **Authoring burden.** Triggers only help if filled; the real KB barely uses
  `tags`/`description` today. Mitigation: lint recommends `applies_when`;
  agent-proposed + human-reviewed via the write pipeline. The benchmark measures
  the realistic partial-coverage case, not a fully-tagged ideal.
- **Benchmark rigging.** The single biggest integrity risk. Mitigation: the
  held-out-alias stratum and negative queries, plus authoring triggers and
  queries from disjoint vocabularies by construction.

## 8. Acceptance criteria

**D1:**
- `markdown_parse.parse_file` uses `yaml.safe_load` (lenient) and extracts
  `applies_when` (list) and `description`; full existing suite stays green.
- Index has `applies_when`/`description` FTS columns; `search()` uses column
  weights; an `applies_when` trigger term retrieves its governing doc.
- Retrieval features are toggleable for ablation.
- README/comparison.md state the governance positioning.

**D2:**
- Committed governance corpus (80-150 docs, authored triggers, distractors,
  supersession) + scenario query set with held-out-alias and negative strata.
- Ablation runner reports per-config metrics incl. governance-miss-rate and
  false-positive-on-negatives.
- `docs/comparison.md` reports the ablation result (each lever's marginal value),
  including where data-olympus still loses.
