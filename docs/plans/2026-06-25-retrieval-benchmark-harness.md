# Retrieval Benchmark Harness Implementation Plan (Part B)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A reproducible, retrieval-only benchmark that measures token cost and retrieval accuracy (incl. staleness avoidance) of data-olympus selective loading versus whole-bundle-dump, grep+read, BM25, and (optional) vector RAG, over a synthetic committed corpus.

**Architecture:** A self-contained `benchmarks/` Python package. Pure-logic modules (tokenizer, chunking, metrics) are dep-free and unit-tested. A deterministic generator emits the corpus and the query set with gold labels. Five "method" adapters each map a query to a retrieval payload. A runner executes all methods over all queries, computes per-category metrics and a token-vs-size curve, and writes a results report that feeds a "Quantified comparison" section in `docs/comparison.md`. The core runs with zero heavy dependencies; `tiktoken` and `sentence-transformers` are optional (`[bench]` extra) and the harness degrades gracefully without them.

**Tech Stack:** Python 3.13, the in-repo `data_olympus.index.Index`, stdlib only for the core; optional `tiktoken` + `sentence-transformers` + `numpy` behind `pip install -e '.[bench]'`. pytest, ruff (CI runs `ruff check .`, so `benchmarks/` must be ruff-clean).

**Part of:** [docs/specs/2026-06-25-retrieval-benchmark-design.md](../specs/2026-06-25-retrieval-benchmark-design.md) §4-§9 (Part B). Depends on Part A (status/type filtering), already landed.

---

## File Structure

```
benchmarks/
  __init__.py
  tokenizer.py        # token counting: dep-free default + optional tiktoken
  chunking.py         # fixed-size token chunker
  metrics.py          # recall@k, precision, ndcg, mrr, staleness_error
  corpus_model.py     # Concept dataclass + corpus load/manifest types
  corpus_gen.py       # deterministic corpus generator
  query_gen.py        # deterministic query+gold-label generator
  methods/
    __init__.py
    base.py           # RetrievalResult + Method protocol + helpers
    whole_dump.py
    grep_read.py
    bm25.py
    data_olympus.py
    vector_rag.py     # optional ([bench]); skipped if deps absent
  run.py              # orchestrator + aggregation + report writer
  corpus/             # GENERATED, committed (created by corpus_gen)
  queries.yaml        # GENERATED, committed (created by query_gen)
  results/            # GENERATED, committed (created by run.py)
  README.md           # how to run + honesty labeling
tests/
  test_bench_tokenizer.py
  test_bench_chunking.py
  test_bench_metrics.py
  test_bench_corpus_gen.py
  test_bench_query_gen.py
  test_bench_methods.py
  test_bench_run_smoke.py
```

`pyproject.toml`: add a `[bench]` optional-dependency group and add `benchmarks` to pytest `pythonpath`.
`docs/comparison.md`: gains a "Quantified comparison" section (Task 11).

Run the suite at any point with `uv run pytest -q`. CI installs only `.[dev]`, so **every benchmark test must import and pass without the `[bench]` extra** (use `pytest.importorskip` for the vector-RAG test).

---

### Task 1: Package skeleton + pyproject wiring

**Files:**
- Create: `benchmarks/__init__.py`, `benchmarks/methods/__init__.py`
- Modify: `pyproject.toml`
- Test: `tests/test_bench_run_smoke.py` (placeholder import test for now)

- [ ] **Step 1: Write the failing test**

Create `tests/test_bench_run_smoke.py`:

```python
"""Smoke tests for the benchmark harness (dep-free)."""
from __future__ import annotations


def test_benchmarks_package_imports() -> None:
    import benchmarks  # noqa: F401
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_bench_run_smoke.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'benchmarks'`

- [ ] **Step 3: Implement**

Create `benchmarks/__init__.py`:

```python
"""data-olympus retrieval benchmark harness (Part B).

Self-contained, retrieval-only. Core modules are dependency-free; tiktoken and
sentence-transformers are optional (install with `pip install -e '.[bench]'`).
"""
```

Create `benchmarks/methods/__init__.py`:

```python
"""Retrieval method adapters under benchmark."""
```

In `pyproject.toml`, add to `[project.optional-dependencies]` (alongside `dev`):

```toml
bench = [
    "tiktoken>=0.8",
    "sentence-transformers>=3.0",
    "numpy>=1.26",
]
```

And extend the pytest pythonpath so `import benchmarks` resolves from the repo root:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = "-ra -q"
pythonpath = ["src", "."]
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_bench_run_smoke.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add benchmarks/__init__.py benchmarks/methods/__init__.py pyproject.toml tests/test_bench_run_smoke.py
git commit -m "feat(bench): scaffold benchmarks package and [bench] extra"
```

---

### Task 2: Tokenizer (dep-free default, optional tiktoken)

**Files:**
- Create: `benchmarks/tokenizer.py`
- Test: `tests/test_bench_tokenizer.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_bench_tokenizer.py`:

```python
from __future__ import annotations

from benchmarks.tokenizer import SimpleTokenizer, get_tokenizer


def test_simple_tokenizer_counts_words_and_punct() -> None:
    tok = SimpleTokenizer()
    # 5 word tokens + 1 period
    assert tok.count("the quick brown fox jumps.") == 6


def test_simple_tokenizer_empty_is_zero() -> None:
    assert SimpleTokenizer().count("") == 0
    assert SimpleTokenizer().count("   \n  ") == 0


def test_simple_tokenizer_is_deterministic() -> None:
    tok = SimpleTokenizer()
    text = "STD-U-002: avoid em-dashes; use commas, please."
    assert tok.count(text) == tok.count(text)


def test_get_tokenizer_default_is_simple() -> None:
    tok = get_tokenizer("simple")
    assert tok.name == "simple"
    assert tok.count("a b c") == 3


