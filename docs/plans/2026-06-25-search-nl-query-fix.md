# Search Natural-Language Query Fix Implementation Plan (Part C)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make `Index.search()` retrieve on natural-language / multi-word queries (not just verbatim substrings) while preserving single-token ID lookups, then re-run the benchmark so the accuracy and staleness numbers reflect the fix.

**Architecture:** Today `search()` wraps the *entire* query as one exact FTS5 phrase (`'"' + query + '"'`), so anything but a verbatim phrase match returns nothing. Change it to quote *each whitespace term individually* and combine the terms with FTS5 `OR`. Single-token ID queries (`STD-U-002`) stay a single quoted phrase (unchanged behavior); multi-word queries become an any-term match ranked by bm25. Per-term quoting keeps it injection-safe (user terms cannot inject FTS operators).

**Tech Stack:** Python 3.13, SQLite FTS5, pytest, ruff. Benchmark harness from Part B.

**Part of:** [docs/specs/2026-06-25-retrieval-benchmark-design.md](../specs/2026-06-25-retrieval-benchmark-design.md). Follows Part A (status/type filter) and Part B (benchmark), both landed. Motivated by the Part B finding that phrase-only search produced recall=0.000 on 3 of 4 query categories and masked the staleness differentiator.

---

## File Structure

- `src/data_olympus/index.py` — rewrite the query-construction lines in `search()`.
- `tests/test_index.py` — new tests for multi-word/NL retrieval, ID-lookup regression, empty query, OR semantics, filter composition.
- `benchmarks/results/**`, `docs/comparison.md` — regenerated/rewritten from the new run (Tasks 2-3).

Run the suite with `uv run pytest -q`.

---

### Task 1: `search()` matches per-term with OR (NL queries retrieve)

**Files:**
- Modify: `src/data_olympus/index.py` (the `search()` method)
- Test: `tests/test_index.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_index.py` (the `status_kb` fixture from conftest has three concepts whose bodies mention "caching": `STD-OLD` (superseded), `STD-NEW` (active, body "Current caching guidance."), `DEC-1` (accepted)):

```python
def test_search_multiword_nl_query_retrieves(status_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(status_kb, source_commit="x")
    # Previously this exact-phrase query matched nothing; now OR-of-terms retrieves.
    hits = idx.search("current rule for caching", limit=10)
    ids = {h.id for h in hits}
    assert "STD-NEW" in ids, f"NL query must retrieve the caching concept; got {ids}"


def test_search_multiword_composes_with_status_filter(status_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(status_kb, source_commit="x")
    hits = idx.search("current rule for caching", limit=10, status="active")
    assert {h.id for h in hits} == {"STD-NEW"}, "status=active must still filter NL-query results"


def test_search_or_semantics_matches_any_term(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    # 'worktree' is in STD-U-001/tooling; 'disagreement' is in STD-U-007.
    hits = idx.search("worktree disagreement", limit=10)
    ids = {h.id for h in hits}
    assert "STD-U-001" in ids and "STD-U-007" in ids, f"OR must match either term; got {ids}"


def test_search_single_term_id_lookup_still_works(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    hits = idx.search("STD-U-002", limit=10)
    assert any(h.id == "STD-U-002" for h in hits)


def test_search_empty_query_returns_empty(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    assert idx.search("   ", limit=10) == []
    assert idx.search("", limit=10) == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_index.py::test_search_multiword_nl_query_retrieves tests/test_index.py::test_search_empty_query_returns_empty -q`
Expected: the NL test FAILS (returns no hits with the current exact-phrase behavior); the empty test currently may error/return oddly. Both must fail before the fix.

- [ ] **Step 3: Implement**

In `src/data_olympus/index.py`, replace the single line:

```python
        safe_query = '"' + query.replace('"', '""') + '"'
        conn = self._connect()
```

with per-term OR construction and an empty-query short-circuit:

```python
        # Quote each whitespace term individually and OR them together. Quoting
        # per-term keeps FTS5 from treating dashes/dots/colons inside a term
        # (e.g. "STD-U-002") as operators, while OR lets multi-word natural-
        # language queries match on any term (ranked by bm25) instead of
        # requiring a verbatim phrase. Doubling embedded quotes prevents a term
        # from breaking out of its phrase, so user input cannot inject FTS
        # operators.
        terms = query.split()
        if not terms:
            return []
        match_query = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
        conn = self._connect()
```

