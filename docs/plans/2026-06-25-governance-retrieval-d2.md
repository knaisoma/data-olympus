# Governance Benchmark + Ablation Implementation Plan (Part D2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Honestly measure whether curated `applies_when` trigger metadata improves coding-intent → governing-rule retrieval, by ablating the levers (no-metadata FTS → +description → +applies_when, plus a BM25 baseline) over a governance corpus with held-out-no-trigger and negative query strata, then report each lever's marginal value.

**Architecture:** Reuse the Part B harness (`benchmarks/tokenizer.py`, `metrics.py`). Add a governance corpus generator (docs carry authored `applies_when` triggers, descriptions, distractors, supersession pairs), a scenario query generator (strata incl. held-out-no-trigger and negatives), two governance metrics, and an ablation runner that calls the real `Index.search()` with different `columns`/`column_weights` configs. Commit the corpus, queries, and results; add a governance ablation section to `docs/comparison.md`.

**Tech Stack:** Python 3.13, the in-repo `data_olympus.index.Index` (D1: `search(columns=, column_weights=)`), stdlib only (no `[bench]` deps), pytest, ruff.

**Part of:** [docs/specs/2026-06-25-governance-retrieval-design.md](../specs/2026-06-25-governance-retrieval-design.md) §5 (D2). Depends on D1 (landed).

**Integrity guardrails (from the spec, load-bearing):** triggers and query phrasings are drawn from disjoint-by-construction vocabularies; a `paraphrase-uncovered` stratum uses terms in NO trigger (data-olympus is expected to lose there); `negative` queries have NO governing rule (measure false positives / abstention). If the ablation shows `applies_when` does NOT help, report that plainly.

---

## File Structure

```
benchmarks/
  governance_corpus.py     # governance corpus generator (applies_when triggers)
  governance_queries.py    # scenario query + gold generator (stratified)
  ablate.py                # ablation runner over Index.search configs + BM25
  governance/              # GENERATED, committed corpus
  governance_queries.yaml  # GENERATED, committed
  governance_results/      # GENERATED, committed report + json
  metrics.py               # ADD governance_miss_rate + false_positive_rate
tests/
  test_bench_governance_corpus.py
  test_bench_governance_queries.py
  test_bench_governance_metrics.py
  test_bench_ablate_smoke.py
docs/comparison.md         # ADD "Governance ablation" subsection
```

All tests dep-free (CI runs `.[dev]` + `ruff check .`). Run with `uv run pytest -q`.

---

### Task 1: Two governance metrics

**Files:**
- Modify: `benchmarks/metrics.py`
- Test: `tests/test_bench_governance_metrics.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_bench_governance_metrics.py`:

```python
from __future__ import annotations

from benchmarks.metrics import false_positive_rate, governance_miss_rate


def test_governance_miss_rate_counts_queries_with_no_gold_in_topk() -> None:
    # ranked lists per query, gold sets per query, k=3
    ranked = [["a", "b"], ["x", "y", "z"], ["c"]]
    golds = [{"a"}, {"GONE"}, {"c"}]
    # query 2 misses -> miss rate 1/3
    assert governance_miss_rate(ranked, golds, k=3) == 1 / 3


def test_governance_miss_rate_empty_inputs_is_zero() -> None:
    assert governance_miss_rate([], [], k=3) == 0.0


def test_false_positive_rate_on_negative_queries() -> None:
    # For negative queries (no governing rule), any non-empty retrieval is a
    # false positive. retrieved counts per negative query:
    retrieved_counts = [0, 2, 0, 5]
    # 2 of 4 returned something -> 0.5
    assert false_positive_rate(retrieved_counts) == 0.5


def test_false_positive_rate_no_negatives_is_zero() -> None:
    assert false_positive_rate([]) == 0.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_bench_governance_metrics.py -q`
Expected: FAIL — `ImportError: cannot import name 'governance_miss_rate'`

- [ ] **Step 3: Implement**

Append to `benchmarks/metrics.py`:

```python
def governance_miss_rate(
    ranked_per_query: list[Sequence[str]], gold_per_query: list[set[str]], *, k: int
) -> float:
    """Fraction of queries with NO gold concept in the top-k. The headline
    governance failure: the agent gets no governing rule."""
    if not ranked_per_query:
        return 0.0
    misses = 0
    for ranked, gold in zip(ranked_per_query, gold_per_query, strict=True):
        if not gold or not (set(ranked[:k]) & gold):
            misses += 1
    return misses / len(ranked_per_query)


def false_positive_rate(retrieved_counts_on_negatives: list[int]) -> float:
    """For negative queries (no governing rule exists), the fraction that
    returned anything at all. A governance tool should abstain on these."""
    if not retrieved_counts_on_negatives:
        return 0.0
    return sum(1 for c in retrieved_counts_on_negatives if c > 0) / len(
        retrieved_counts_on_negatives
    )
```

(`Sequence` is already imported under TYPE_CHECKING in metrics.py.)

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_bench_governance_metrics.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add benchmarks/metrics.py tests/test_bench_governance_metrics.py
git commit -m "feat(bench): governance metrics (miss rate, false-positive on negatives)"
```

---

### Task 2: Governance corpus generator

**Files:**
- Create: `benchmarks/governance_corpus.py`
- Test: `tests/test_bench_governance_corpus.py`

**Design:** Deterministic. ~120 governing docs across tiers/types. Each governs one TOPIC drawn from a fixed list with, per topic, a `TRIGGER_VOCAB` (terms authored onto the doc's `applies_when`) and a disjoint `INTENT_VOCAB` (real-world phrasings used later to build queries — never written to the doc). ~15% supersession pairs. Plus "distractor" topics with no doc (for negative queries). Emits frontmatter incl. `applies_when` + `description`; lint-clean. Returns a manifest: per topic the `current_id`, `stale_id`, `triggers` (subset authored), `covered_terms` (trigger terms), and `uncovered_terms` (INTENT_VOCAB terms in no trigger).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_bench_governance_corpus.py`:

```python
from __future__ import annotations

from pathlib import Path

from benchmarks.governance_corpus import generate_governance_corpus
from data_olympus.index import Index


def test_corpus_is_deterministic(tmp_path: Path) -> None:
    a = generate_governance_corpus(tmp_path / "a", n=60, seed=3)
    b = generate_governance_corpus(tmp_path / "b", n=60, seed=3)
    assert [c.id for c in a.concepts] == [c.id for c in b.concepts]


def test_docs_carry_applies_when_triggers(tmp_path: Path) -> None:
    m = generate_governance_corpus(tmp_path / "kb", n=80, seed=1)
    idx = Index(tmp_path / "i.db")
    idx.build(tmp_path / "kb", source_commit="x")
    topic = m.topics[0]
    doc = idx.get(topic.current_id)
    assert doc is not None
    assert doc.applies_when, "governing docs must carry applies_when triggers"


def test_covered_and_uncovered_terms_are_disjoint(tmp_path: Path) -> None:
    m = generate_governance_corpus(tmp_path / "kb", n=80, seed=1)
    for t in m.topics:
        assert not (set(t.covered_terms) & set(t.uncovered_terms)), (
            "trigger-covered and held-out terms must be disjoint by construction"
        )


def test_corpus_lints_clean(tmp_path: Path) -> None:
    from data_olympus.format import discover_bundle_files, lint_files
    root = tmp_path / "kb"
    generate_governance_corpus(root, n=80, seed=1)
    results = lint_files(discover_bundle_files(root))
    errors = [(p, f) for p, fs in results.items() for f in fs if f.severity == "error"]
    assert not errors, f"governance corpus must lint clean; got {errors}"


def test_has_supersession_pairs(tmp_path: Path) -> None:
    m = generate_governance_corpus(tmp_path / "kb", n=120, seed=2)
    assert any(t.stale_id is not None for t in m.topics)
```