def test_get_tokenizer_unknown_raises() -> None:
    import pytest
    with pytest.raises(ValueError, match="unknown tokenizer"):
        get_tokenizer("nope")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_bench_tokenizer.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'benchmarks.tokenizer'`

- [ ] **Step 3: Implement**

Create `benchmarks/tokenizer.py`:

```python
"""Token counting for the benchmark.

Default is a dependency-free deterministic splitter (words + punctuation runs).
`tiktoken` is an optional precision upgrade selected by name. All methods in a
given run use the SAME tokenizer, so token RATIOS are comparable regardless of
which tokenizer is chosen; only absolute counts differ.
"""
from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

_TOKEN_RE = re.compile(r"\w+|[^\w\s]")


@runtime_checkable
class Tokenizer(Protocol):
    name: str

    def count(self, text: str) -> int: ...


class SimpleTokenizer:
    """Dependency-free: count word runs and individual punctuation marks."""

    name = "simple"

    def count(self, text: str) -> int:
        return len(_TOKEN_RE.findall(text))


class TiktokenTokenizer:
    """Optional cl100k_base counts. Requires `pip install -e '.[bench]'`."""

    name = "tiktoken-cl100k"

    def __init__(self) -> None:
        import tiktoken  # lazy: only imported when explicitly requested

        self._enc = tiktoken.get_encoding("cl100k_base")

    def count(self, text: str) -> int:
        return len(self._enc.encode(text))


def get_tokenizer(name: str = "simple") -> Tokenizer:
    if name == "simple":
        return SimpleTokenizer()
    if name in ("tiktoken", "tiktoken-cl100k"):
        return TiktokenTokenizer()
    raise ValueError(f"unknown tokenizer: {name!r}")
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_bench_tokenizer.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add benchmarks/tokenizer.py tests/test_bench_tokenizer.py
git commit -m "feat(bench): token counting (simple default + optional tiktoken)"
```

---

### Task 3: Chunking

**Files:**
- Create: `benchmarks/chunking.py`
- Test: `tests/test_bench_chunking.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_bench_chunking.py`:

```python
from __future__ import annotations

from benchmarks.chunking import chunk_text


def test_chunk_short_text_is_single_chunk() -> None:
    chunks = chunk_text("one two three", size=10, overlap=2)
    assert chunks == ["one two three"]


def test_chunk_splits_on_size() -> None:
    words = " ".join(str(i) for i in range(10))  # 10 whitespace tokens
    chunks = chunk_text(words, size=4, overlap=0)
    assert chunks == ["0 1 2 3", "4 5 6 7", "8 9"]


def test_chunk_overlap_repeats_tail() -> None:
    words = " ".join(str(i) for i in range(6))
    chunks = chunk_text(words, size=4, overlap=2)
    # step = size - overlap = 2
    assert chunks[0] == "0 1 2 3"
    assert chunks[1] == "2 3 4 5"


def test_chunk_empty_returns_empty_list() -> None:
    assert chunk_text("", size=4, overlap=0) == []


def test_chunk_rejects_bad_overlap() -> None:
    import pytest
    with pytest.raises(ValueError):
        chunk_text("a b c", size=2, overlap=2)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_bench_chunking.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'benchmarks.chunking'`

- [ ] **Step 3: Implement**

Create `benchmarks/chunking.py`:

```python
"""Whitespace-token windowed chunking.

Chunk boundaries are on whitespace tokens (not BPE tokens) so chunking is
dependency-free and deterministic. `size`/`overlap` are in whitespace tokens.
"""
from __future__ import annotations


def chunk_text(text: str, *, size: int, overlap: int) -> list[str]:
    if overlap >= size:
        raise ValueError(f"overlap ({overlap}) must be < size ({size})")
    words = text.split()
    if not words:
        return []
    step = size - overlap
    chunks: list[str] = []
    i = 0
    while i < len(words):
        chunks.append(" ".join(words[i : i + size]))
        i += step
    return chunks
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_bench_chunking.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add benchmarks/chunking.py tests/test_bench_chunking.py
git commit -m "feat(bench): deterministic whitespace-token chunker"
```

---

### Task 4: Metrics

**Files:**
- Create: `benchmarks/metrics.py`
- Test: `tests/test_bench_metrics.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_bench_metrics.py`:

```python
from __future__ import annotations

import math

from benchmarks.metrics import (
    mrr,
    ndcg_at_k,
    precision_signal,
    recall_at_k,
    staleness_error,
)


def test_recall_hit_when_gold_in_top_k() -> None:
    assert recall_at_k(["a", "b", "c"], {"b"}, k=2) == 1.0


def test_recall_miss_when_gold_below_k() -> None:
    assert recall_at_k(["a", "b", "c"], {"c"}, k=2) == 0.0


def test_recall_fraction_of_multiple_gold() -> None:
    assert recall_at_k(["a", "b", "c"], {"a", "c"}, k=3) == 1.0
    assert recall_at_k(["a", "b", "d"], {"a", "c"}, k=3) == 0.5


def test_precision_signal_full_when_payload_is_just_gold() -> None:
    # payload tokens == gold tokens -> ratio 1.0
    assert precision_signal(payload_tokens=20, gold_tokens=20, gold_retrieved=True) == 1.0


def test_precision_signal_low_for_huge_payload() -> None:
    p = precision_signal(payload_tokens=1000, gold_tokens=20, gold_retrieved=True)
    assert math.isclose(p, 0.02)


def test_precision_zero_when_gold_not_retrieved() -> None:
    assert precision_signal(payload_tokens=1000, gold_tokens=20, gold_retrieved=False) == 0.0


def test_mrr_first_gold_rank() -> None:
    assert mrr(["a", "b", "c"], {"b"}) == 0.5
    assert mrr(["a", "b", "c"], {"a"}) == 1.0
    assert mrr(["a", "b", "c"], {"z"}) == 0.0


def test_ndcg_perfect_when_gold_first() -> None:
    assert ndcg_at_k(["a", "b", "c"], {"a"}, k=3) == 1.0