Then change the params line to use `match_query`:

```python
            params: list[object] = [match_query]
```

- [ ] **Step 4: Run to verify they pass + no regressions**

Run: `uv run pytest tests/test_index.py tests/test_tools_read.py -q`
Expected: PASS (all existing single-term search tests still pass; new NL/OR/empty tests pass).

- [ ] **Step 5: Commit**

```bash
git add src/data_olympus/index.py tests/test_index.py
git commit -m "fix(index): match search query per-term with OR so NL queries retrieve"
```

---

### Task 2: Re-run the benchmark with the fixed search

**Files:**
- Regenerate (committed): `benchmarks/results/results.json`, `benchmarks/results/report.md`
- (Corpus and queries are unchanged: same `seed=0`. Only retrieval behavior changed.)

- [ ] **Step 1: Confirm benchmark method/smoke tests still pass under the new search**

Run: `uv run pytest tests/test_bench_methods.py tests/test_bench_run_smoke.py -q`
Expected: PASS (vector-RAG SKIPPED). In particular `test_data_olympus_status_filter_excludes_superseded` must still pass — the active filter still drops superseded concepts even though the query now retrieves more.

- [ ] **Step 2: Regenerate the artifacts**

Run:
```bash
uv run python -m benchmarks.generate_artifacts
uv run data-olympus lint benchmarks/corpus
git --no-pager diff --stat benchmarks/
```
Expected: `benchmarks/results/results.json` and `report.md` change; `benchmarks/corpus/**` and `benchmarks/queries.yaml` are unchanged (same seed). Lint exits 0.

- [ ] **Step 3: Read the new numbers**

Run: `cat benchmarks/results/report.md`
Record the new per-category recall/staleness for data-olympus, especially `status` and `graph` (expected to rise from 0.000) and the staleness rates (the differentiator is expected to become genuine: data-olympus retrieves the active concept and excludes the superseded one, while BM25 still surfaces the stale one on `graph`).

- [ ] **Step 4: Commit the regenerated results**

```bash
git add benchmarks/results
git commit -m "bench: re-run with NL-query search fix (status/graph categories now retrieve)"
```

---

### Task 3: Rewrite the Quantified comparison narrative from the new numbers

**Files:**
- Modify: `docs/comparison.md` (the `## Quantified comparison` section)

This task is HONESTY-CRITICAL and is done by the coordinator, not delegated: copy the new tables verbatim from `benchmarks/results/report.md`, then rewrite the prose (`### Staleness avoidance`, `### Where data-olympus loses`) to match the NEW reality.

- [ ] **Step 1: Replace the per-category table and curve** with the regenerated numbers from `benchmarks/results/report.md`. Do not hand-edit any figure.

- [ ] **Step 2: Rewrite the narrative honestly.** If the fix made the staleness differentiator genuine (data-olympus retrieves on `status`/`graph` and excludes the superseded concept while BM25 surfaces it), say so with the new numbers. If data-olympus still loses on `semantic`, keep that loss stated. Remove the now-stale "vacuous 0.000" explanation if it no longer applies, or keep/adjust it to whatever the new numbers show. The section must continue to name the synthetic corpus, the simple tokenizer, and the excluded vector-RAG.

- [ ] **Step 3: Final full gate**

Run: `uv run pytest -q && uv run ruff check . && uv run data-olympus lint benchmarks/corpus && uv run data-olympus lint example-bundle`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add docs/comparison.md
git commit -m "docs: update quantified comparison after NL-query search fix"
```

---

## Self-Review

- **Spec coverage:** addresses the Part B finding (phrase-only search) directly; keeps the benchmark's honesty discipline (numbers copied from the regenerated report, losses still reported).
- **Regression safety:** existing single-term search tests are unchanged by per-term quoting (a one-token query yields one quoted phrase = prior behavior). New tests cover the multi-word, OR, empty, and filter-composition cases.
- **Injection safety:** each user term is individually quoted with doubled embedded quotes; the `OR` is harness-controlled, not user input.
- **No fabricated numbers:** Task 3 copies from `benchmarks/results/report.md`; the narrative reports whatever the new run shows, including remaining losses.