(Confirm the `lint_files` result shape — `dict[Path, list[Finding]]` with `Finding.severity` — matches the existing `benchmarks/corpus_gen.py` lint test; reuse that exact pattern.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_bench_governance_corpus.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'benchmarks.governance_corpus'`

- [ ] **Step 3: Implement**

Create `benchmarks/governance_corpus.py`. Reuse the `Concept`/`CorpusManifest` shapes from `benchmarks/corpus_model.py` where possible; add a governance `TopicRecord` carrying `covered_terms`/`uncovered_terms`/`triggers` (define a local dataclass if `corpus_model.TopicRecord` lacks them). Provide a fixed table per topic, e.g.:

```python
# topic -> (trigger terms authored onto applies_when, held-out intent terms NOT authored)
GOV_TOPICS = {
    "excel-export": (["openpyxl", "xlsxwriter", "insert_cols", "xlsx"],
                     ["spreadsheet", "workbook", "cell formulas"]),
    "force-push":   (["force-push", "--force", "git push -f"],
                     ["overwrite remote history", "rewrite the branch"]),
    "module-structure": (["nestjs module", "feature module", "providers"],
                         ["organize the backend", "where to put services"]),
    # ... at least ~12 topics, each with disjoint trigger vs held-out term sets ...
}
```

Write each governing doc with frontmatter `id/type/status/tier/title/description` plus `applies_when: [<triggers>]`. Body is templated prose that does NOT contain the held-out terms (so the held-out stratum truly tests bridging). Generate ~15% supersession pairs (superseded predecessor + active replacement, overlapping bodies, `supersedes`/`superseded_by`). Record per topic in the manifest: `current_id`, `stale_id`, `covered_terms` (the authored triggers), `uncovered_terms` (the held-out intent terms). Deterministic via `random.Random(seed)`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_bench_governance_corpus.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add benchmarks/governance_corpus.py tests/test_bench_governance_corpus.py
git commit -m "feat(bench): governance corpus generator with applies_when triggers"
```

---

### Task 3: Scenario query generator (stratified, incl. held-out + negatives)

**Files:**
- Create: `benchmarks/governance_queries.py`
- Test: `tests/test_bench_governance_queries.py`

**Design:** From the manifest, emit `GovQuery(text, stratum, gold_ids, current_id, stale_id)` across strata:
- `trigger_covered`: scenario phrased using a trigger term ("I'm exporting to Excel with openpyxl insert_cols"). gold = current_id.
- `paraphrase_uncovered`: scenario phrased using ONLY held-out terms ("I need to write a spreadsheet workbook"). gold = current_id. **data-olympus expected to lose here.**
- `supersession`: "what's the current rule for <topic>" on pair topics. gold = current_id, stale set.
- `negative`: a scenario about a distractor topic with NO governing doc. gold = empty (`[]`).

`write_governance_queries` / `load_governance_queries` round-trip via yaml.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_bench_governance_queries.py`:

```python
from __future__ import annotations

from pathlib import Path

from benchmarks.governance_corpus import generate_governance_corpus
from benchmarks.governance_queries import build_governance_queries


def test_covers_all_strata(tmp_path: Path) -> None:
    m = generate_governance_corpus(tmp_path / "kb", n=120, seed=2)
    qs = build_governance_queries(m)
    strata = {q.stratum for q in qs}
    assert {"trigger_covered", "paraphrase_uncovered", "supersession", "negative"} <= strata


def test_negative_queries_have_empty_gold(tmp_path: Path) -> None:
    m = generate_governance_corpus(tmp_path / "kb", n=120, seed=2)
    negs = [q for q in build_governance_queries(m) if q.stratum == "negative"]
    assert negs
    assert all(q.gold_ids == [] for q in negs)


def test_uncovered_queries_use_no_trigger_term(tmp_path: Path) -> None:
    m = generate_governance_corpus(tmp_path / "kb", n=120, seed=2)
    triggers = {term for t in m.topics for term in t.covered_terms}
    uncovered = [q for q in build_governance_queries(m) if q.stratum == "paraphrase_uncovered"]
    assert uncovered
    for q in uncovered:
        assert not (set(q.text.lower().split()) & {x.lower() for x in triggers}), (
            "uncovered queries must not contain any trigger term"
        )
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_bench_governance_queries.py -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `benchmarks/governance_queries.py` with `GovQuery` (frozen dataclass) and `build_governance_queries(manifest)`. Build `trigger_covered` from `covered_terms`, `paraphrase_uncovered` from `uncovered_terms` ONLY (assert no overlap with triggers), `supersession` for pair topics, and `negative` from distractor topics (gold empty). Add yaml `write_governance_queries`/`load_governance_queries`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_bench_governance_queries.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add benchmarks/governance_queries.py tests/test_bench_governance_queries.py
git commit -m "feat(bench): stratified governance scenario queries (held-out + negatives)"
```

---

### Task 4: Ablation runner

**Files:**
- Create: `benchmarks/ablate.py`
- Test: `tests/test_bench_ablate_smoke.py`

**Design:** Define ablation CONFIGS, each a label + kwargs for `Index.search()`:
- `fts-no-metadata`: `columns=["title", "tags", "body"]` (exclude applies_when/description), flat weights.
- `fts+description`: `columns=["title", "tags", "description", "body"]`, flat weights.
- `fts+applies_when`: default (all columns), default weights (production).
- `bm25-baseline`: the existing `benchmarks.methods.bm25.Bm25Method`.

`run_ablation(corpus_root, idx, queries, tokenizer, k=5)` runs each config over all governance queries, computing per-stratum recall@k, MRR, governance_miss_rate, plus false_positive_rate on the `negative` stratum and token cost. Returns an `AblationReport`. `write_ablation(report, out_dir)` writes `ablation.json` + `ablation.md` (per-config × per-stratum table, an explicit "marginal value of applies_when" line comparing `fts+description` vs `fts+applies_when`, and a "held-out (paraphrase_uncovered) — where metadata does not help" line). For configs that use `Index.search`, build a thin method wrapper that calls `idx.search(q.text, limit=k, **config_kwargs)`; for bm25 use the existing method.

- [ ] **Step 1: Write the failing smoke test**

Create `tests/test_bench_ablate_smoke.py`:

```python
from __future__ import annotations

from pathlib import Path


def test_run_ablation_smoke(tmp_path: Path) -> None:
    from benchmarks.ablate import run_ablation
    from benchmarks.governance_corpus import generate_governance_corpus
    from benchmarks.governance_queries import build_governance_queries
    from benchmarks.tokenizer import SimpleTokenizer
    from data_olympus.index import Index

    root = tmp_path / "kb"
    m = generate_governance_corpus(root, n=80, seed=1)
    idx = Index(tmp_path / "i.db")
    idx.build(root, source_commit="x")
    qs = build_governance_queries(m)
    report = run_ablation(corpus_root=root, idx=idx, queries=qs,
                          tokenizer=SimpleTokenizer(), k=5)
    labels = {r.config for r in report.rows}
    assert {"fts-no-metadata", "fts+applies_when", "bm25-baseline"} <= labels
    # applies_when config should not do WORSE than no-metadata on trigger_covered recall
    cov_no = next(r for r in report.rows
                  if r.config == "fts-no-metadata" and r.stratum == "trigger_covered")
    cov_aw = next(r for r in report.rows
                  if r.config == "fts+applies_when" and r.stratum == "trigger_covered")
    assert cov_aw.recall >= cov_no.recall


def test_write_ablation(tmp_path: Path) -> None:
    from benchmarks.ablate import run_ablation, write_ablation
    from benchmarks.governance_corpus import generate_governance_corpus
    from benchmarks.governance_queries import build_governance_queries
    from benchmarks.tokenizer import SimpleTokenizer
    from data_olympus.index import Index
    root = tmp_path / "kb"
    m = generate_governance_corpus(root, n=60, seed=1)
    idx = Index(tmp_path / "i.db")
    idx.build(root, source_commit="x")
    report = run_ablation(corpus_root=root, idx=idx,
                          queries=build_governance_queries(m),
                          tokenizer=SimpleTokenizer(), k=5)
    out = tmp_path / "res"
    write_ablation(report, out)
    assert (out / "ablation.json").exists()
    assert (out / "ablation.md").exists()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_bench_ablate_smoke.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'benchmarks.ablate'`

- [ ] **Step 3: Implement**

Create `benchmarks/ablate.py`: `AblationRow(config, stratum, recall, mrr, miss_rate, false_positive_rate, mean_tokens, n)`, `AblationReport(rows, k)`, `run_ablation(...)`, `write_ablation(...)`. For each config and each query, get ranked ids (from `idx.search(...).hits` ids, or the bm25 method's `ranked_ids`), compute per-(config,stratum) metrics with the existing + new metric functions, and false_positive_rate over the `negative` stratum (retrieved count per negative query). Token cost = tokenizer count over the retrieved payload (for the search configs, payload = concatenated hit snippets + top `get()` body, mirroring `DataOlympusMethod`).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_bench_ablate_smoke.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add benchmarks/ablate.py tests/test_bench_ablate_smoke.py
git commit -m "feat(bench): governance retrieval ablation runner"
```

---

### Task 5: Generate committed governance artifacts

**Files:**
- Create: `benchmarks/generate_governance_artifacts.py`; generated `benchmarks/governance/**`, `benchmarks/governance_queries.yaml`, `benchmarks/governance_results/{ablation.json,ablation.md}`

- [ ] **Step 1: Write the entrypoint**

Create `benchmarks/generate_governance_artifacts.py` (runnable as `python -m benchmarks.generate_governance_artifacts`): generate the corpus (`n=120, seed=0`) into `benchmarks/governance/`, write `benchmarks/governance_queries.yaml`, build an `Index` (to `benchmarks/bench.db`, gitignored), run `run_ablation` with `SimpleTokenizer`, write `benchmarks/governance_results/`.

- [ ] **Step 2: Produce + lint**

Run:
```bash
uv run python -m benchmarks.generate_governance_artifacts
uv run data-olympus lint benchmarks/governance
```
Expected: artifacts written; lint exits 0.

- [ ] **Step 3: Read the numbers**

Run: `cat benchmarks/governance_results/ablation.md`. Record the marginal value of `applies_when` (trigger_covered recall vs no-metadata), the paraphrase_uncovered numbers (where it should NOT win), and false-positive on negatives.

- [ ] **Step 4: Commit**

```bash
git add benchmarks/governance benchmarks/governance_queries.yaml benchmarks/governance_results benchmarks/generate_governance_artifacts.py
git commit -m "feat(bench): commit governance corpus, queries, and ablation results"
```

---

### Task 6: Add the governance ablation section to docs/comparison.md (coordinator, honesty-critical)

**Files:**
- Modify: `docs/comparison.md`

This is done by the coordinator, copying real numbers from `benchmarks/governance_results/ablation.md`.

- [ ] **Step 1** Read `benchmarks/governance_results/ablation.md`.
- [ ] **Step 2** Add a `### Governance ablation` subsection under "Quantified comparison" with: the per-config × per-stratum table (real numbers), a one-line marginal-value statement for `applies_when`, the held-out `paraphrase_uncovered` result stated as the honest limit (where curated metadata does not bridge), and the negative-query false-positive/abstention result. If `applies_when` does not materially help, say so.
- [ ] **Step 3** Final gate: `uv run pytest -q && uv run ruff check . && uv run data-olympus lint benchmarks/governance && uv run data-olympus lint example-bundle`. All pass.
- [ ] **Step 4** Commit: `git commit -m "docs: add governance retrieval ablation results to comparison"`

---

## Self-Review

- **Spec coverage (§5):** governance corpus with triggers (Task 2), scenario queries with held-out + negatives (Task 3), governance metrics (Task 1), ablation across levers + BM25 (Task 4), committed artifacts (Task 5), comparison.md (Task 6). All map to a task.
- **Non-rigged:** `covered_terms` and `uncovered_terms` disjoint by construction (Task 2 test); `paraphrase_uncovered` queries provably contain no trigger term (Task 3 test); negatives have empty gold and measure false positives. The benchmark can show metadata NOT helping.
- **CI-safe:** all tests dep-free; `ruff check .` covers `benchmarks/`; `bench.db` is gitignored.
- **Reuse:** tokenizer + base metrics + bm25 method reused from Part B; only governance-specific pieces are new.
- **Honesty:** Task 6 copies numbers from the committed `ablation.md`; reports the held-out limit and any null result.