def test_ndcg_discounts_lower_rank() -> None:
    # single gold at rank 2: DCG = 1/log2(3); IDCG = 1/log2(2) = 1
    expected = (1 / math.log2(3)) / 1.0
    assert math.isclose(ndcg_at_k(["a", "b", "c"], {"b"}, k=3), expected)


def test_staleness_error_when_stale_ranked_above_current() -> None:
    assert staleness_error(["STALE", "CURRENT"], current_id="CURRENT", stale_id="STALE") == 1


def test_no_staleness_when_current_above_stale() -> None:
    assert staleness_error(["CURRENT", "STALE"], current_id="CURRENT", stale_id="STALE") == 0


def test_no_staleness_when_stale_absent() -> None:
    assert staleness_error(["CURRENT", "OTHER"], current_id="CURRENT", stale_id="STALE") == 0


def test_staleness_error_when_only_stale_present() -> None:
    assert staleness_error(["STALE", "OTHER"], current_id="CURRENT", stale_id="STALE") == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_bench_metrics.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'benchmarks.metrics'`

- [ ] **Step 3: Implement**

Create `benchmarks/metrics.py`:

```python
"""Retrieval metrics. Pure functions, dependency-free, deterministic.

`ranked` is the method's ranked list of concept ids (best first). `gold` is the
set of concept ids that correctly answer the query.
"""
from __future__ import annotations

import math
from collections.abc import Sequence


def recall_at_k(ranked: Sequence[str], gold: set[str], *, k: int) -> float:
    if not gold:
        return 0.0
    top = set(ranked[:k])
    return len(top & gold) / len(gold)


def precision_signal(*, payload_tokens: int, gold_tokens: int, gold_retrieved: bool) -> float:
    """Signal-to-noise: share of the payload that is the relevant concept.

    1.0 means the payload is essentially just the answer; tiny means the answer
    is buried in a large payload (e.g. whole-bundle dump). 0.0 if the gold
    concept was not retrieved at all.
    """
    if not gold_retrieved or payload_tokens <= 0:
        return 0.0
    return min(1.0, gold_tokens / payload_tokens)


def mrr(ranked: Sequence[str], gold: set[str]) -> float:
    for i, doc_id in enumerate(ranked, start=1):
        if doc_id in gold:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranked: Sequence[str], gold: set[str], *, k: int) -> float:
    dcg = 0.0
    for i, doc_id in enumerate(ranked[:k], start=1):
        if doc_id in gold:
            dcg += 1.0 / math.log2(i + 1)
    ideal_hits = min(len(gold), k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    return dcg / idcg if idcg > 0 else 0.0


def staleness_error(ranked: Sequence[str], *, current_id: str, stale_id: str) -> int:
    """1 if the method surfaces the superseded concept at or above the current
    one (or surfaces stale while current is absent), else 0."""
    inf = len(ranked) + 1
    pos_current = ranked.index(current_id) if current_id in ranked else inf
    pos_stale = ranked.index(stale_id) if stale_id in ranked else inf
    if pos_stale == inf:
        return 0
    return 1 if pos_stale <= pos_current else 0
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_bench_metrics.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add benchmarks/metrics.py tests/test_bench_metrics.py
git commit -m "feat(bench): retrieval metrics (recall, precision, ndcg, mrr, staleness)"
```

---

### Task 5: Corpus model + deterministic corpus generator

**Files:**
- Create: `benchmarks/corpus_model.py`, `benchmarks/corpus_gen.py`
- Test: `tests/test_bench_corpus_gen.py`

**Design:** The generator emits N concepts deterministically (seeded). Each concept covers one of a fixed list of `TOPICS` (e.g. "caching", "retries", "pagination", ...). ~15% of concepts are a **supersession pair**: a `superseded` predecessor `STD-OLD-<topic>` and an `active` replacement `STD-NEW-<topic>` with overlapping bodies (both mention the topic) linked by `supersedes`/`superseded_by`. Concepts spread across tiers (`T1,T2,T3,T4,meta`) and types (`standard,decision,workflow,project,reference,memory`). A `CorpusManifest` records, for each topic, which id is current, which (if any) is the stale predecessor, and the concept type/status, so the query generator can author gold labels from ground truth.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_bench_corpus_gen.py`:

```python
from __future__ import annotations

from pathlib import Path

from benchmarks.corpus_gen import generate_corpus
from data_olympus.index import Index


def test_generate_corpus_is_deterministic(tmp_path: Path) -> None:
    a = generate_corpus(tmp_path / "a", n=60, seed=7)
    b = generate_corpus(tmp_path / "b", n=60, seed=7)
    assert [c.id for c in a.concepts] == [c.id for c in b.concepts]


def test_generate_corpus_has_supersession_pairs(tmp_path: Path) -> None:
    manifest = generate_corpus(tmp_path / "kb", n=120, seed=1)
    pairs = [t for t in manifest.topics if t.stale_id is not None]
    assert pairs, "expected at least one supersession pair"
    for t in pairs:
        assert t.current_id != t.stale_id


def test_generated_corpus_lints_clean(tmp_path: Path) -> None:
    from data_olympus.format import discover_bundle_files, lint_files
    root = tmp_path / "kb"
    generate_corpus(root, n=120, seed=1)
    results = lint_files(discover_bundle_files(root))
    errors = [r for r in results for _ in getattr(r, "errors", [])]
    assert not errors, f"generated corpus must lint clean; got {errors}"


def test_generated_corpus_indexes_without_duplicate_ids(tmp_path: Path) -> None:
    root = tmp_path / "kb"
    generate_corpus(root, n=120, seed=1)
    idx = Index(tmp_path / "idx.db")
    result = idx.build(root, source_commit="bench")
    assert result.docs_indexed >= 120


def test_supersession_pair_has_active_and_superseded_status(tmp_path: Path) -> None:
    root = tmp_path / "kb"
    manifest = generate_corpus(root, n=120, seed=1)
    idx = Index(tmp_path / "idx.db")
    idx.build(root, source_commit="bench")
    pair = next(t for t in manifest.topics if t.stale_id is not None)
    cur = idx.get(pair.current_id)
    old = idx.get(pair.stale_id)
    assert cur is not None and cur.status == "active"
    assert old is not None and old.status == "superseded"
```

NOTE for implementer: confirm the actual public names in `data_olympus.format` (`discover_bundle_files`, `lint_files`) and the lint result's error attribute by reading `src/data_olympus/format/__init__.py` and `lint.py`; adjust the `test_generated_corpus_lints_clean` assertion to the real result shape (the intent is "zero error-severity findings"). This is the one place to verify against the real lint API before implementing.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_bench_corpus_gen.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'benchmarks.corpus_gen'`

- [ ] **Step 3: Implement**

Create `benchmarks/corpus_model.py`:

```python
"""Types describing a generated benchmark corpus."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Concept:
    id: str
    path: str          # bundle-relative
    tier: str
    type: str
    status: str
    title: str
    topic: str
    body: str


@dataclass(frozen=True)
class TopicRecord:
    """Ground truth for one topic: the current concept and optional stale one."""

    topic: str
    current_id: str
    current_type: str
    stale_id: str | None = None


@dataclass(frozen=True)
class CorpusManifest:
    concepts: list[Concept] = field(default_factory=list)
    topics: list[TopicRecord] = field(default_factory=list)
```

Create `benchmarks/corpus_gen.py`:

```python
"""Deterministic synthetic corpus generator.

Honesty: this corpus is SYNTHETIC, generated, and does not represent any real
KB. It exists to exercise scale, supersession chains, and type/status diversity
under controlled, reproducible conditions.
"""
from __future__ import annotations

import random
from pathlib import Path

from benchmarks.corpus_model import Concept, CorpusManifest, TopicRecord

TOPICS = [
    "caching", "retries", "pagination", "rate-limiting", "idempotency",
    "logging", "tracing", "secrets-handling", "input-validation", "auth-tokens",
    "database-migrations", "connection-pooling", "feature-flags", "circuit-breakers",
    "message-ordering", "schema-evolution", "backpressure", "graceful-shutdown",
    "health-checks", "config-reloading", "error-budgets", "canary-rollouts",
    "blue-green-deploys", "dead-letter-queues", "saga-orchestration",
]

# (tier, type) assignments cycle so the corpus spans all tiers and types.
_TIERS = ["T1", "T2", "T3", "T4", "meta"]
_TYPES = ["standard", "decision", "workflow", "project", "reference", "memory"]
_DIR_FOR_TIER = {
    "T1": "universal/foundation",
    "T2": "tech-stacks/backend-nestjs",
    "T3": "projects/example-project",
    "T4": "projects/example-project/components/api",
    "meta": "decisions",
}

_SUPERSEDE_FRACTION = 0.15


def _body(topic: str, qualifier: str) -> str:
    return (
        f"# {topic} ({qualifier})\n\n"
        f"This concept defines the {qualifier} guidance for {topic}. "
        f"When working with {topic}, follow the {qualifier} rules below. "
        f"The {topic} approach affects reliability and developer ergonomics.\n\n"
        f"- Prefer the documented {topic} pattern.\n"
        f"- Record exceptions to the {topic} rule.\n"
    )


def generate_corpus(dest: Path, *, n: int = 250, seed: int = 0) -> CorpusManifest:
    rng = random.Random(seed)
    concepts: list[Concept] = []
    topics: list[TopicRecord] = []

    count = 0
    topic_idx = 0
    while count < n:
        topic = TOPICS[topic_idx % len(TOPICS)]
        # Disambiguate repeated topics across cycles.
        suffix = topic_idx // len(TOPICS)
        topic_key = topic if suffix == 0 else f"{topic}-{suffix}"
        tier = _TIERS[topic_idx % len(_TIERS)]
        ctype = _TYPES[topic_idx % len(_TYPES)]
        directory = _DIR_FOR_TIER[tier]
        make_pair = rng.random() < _SUPERSEDE_FRACTION and count + 1 < n

        if make_pair:
            old_id = f"BENCH-OLD-{topic_key}".upper()
            new_id = f"BENCH-NEW-{topic_key}".upper()
            old = Concept(
                id=old_id, path=f"{directory}/{old_id}.md", tier=tier, type=ctype,
                status="superseded", title=f"{topic_key} (old)", topic=topic_key,
                body=_body(topic_key, "previous"),
            )
            new = Concept(
                id=new_id, path=f"{directory}/{new_id}.md", tier=tier, type=ctype,
                status="active", title=f"{topic_key} (current)", topic=topic_key,
                body=_body(topic_key, "current"),
            )
            concepts.extend([old, new])
            topics.append(TopicRecord(topic_key, current_id=new_id, current_type=ctype,
                                      stale_id=old_id))
            count += 2
        else:
            cid = f"BENCH-{topic_key}".upper()
            status = "active" if ctype != "decision" else "accepted"
            concepts.append(Concept(
                id=cid, path=f"{directory}/{cid}.md", tier=tier, type=ctype,
                status=status, title=f"{topic_key}", topic=topic_key,
                body=_body(topic_key, "current"),
            ))
            topics.append(TopicRecord(topic_key, current_id=cid, current_type=ctype,
                                      stale_id=None))
            count += 1
        topic_idx += 1

    _write(dest, concepts)
    return CorpusManifest(concepts=concepts, topics=topics)


def _frontmatter(c: Concept) -> str:
    lines = [
        "---",
        f"id: {c.id}",
        f"type: {c.type}",
        f"status: {c.status}",
        f"tier: {c.tier}",
        f"title: {c.title}",
    ]
    return "\n".join(lines) + "\n---\n\n"


def _write(dest: Path, concepts: list[Concept]) -> None:
    for c in concepts:
        p = dest / c.path
        p.parent.mkdir(parents=True, exist_ok=True)
        # supersession links are recorded in frontmatter for the pair
        extra = ""
        if c.id.startswith("BENCH-OLD-"):
            new_id = c.id.replace("BENCH-OLD-", "BENCH-NEW-")
            extra = f"superseded_by: {new_id}\n"
        elif c.id.startswith("BENCH-NEW-"):
            old_id = c.id.replace("BENCH-NEW-", "BENCH-OLD-")
            extra = f"supersedes: {old_id}\n"
        fm = _frontmatter(c)
        if extra:
            fm = fm[:-5] + extra + "---\n\n"  # insert before closing fence
        p.write_text(fm + c.body, encoding="utf-8")
```

NOTE for implementer: the `_write` frontmatter-fence splice is fragile; implement it so the closing `---` lands correctly (a clean approach: build the frontmatter field lines as a list including `extra`, then join). Verify with `test_supersession_pair_has_active_and_superseded_status`. Keep the corpus lint-clean (required fields: id, type, status, tier — all present).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_bench_corpus_gen.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add benchmarks/corpus_model.py benchmarks/corpus_gen.py tests/test_bench_corpus_gen.py
git commit -m "feat(bench): deterministic synthetic corpus generator"
```

---

### Task 6: Query generator + gold labels

**Files:**
- Create: `benchmarks/query_gen.py`
- Test: `tests/test_bench_query_gen.py`

**Design:** From a `CorpusManifest`, emit queries across four categories, each with a gold id (the topic's `current_id`) and, where applicable, the `stale_id`:
- `exact`: literal topic term ("caching").
- `semantic`: a paraphrase with low literal overlap (a fixed synonym map; the category data-olympus is expected to lose).
- `status`: "current rule for <topic>" for topics that have a stale predecessor (gold = current_id, stale = stale_id).
- `graph`: "what replaced <stale title>" for supersession pairs (gold = current_id).

A `BenchQuery` has: `text`, `category`, `gold_ids: list[str]`, `current_id`, `stale_id`. `write_queries(manifest, path)` serializes to YAML; `load_queries(path)` reads them back.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_bench_query_gen.py`:

```python
from __future__ import annotations

from pathlib import Path

from benchmarks.corpus_gen import generate_corpus
from benchmarks.query_gen import build_queries, load_queries, write_queries


def test_build_queries_covers_all_categories(tmp_path: Path) -> None:
    manifest = generate_corpus(tmp_path / "kb", n=150, seed=3)
    queries = build_queries(manifest)
    cats = {q.category for q in queries}
    assert {"exact", "semantic", "status", "graph"} <= cats


def test_status_queries_carry_stale_id(tmp_path: Path) -> None:
    manifest = generate_corpus(tmp_path / "kb", n=150, seed=3)
    status_qs = [q for q in build_queries(manifest) if q.category == "status"]
    assert status_qs
    for q in status_qs:
        assert q.stale_id is not None
        assert q.gold_ids == [q.current_id]


def test_gold_ids_exist_in_corpus(tmp_path: Path) -> None:
    manifest = generate_corpus(tmp_path / "kb", n=150, seed=3)
    valid = {c.id for c in manifest.concepts}
    for q in build_queries(manifest):
        for gid in q.gold_ids:
            assert gid in valid, f"gold id {gid} not in corpus"


def test_write_then_load_roundtrips(tmp_path: Path) -> None:
    manifest = generate_corpus(tmp_path / "kb", n=80, seed=2)
    queries = build_queries(manifest)
    out = tmp_path / "queries.yaml"
    write_queries(queries, out)
    loaded = load_queries(out)
    assert [q.text for q in loaded] == [q.text for q in queries]
    assert [q.gold_ids for q in loaded] == [q.gold_ids for q in queries]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_bench_query_gen.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'benchmarks.query_gen'`

- [ ] **Step 3: Implement**

Create `benchmarks/query_gen.py`. Implement `BenchQuery` (frozen dataclass with `text, category, gold_ids, current_id, stale_id`), a fixed `_SEMANTIC` synonym map keyed by base topic (e.g. `"caching": "storing computed results to avoid recomputation"`; provide entries for every base topic in `TOPICS`, falling back to `f"approach for {topic}"` when a topic key has a numeric suffix), and:

```python
def build_queries(manifest: CorpusManifest) -> list[BenchQuery]:
    queries: list[BenchQuery] = []
    for t in manifest.topics:
        base = t.topic.split("-")[0] if t.topic[-1].isdigit() else t.topic
        # exact
        queries.append(BenchQuery(
            text=t.topic.replace("-", " "), category="exact",
            gold_ids=[t.current_id], current_id=t.current_id, stale_id=t.stale_id,
        ))
        # semantic
        queries.append(BenchQuery(
            text=_semantic_phrase(base), category="semantic",
            gold_ids=[t.current_id], current_id=t.current_id, stale_id=t.stale_id,
        ))
        if t.stale_id is not None:
            queries.append(BenchQuery(
                text=f"current rule for {t.topic.replace('-', ' ')}", category="status",
                gold_ids=[t.current_id], current_id=t.current_id, stale_id=t.stale_id,
            ))
            queries.append(BenchQuery(
                text=f"what replaced the previous {t.topic.replace('-', ' ')} guidance",
                category="graph", gold_ids=[t.current_id],
                current_id=t.current_id, stale_id=t.stale_id,
            ))
    return queries
```

`write_queries`/`load_queries` use `yaml` (already a dependency via `pyyaml`). Serialize each query as a mapping with keys `text, category, gold_ids, current_id, stale_id`. `load_queries` reconstructs `BenchQuery` objects.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_bench_query_gen.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add benchmarks/query_gen.py tests/test_bench_query_gen.py
git commit -m "feat(bench): deterministic query + gold-label generator"
```

---

### Task 7: Method protocol + zero-dep methods (whole_dump, grep_read, bm25, data_olympus)

**Files:**
- Create: `benchmarks/methods/base.py`, `whole_dump.py`, `grep_read.py`, `bm25.py`, `data_olympus.py`
- Test: `tests/test_bench_methods.py`

**Design:** `RetrievalResult` is a frozen dataclass: `payload_text: str`, `ranked_ids: list[str]` (best-first, deduped), `retrieved_ids: set[str]`. Each method exposes `name: str` and `retrieve(query: str) -> RetrievalResult`. Methods are constructed with the corpus root (and, for data_olympus, a built `Index`). The data_olympus method calls the REAL `Index`: `outline()` for a cheap map, `search(query, limit=5, status="active")`, then `get(top_hit.id)`; payload = outline text + hit snippets + top doc body; `ranked_ids` = the active-filtered search hit ids.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_bench_methods.py`:

```python
from __future__ import annotations

from pathlib import Path

from benchmarks.corpus_gen import generate_corpus
from benchmarks.methods.base import RetrievalResult
from benchmarks.methods.bm25 import Bm25Method
from benchmarks.methods.data_olympus import DataOlympusMethod
from benchmarks.methods.grep_read import GrepReadMethod
from benchmarks.methods.whole_dump import WholeDumpMethod
from data_olympus.index import Index


def _corpus(tmp_path: Path):
    root = tmp_path / "kb"
    manifest = generate_corpus(root, n=120, seed=5)
    return root, manifest


def test_whole_dump_returns_everything(tmp_path: Path) -> None:
    root, manifest = _corpus(tmp_path)
    m = WholeDumpMethod(root)
    res = m.retrieve("caching")
    assert isinstance(res, RetrievalResult)
    assert len(res.retrieved_ids) == len(manifest.concepts)


def test_grep_read_finds_topic(tmp_path: Path) -> None:
    root, manifest = _corpus(tmp_path)
    m = GrepReadMethod(root)
    res = m.retrieve("caching")
    assert any("CACHING" in rid for rid in res.retrieved_ids)


def test_bm25_ranks_topic_concept_first(tmp_path: Path) -> None:
    root, _ = _corpus(tmp_path)
    m = Bm25Method(root, k=5)
    res = m.retrieve("pagination")
    assert res.ranked_ids
    assert "PAGINATION" in res.ranked_ids[0]


def test_data_olympus_status_filter_excludes_superseded(tmp_path: Path) -> None:
    root, manifest = _corpus(tmp_path)
    idx = Index(tmp_path / "idx.db")
    idx.build(root, source_commit="bench")
    m = DataOlympusMethod(idx)
    pair = next(t for t in manifest.topics if t.stale_id is not None)
    res = m.retrieve(pair.topic.replace("-", " "))
    assert pair.stale_id not in res.ranked_ids, "active filter must drop the superseded concept"


def test_data_olympus_payload_smaller_than_dump(tmp_path: Path) -> None:
    from benchmarks.tokenizer import SimpleTokenizer
    root, _ = _corpus(tmp_path)
    idx = Index(tmp_path / "idx.db")
    idx.build(root, source_commit="bench")
    tok = SimpleTokenizer()
    do = DataOlympusMethod(idx).retrieve("caching")
    dump = WholeDumpMethod(root).retrieve("caching")
    assert tok.count(do.payload_text) < tok.count(dump.payload_text)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_bench_methods.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'benchmarks.methods.base'`

- [ ] **Step 3: Implement**

Create `benchmarks/methods/base.py`:

```python
"""Common types for retrieval methods."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetrievalResult:
    payload_text: str
    ranked_ids: list[str]   # best-first, deduped
    retrieved_ids: set[str]


def dedupe(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out
```

Create the four method modules. Implementation notes (each constructed with what it needs; pin behavior with the tests above):

- `whole_dump.py` — `WholeDumpMethod(root: Path)`. `retrieve` reads every `*.md` under root, concatenates bodies, `retrieved_ids` = all ids (derive id from frontmatter via `data_olympus.markdown_parse.parse_file`), `ranked_ids` = ids in sorted path order.
- `grep_read.py` — `GrepReadMethod(root: Path)`. Lowercase the query into content terms (split on non-word, drop terms shorter than 3 chars). A file matches if any term appears (case-insensitive) in its text. Payload = concat of whole matched files; `retrieved_ids`/`ranked_ids` from matched files in path order.
- `bm25.py` — `Bm25Method(root: Path, k: int = 5)`. On init, chunk every doc with `chunk_text(body, size=512, overlap=64)`, keep `(doc_id, chunk_text)`. Implement BM25 (k1=1.5, b=0.75) over the chunk corpus with whitespace-lowercased terms. `retrieve` scores chunks, takes top-k, payload = those chunk texts, `ranked_ids` = `dedupe([doc_id for chunk in top_k])`, `retrieved_ids` = set thereof.
- `data_olympus.py` — `DataOlympusMethod(idx: Index, limit: int = 5)`. `retrieve`:
  ```python
  def retrieve(self, query: str) -> RetrievalResult:
      outline_text = _render_outline(self._idx.outline())
      hits = self._idx.search(query, limit=self._limit, status="active")
      ranked = dedupe([h.id for h in hits])
      parts = [outline_text]
      parts.extend(f"{h.title}: {h.snippet}" for h in hits)
      if hits:
          top = self._idx.get(hits[0].id)
          if top is not None:
              parts.append(top.content_markdown)
      return RetrievalResult(
          payload_text="\n".join(parts),
          ranked_ids=ranked,
          retrieved_ids=set(ranked),
      )
  ```
  where `_render_outline` turns the outline list into a short text map (`"<tier>: <category>(<count>) ..."`).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_bench_methods.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add benchmarks/methods/ tests/test_bench_methods.py
git commit -m "feat(bench): retrieval methods (dump, grep, bm25, data-olympus)"
```

---

### Task 8: Optional vector-RAG method (skipped without [bench])

**Files:**
- Create: `benchmarks/methods/vector_rag.py`
- Test: add to `tests/test_bench_methods.py`

- [ ] **Step 1: Write the failing test (import-skipped without deps)**

Append to `tests/test_bench_methods.py`:

```python
def test_vector_rag_ranks_topic_when_available(tmp_path: Path) -> None:
    import pytest
    pytest.importorskip("sentence_transformers")
    from benchmarks.methods.vector_rag import VectorRagMethod
    root, _ = _corpus(tmp_path)
    m = VectorRagMethod(root, k=5)
    res = m.retrieve("pagination")
    assert res.ranked_ids  # at least returns something
```

- [ ] **Step 2: Run to verify it fails (or skips)**

Run: `uv run pytest tests/test_bench_methods.py::test_vector_rag_ranks_topic_when_available -q`
Expected: SKIP if `sentence_transformers` not installed (the import inside the module doesn't exist yet, but `importorskip` skips before reaching it). If `[bench]` IS installed, expect FAIL (`No module named 'benchmarks.methods.vector_rag'`).

- [ ] **Step 3: Implement**

Create `benchmarks/methods/vector_rag.py`. Lazy-import `sentence_transformers` and `numpy` inside `__init__`; raise a clear `RuntimeError("vector_rag requires `pip install -e '.[bench]'`")` if unavailable. On init: chunk all docs (same `chunk_text(512, 64)`), embed chunks with `SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")` (pinned), store the matrix. `retrieve`: embed the query, cosine-rank chunks, top-k, payload = those chunk texts, `ranked_ids = dedupe([doc_id ...])`. Same `RetrievalResult` shape as the others.

- [ ] **Step 4: Run to verify it passes/skips**

Run: `uv run pytest tests/test_bench_methods.py -q`
Expected: PASS (the new test SKIPS when `[bench]` is absent; all other method tests pass).

- [ ] **Step 5: Commit**

```bash
git add benchmarks/methods/vector_rag.py tests/test_bench_methods.py
git commit -m "feat(bench): optional vector-RAG method behind [bench] extra"
```

---

### Task 9: Runner + aggregation + smoke test

**Files:**
- Create: `benchmarks/run.py`
- Test: extend `tests/test_bench_run_smoke.py`

**Design:** `run_benchmark(corpus_root, queries, methods, tokenizer, k=5) -> BenchReport` runs every method over every query, computes per-query metrics (tokens via tokenizer, recall@k, precision_signal, ndcg@k, mrr, and staleness_error on `status`/`graph` queries), and aggregates per (method, category). `BenchReport` holds the aggregate rows + a token-vs-size curve (mean tokens per method over corpus subsets of sizes [25, 50, 100, 250] — re-generate + rebuild per size). `write_report(report, results_dir)` writes `results.json` (machine) and `report.md` (human, with the per-category table, staleness rates, token curve, and a "where data-olympus loses" subsection driven by the `semantic` category numbers). The runner exposes a `main()` CLI: `python -m benchmarks.run [--tokenizer simple|tiktoken] [--n 250] [--with-rag]`.

`precision_signal` needs `gold_tokens` (tokens of the gold concept's body) — the runner gets the gold concept text from the built `Index.get(gold_id)`.

- [ ] **Step 1: Write the failing smoke test**

Append to `tests/test_bench_run_smoke.py`:

```python
from pathlib import Path


def test_run_benchmark_smoke_dep_free(tmp_path: Path) -> None:
    from benchmarks.corpus_gen import generate_corpus
    from benchmarks.methods.bm25 import Bm25Method
    from benchmarks.methods.data_olympus import DataOlympusMethod
    from benchmarks.methods.grep_read import GrepReadMethod
    from benchmarks.methods.whole_dump import WholeDumpMethod
    from benchmarks.query_gen import build_queries
    from benchmarks.run import run_benchmark
    from benchmarks.tokenizer import SimpleTokenizer
    from data_olympus.index import Index

    root = tmp_path / "kb"
    manifest = generate_corpus(root, n=60, seed=4)
    idx = Index(tmp_path / "idx.db")
    idx.build(root, source_commit="bench")

    methods = [
        DataOlympusMethod(idx),
        WholeDumpMethod(root),
        GrepReadMethod(root),
        Bm25Method(root, k=5),
    ]
    queries = build_queries(manifest)[:12]
    report = run_benchmark(
        corpus_root=root, idx=idx, queries=queries, methods=methods,
        tokenizer=SimpleTokenizer(), k=5, curve_sizes=(25, 50),
    )
    rows = {r.method for r in report.rows}
    assert "data-olympus" in rows
    # data-olympus mean tokens should beat whole-bundle dump
    do = next(r for r in report.rows if r.method == "data-olympus" and r.category == "ALL")
    dump = next(r for r in report.rows if r.method == "whole-dump" and r.category == "ALL")
    assert do.mean_tokens < dump.mean_tokens


def test_run_writes_report(tmp_path: Path) -> None:
    from benchmarks.corpus_gen import generate_corpus
    from benchmarks.methods.whole_dump import WholeDumpMethod
    from benchmarks.methods.data_olympus import DataOlympusMethod
    from benchmarks.query_gen import build_queries
    from benchmarks.run import run_benchmark, write_report
    from benchmarks.tokenizer import SimpleTokenizer
    from data_olympus.index import Index

    root = tmp_path / "kb"
    manifest = generate_corpus(root, n=40, seed=4)
    idx = Index(tmp_path / "idx.db")
    idx.build(root, source_commit="bench")
    report = run_benchmark(
        corpus_root=root, idx=idx, queries=build_queries(manifest)[:8],
        methods=[DataOlympusMethod(idx), WholeDumpMethod(root)],
        tokenizer=SimpleTokenizer(), k=5, curve_sizes=(25,),
    )
    out = tmp_path / "results"
    write_report(report, out)
    assert (out / "results.json").exists()
    assert (out / "report.md").exists()
    assert "Quantified" in (out / "report.md").read_text() or "data-olympus" in (out / "report.md").read_text()
```

NOTE: the method label strings (`"data-olympus"`, `"whole-dump"`, `"grep-read"`, `"bm25"`, `"vector-rag"`) must be the `name` attribute on each method class. Set them in Task 7/8 implementations accordingly (the methods' `name` is the row key).

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_bench_run_smoke.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'benchmarks.run'`

- [ ] **Step 3: Implement**

Create `benchmarks/run.py` with: an `AggRow` dataclass (`method, category, mean_tokens, recall, precision, ndcg, mrr, staleness, n`), a `BenchReport` (`rows: list[AggRow]`, `curve: dict[str, list[tuple[int, float]]]`, `tokenizer_name: str`, `rag_included: bool`), `run_benchmark(...)`, `write_report(...)`, and `main()`. Compute per-query metrics, aggregate per (method, category) plus a synthetic `category="ALL"`. The token curve regenerates the corpus at each `curve_sizes` value, rebuilds an `Index`, and records mean payload tokens per method over the same queries. `report.md` must include: a per-category table, a staleness-rate line per method, the token curve, and an explicit "### Where data-olympus loses" subsection populated from the `semantic` category rows.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_bench_run_smoke.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add benchmarks/run.py tests/test_bench_run_smoke.py
git commit -m "feat(bench): runner, aggregation, and report writer"
```

---

### Task 10: Generate committed corpus, queries, and a baseline results report

**Files:**
- Create (generated, committed): `benchmarks/corpus/**`, `benchmarks/queries.yaml`, `benchmarks/results/results.json`, `benchmarks/results/report.md`
- Create: `benchmarks/README.md`, `benchmarks/generate_artifacts.py` (one-shot writer used to produce the committed artifacts)

- [ ] **Step 1: Write a generator entrypoint**

Create `benchmarks/generate_artifacts.py` that: generates the corpus (`n=250, seed=0`) into `benchmarks/corpus/`, writes `benchmarks/queries.yaml`, builds an `Index`, runs `run_benchmark` with the `SimpleTokenizer` over ALL queries and `curve_sizes=(25,50,100,250)`, and writes `benchmarks/results/`. It must be runnable as `python -m benchmarks.generate_artifacts`.

- [ ] **Step 2: Produce the artifacts**

Run:
```bash
uv run python -m benchmarks.generate_artifacts
uv run data-olympus lint benchmarks/corpus
```
Expected: artifacts written; `lint benchmarks/corpus` exits 0 (prints `0 errors`).

- [ ] **Step 3: Write `benchmarks/README.md`**

Document: what the benchmark measures, the honesty caveats (synthetic corpus; simple tokenizer by default, `tiktoken`/`[bench]` for closer counts; vector-RAG optional and expected to win on `semantic`), and the exact commands:
```bash
uv pip install -e '.[bench]'      # optional: enables tiktoken + vector-RAG
uv run python -m benchmarks.generate_artifacts
uv run python -m benchmarks.run --tokenizer tiktoken --with-rag   # optional richer run
```

- [ ] **Step 4: Verify the committed report is internally consistent**

Run: `uv run python - <<'PY'
import json
d = json.load(open("benchmarks/results/results.json"))
print("methods:", sorted({r["method"] for r in d["rows"]}))
PY`
Expected: includes at least `data-olympus`, `whole-dump`, `grep-read`, `bm25`.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/corpus benchmarks/queries.yaml benchmarks/results benchmarks/README.md benchmarks/generate_artifacts.py
git commit -m "feat(bench): commit generated corpus, queries, and baseline results"
```

---

### Task 11: Add the "Quantified comparison" section to docs/comparison.md

**Files:**
- Modify: `docs/comparison.md`

- [ ] **Step 1: Read the committed results**

Read `benchmarks/results/report.md` to get the actual numbers from Task 10.

- [ ] **Step 2: Add the section**

Insert a new `## Quantified comparison` section after the "Summary table" section (before the per-tool comparisons). It must:
- State the methodology in two sentences and link to `benchmarks/README.md` and the spec.
- Include the per-category table (token ratios as headline, accuracy metrics, staleness rate) copied from the results.
- Include the token-vs-corpus-size curve data.
- Keep an explicit "where data-olympus loses" sentence citing the `semantic` category.
- Label the corpus synthetic and the tokenizer used.

Do NOT hand-invent numbers — copy them from `benchmarks/results/report.md`. If a number is not in the report, it does not go in the doc.

- [ ] **Step 3: Final full gate**

Run: `uv run pytest -q && uv run ruff check . && uv run data-olympus lint benchmarks/corpus && uv run data-olympus lint example-bundle`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add docs/comparison.md
git commit -m "docs: add quantified comparison section from benchmark results"
```

---

## Self-Review

- **Spec coverage:** five methods (Tasks 7-8), synthetic corpus with supersession (Task 5), four query categories incl. adversarial `semantic` (Task 6), metrics incl. tokens + staleness (Task 4), token-vs-size curve (Task 9), committed reproducible artifacts (Task 10), comparison.md section (Task 11), honesty labeling (Tasks 5/10 README). All §4-§9 map to a task.
- **CI safety:** every test is dep-free except the vector-RAG test, which uses `pytest.importorskip`. The default tokenizer needs no deps. `ruff check .` covers `benchmarks/`. CI install (`.[dev]`) never needs `[bench]`.
- **No fabricated numbers:** Task 11 copies numbers from the committed `report.md`; the runner computes them from real retrieval over the committed corpus.
- **Type consistency:** `RetrievalResult(payload_text, ranked_ids, retrieved_ids)` and method `.name` strings (`data-olympus`/`whole-dump`/`grep-read`/`bm25`/`vector-rag`) are used identically across methods, runner, and the smoke test's row lookups.
- **Known fragile spots flagged for the implementer:** the corpus frontmatter-fence splice (Task 5), and the real `data_olympus.format` lint API names (Task 5 note) — both must be verified against the actual code before implementing.
