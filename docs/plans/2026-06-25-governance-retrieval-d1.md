# Governance Retrieval Mechanism Implementation Plan (Part D1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add curated `applies_when` trigger metadata and `description` to the retrieval surface, indexed with column-weighted bm25, so coding-intent queries retrieve the governing rule; make the search feature set toggleable so D2 can ablate it.

**Architecture:** Reuse the existing yaml-based `parse_frontmatter` in the index parse path (replacing the weak hand-rolled parser), add `applies_when`/`description` as indexed FTS columns, weight columns in `bm25()`, and add optional `columns`/`column_weights` parameters to `search()` for ablation. Production defaults weight title/`applies_when` highest, body lowest.

**Tech Stack:** Python 3.13, SQLite FTS5, pyyaml (already a dependency), pytest, ruff, mypy strict.

**Part of:** [docs/specs/2026-06-25-governance-retrieval-design.md](../specs/2026-06-25-governance-retrieval-design.md) §4 (D1). D2 (the governance benchmark + ablation) is planned separately against this landed API.

---

## File Structure

- `src/data_olympus/markdown_parse.py` — reuse `parse_frontmatter`; add `applies_when`/`description` to `ParsedDoc`; delete the hand-rolled parser internals.
- `src/data_olympus/index.py` — FTS + docs schema columns, `build()` population, weighted/ablatable `search()`, snippet column index fix, `_SCHEMA_VERSION` bump to `"5"`, `SearchHit`/`IndexedDoc` fields.
- `src/data_olympus/models.py`, `src/data_olympus/tools_read.py` — surface `applies_when`/`description` on `GetResponse`.
- `README.md`, `docs/comparison.md` — governance positioning.
- Tests: `tests/test_markdown_parse.py`, `tests/test_index.py`, `tests/test_tools_read.py`.

Run the suite with `uv run pytest -q`.

---

### Task 1: Parser reuses yaml `parse_frontmatter`; adds `applies_when` + `description`

**Files:**
- Modify: `src/data_olympus/markdown_parse.py`
- Test: `tests/test_markdown_parse.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_markdown_parse.py`:

```python
def test_parse_file_extracts_applies_when_list(tmp_path: Path) -> None:
    p = tmp_path / "x.md"
    p.write_text(
        "---\nid: STD-1\ntier: T1\napplies_when:\n  - openpyxl\n  - insert_cols\n"
        "description: Use xlsxwriter for new Excel files.\n---\n# Body\n"
    )
    doc = parse_file(p)
    assert doc.applies_when == ["openpyxl", "insert_cols"]
    assert doc.description == "Use xlsxwriter for new Excel files."


def test_parse_file_applies_when_inline_list(tmp_path: Path) -> None:
    p = tmp_path / "y.md"
    p.write_text("---\nid: STD-2\ntier: T1\napplies_when: [excel, xlsx]\n---\n# B\n")
    doc = parse_file(p)
    assert doc.applies_when == ["excel", "xlsx"]


def test_parse_file_applies_when_and_description_default_empty(tmp_path: Path) -> None:
    p = tmp_path / "z.md"
    p.write_text("---\nid: STD-3\ntier: T1\n---\n# B\n")
    doc = parse_file(p)
    assert doc.applies_when == []
    assert doc.description == ""


def test_parse_file_multiline_description(tmp_path: Path) -> None:
    p = tmp_path / "m.md"
    p.write_text(
        "---\nid: STD-4\ntier: T1\ndescription: >\n  First line\n  second line.\n---\n# B\n"
    )
    doc = parse_file(p)
    assert "First line second line." in doc.description
```

The existing tests in this file (front-matter present/absent, malformed-lenient, git_remote_url with colon, status/type) must continue to pass unchanged.

- [ ] **Step 2: Run to verify the new tests fail**

Run: `uv run pytest tests/test_markdown_parse.py::test_parse_file_extracts_applies_when_list -q`
Expected: FAIL — `AttributeError: 'ParsedDoc' object has no attribute 'applies_when'`

- [ ] **Step 3: Implement**

In `src/data_olympus/markdown_parse.py`: delete `_FM_RE`, `_LIST_RE`, and `_parse_front_matter`, import the yaml parser, add the two fields, and rewrite `parse_file` to reuse `parse_frontmatter` with lenient fallback:

```python
"""Front-matter parsing for the index. Reuses the yaml-based parser from
data_olympus.format.frontmatter, with lenient failure (malformed -> empty)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from data_olympus.format.frontmatter import parse_frontmatter

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class ParsedDoc:
    """A parsed markdown document with optional front-matter fields."""

    path: Path
    id: str
    tier: str
    category: str
    tags: list[str] = field(default_factory=list)
    title: str = ""
    body: str = ""
    git_remote_url: str | None = None
    status: str = ""
    doc_type: str = ""
    applies_when: list[str] = field(default_factory=list)
    description: str = ""


def _as_str_list(value: object) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def parse_file(path: Path) -> ParsedDoc:
    """Read a markdown file and return a ParsedDoc.

    Raises FileNotFoundError if path does not exist. Malformed front matter is
    treated as no front matter (lenient): returns empty metadata fields.
    """
    text = path.read_text(encoding="utf-8")
    try:
        fm, body = parse_frontmatter(text)
    except ValueError:
        fm, body = {}, text

    id_value = fm.get("id", "")
    if not isinstance(id_value, str) or ":" in id_value:
        id_value = ""

    git_remote_url = fm.get("git_remote_url")
    if not isinstance(git_remote_url, str) or not git_remote_url.strip():
        git_remote_url = None

    return ParsedDoc(
        path=path,
        id=id_value,
        tier=str(fm.get("tier", "")),
        category=str(fm.get("category", "")),
        tags=_as_str_list(fm.get("tags", [])),
        title=str(fm.get("title", "")),
        body=body,
        git_remote_url=git_remote_url,
        status=str(fm.get("status", "")),
        doc_type=str(fm.get("type", "")),
        applies_when=_as_str_list(fm.get("applies_when", [])),
        description=str(fm.get("description", "")) if fm.get("description") is not None else "",
    )
```

Note: `str(fm.get("tier", ""))` etc. coerce scalars to str as before. The `":" in id_value` guard is preserved for parity. `description` guards `None` so a bare `description:` key yields `""`, not `"None"`.

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS (new parser tests pass; all existing tests, including index/lint/tools, stay green). If any existing test regresses, investigate the YAML-vs-handparser difference before proceeding (do not weaken the test).

- [ ] **Step 5: Commit**

```bash
git add src/data_olympus/markdown_parse.py tests/test_markdown_parse.py
git commit -m "refactor(index): parse frontmatter via yaml; add applies_when and description"
```

---

### Task 2: Index `applies_when` + `description` columns; fix snippet index; schema v5

**Files:**
- Modify: `src/data_olympus/index.py`
- Test: `tests/test_index.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_index.py`:

```python
def test_fts_indexes_applies_when_and_description(tmp_path: Path, tmp_index_path: Path) -> None:
    kb = tmp_path / "kb"
    (kb / "universal" / "foundation").mkdir(parents=True)
    (kb / "universal" / "foundation" / "STD-XL.md").write_text(
        "---\nid: STD-XL\ntier: T1\ntype: standard\nstatus: active\n"
        "applies_when: [openpyxl, insert_cols, spreadsheet]\n"
        "description: Prefer xlsxwriter for new Excel files.\n---\n"
        "# Excel standard\n\nUse the documented Excel approach.\n"
    )
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    # A query term that appears ONLY in applies_when must retrieve the doc.
    hits = idx.search("openpyxl", limit=10)
    assert any(h.id == "STD-XL" for h in hits), "applies_when trigger must be searchable"
    # A query term that appears ONLY in description must retrieve the doc.
    hits2 = idx.search("xlsxwriter", limit=10)
    assert any(h.id == "STD-XL" for h in hits2), "description must be searchable"


def test_docs_table_has_applies_when_and_description_columns(
    tmp_kb: Path, tmp_index_path: Path
) -> None:
    import sqlite3
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    conn = sqlite3.connect(tmp_index_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(docs)").fetchall()}
    conn.close()
    assert {"applies_when", "description"} <= cols
```

Update the existing `test_index_records_schema_version` assertion from `"4"` to `"5"`.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_index.py::test_fts_indexes_applies_when_and_description -q`
Expected: FAIL — the trigger-only query returns no hits (applies_when not indexed).

- [ ] **Step 3: Implement**

In `src/data_olympus/index.py`:

a) Extend `_SCHEMA`. Add `applies_when TEXT` and `description TEXT` to the `docs` table, and add `applies_when` and `description` columns to the `fts` virtual table (place them between `tags` and `body` so column order is `id, title, tags, applies_when, description, body`):

```python
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    id UNINDEXED,
    title,
    tags,
    applies_when,
    description,
    body,
    tokenize='porter unicode61'
);
```

b) Bump `_SCHEMA_VERSION = "5"`.

c) In `build()`, write the new columns. Join `applies_when` into a space-separated string for FTS and docs storage. Update both INSERTs:

```python
                applies_when_str = " ".join(doc.applies_when)
                conn.execute(
                    "INSERT INTO docs (id, path, tier, category, status, type, "
                    "applies_when, description, title, tags, content_markdown, "
                    "last_modified, last_modified_source, git_remote_url) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (doc_id, str(rel), final_tier, final_category, doc.status, doc.doc_type,
                     applies_when_str, doc.description, doc.title, tags_str, content_markdown,
                     last_modified, lm_source, doc.git_remote_url),
                )
                conn.execute(
                    "INSERT INTO fts (id, title, tags, applies_when, description, body) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (doc_id, doc.title, tags_str, applies_when_str, doc.description, doc.body),
                )
```

(The `docs` `CREATE TABLE` must include `applies_when TEXT` and `description TEXT` columns; place them after `status`/`type`.)

d) Fix the snippet column index in `search()`: body is now fts column index 5 (was 3). Change `snippet(fts, 3, ...)` to `snippet(fts, 5, ...)`.

- [ ] **Step 4: Run the suite**

Run: `uv run pytest tests/test_index.py -q`
Expected: PASS (new tests + schema-version test pass; existing search tests still find their docs).

- [ ] **Step 5: Commit**

```bash
git add src/data_olympus/index.py tests/test_index.py
git commit -m "feat(index): index applies_when and description columns; schema v5"
```

---

### Task 3: Column-weighted, ablatable `search()`

**Files:**
- Modify: `src/data_olympus/index.py`
- Test: `tests/test_index.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_index.py`:

```python
def _excel_governance_kb(tmp_path: Path) -> Path:
    kb = tmp_path / "kb"
    d = kb / "universal" / "foundation"
    d.mkdir(parents=True)
    (d / "STD-XL.md").write_text(
        "---\nid: STD-XL\ntier: T1\ntype: standard\nstatus: active\n"
        "applies_when: [openpyxl, insert_cols, excel]\n"
        "description: Prefer xlsxwriter for new Excel files.\n---\n"
        "# Excel standard\n\nGuidance about spreadsheets.\n"
    )
    (d / "STD-LOG.md").write_text(
        "---\nid: STD-LOG\ntier: T1\ntype: standard\nstatus: active\n"
        "applies_when: [logging, structured-logs]\n"
        "description: Use structured logging.\n---\n"
        "# Logging\n\nopenpyxl is mentioned once here in passing.\n"
    )
    return kb


def test_applies_when_match_outranks_incidental_body_match(
    tmp_path: Path, tmp_index_path: Path
) -> None:
    idx = Index(tmp_index_path)
    idx.build(_excel_governance_kb(tmp_path), source_commit="x")
    hits = idx.search("openpyxl", limit=5)
    assert hits[0].id == "STD-XL", (
        "a doc whose applies_when trigger matches must outrank a doc with only an "
        f"incidental body mention; got {[h.id for h in hits]}"
    )


def test_search_columns_ablation_restricts_match(tmp_path: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(_excel_governance_kb(tmp_path), source_commit="x")
    # Restrict matching to body only: the applies_when-only trigger 'insert_cols'
    # appears in no body, so nothing matches.
    body_only = idx.search("insert_cols", limit=5, columns=["body"])
    assert body_only == []
    # Default (all columns) retrieves via applies_when.
    full = idx.search("insert_cols", limit=5)
    assert any(h.id == "STD-XL" for h in full)


def test_search_rejects_unknown_column(tmp_path: Path, tmp_index_path: Path) -> None:
    import pytest
    idx = Index(tmp_index_path)
    idx.build(_excel_governance_kb(tmp_path), source_commit="x")
    with pytest.raises(ValueError, match="unknown fts column"):
        idx.search("excel", limit=5, columns=["title", "bogus"])
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_index.py::test_search_columns_ablation_restricts_match -q`
Expected: FAIL — `search()` has no `columns` parameter (`TypeError`).

- [ ] **Step 3: Implement**

In `src/data_olympus/index.py`, add module constants near the top:

```python
# fts column order (excluding the UNINDEXED id at position 0).
_FTS_MATCH_COLUMNS: tuple[str, ...] = ("title", "tags", "applies_when", "description", "body")
# bm25 weights, one per declared fts column INCLUDING the UNINDEXED id at index 0.
# Title and applies_when are weighted highest; body lowest. Tune in D2.
_DEFAULT_BM25_WEIGHTS: tuple[float, ...] = (0.0, 10.0, 5.0, 10.0, 4.0, 1.0)
```

Rewrite the head of `search()` to accept `columns`/`column_weights`, build a column-filtered match query, and use a weighted `bm25()`:

```python
    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        tier: str | None = None,
        category: str | None = None,
        status: str | None = None,
        doc_type: str | None = None,
        columns: list[str] | None = None,
        column_weights: tuple[float, ...] | None = None,
    ) -> list[SearchHit]:
        """FTS5 search with column-weighted bm25.

        `columns` restricts which fts columns are matched (for ablation); default
        matches all. `column_weights` overrides bm25 weights (one per declared
        fts column incl. the UNINDEXED id at index 0); default boosts
        title/applies_when, deprioritizes body.
        """
        terms = query.split()
        if not terms:
            return []
        quoted = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
        if columns:
            unknown = [c for c in columns if c not in _FTS_MATCH_COLUMNS]
            if unknown:
                raise ValueError(f"unknown fts column(s): {unknown}")
            match_query = "{" + " ".join(columns) + "} : (" + quoted + ")"
        else:
            match_query = quoted
        weights = column_weights if column_weights is not None else _DEFAULT_BM25_WEIGHTS
        bm25_expr = "bm25(fts, " + ", ".join(repr(float(w)) for w in weights) + ")"
        conn = self._connect()
        try:
            where = ["fts MATCH ?"]
            params: list[object] = [match_query]
            if tier:
                where.append("docs.tier = ?")
                params.append(tier)
            if category:
                where.append("docs.category = ?")
                params.append(category)
            if status:
                where.append("docs.status = ?")
                params.append(status)
            if doc_type:
                where.append("docs.type = ?")
                params.append(doc_type)
            params.append(limit)
            sql = f"""
                SELECT
                    fts.id AS id,
                    docs.path AS path,
                    COALESCE(docs.title, '') AS title,
                    COALESCE(docs.status, '') AS status,
                    COALESCE(docs.type, '') AS doc_type,
                    snippet(fts, 5, '[', ']', '...', 16) AS snippet,
                    {bm25_expr} AS score
                FROM fts
                JOIN docs ON docs.id = fts.id
                WHERE {' AND '.join(where)}
                ORDER BY score
                LIMIT ?
            """
            rows = conn.execute(sql, params).fetchall()
            return [
                SearchHit(
                    id=r["id"], path=r["path"], title=r["title"],
                    snippet=r["snippet"], score=float(r["score"]),
                    status=r["status"], doc_type=r["doc_type"],
                )
                for r in rows
            ]
        finally:
            conn.close()
```

Implementation notes for the engineer:
- `bm25_expr` interpolates only floats (`repr(float(w))`), never user strings, so it is injection-safe. `match_query` is a bound parameter.
- **Verify the bm25 weight arity against real SQLite.** FTS5 `bm25()` expects one weight per declared column. The table declares 6 columns (id UNINDEXED, title, tags, applies_when, description, body), so `_DEFAULT_BM25_WEIGHTS` has 6 entries. If SQLite rejects a weight for the UNINDEXED `id` column, drop the leading `0.0` and use 5 weights; adjust `_DEFAULT_BM25_WEIGHTS` and the `column_weights` contract accordingly, and note it in the docstring. The tests in Step 1 will surface this.

- [ ] **Step 4: Run the suite**

Run: `uv run pytest tests/test_index.py tests/test_tools_read.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/data_olympus/index.py tests/test_index.py
git commit -m "feat(index): column-weighted, ablatable search (applies_when/title boosted)"
```

---

### Task 4: Surface `applies_when` + `description` on `kb_get`

**Files:**
- Modify: `src/data_olympus/index.py` (`IndexedDoc` + `get()`), `src/data_olympus/models.py` (`GetResponse`), `src/data_olympus/tools_read.py` (`kb_get_fn`)
- Test: `tests/test_tools_read.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_tools_read.py`:

```python
def test_kb_get_fn_returns_applies_when_and_description(tmp_path, tmp_index_path):
    from data_olympus.index import Index
    from data_olympus.tools_read import kb_get_fn
    kb = tmp_path / "kb"
    (kb / "universal" / "foundation").mkdir(parents=True)
    (kb / "universal" / "foundation" / "STD-XL.md").write_text(
        "---\nid: STD-XL\ntier: T1\ntype: standard\nstatus: active\n"
        "applies_when: [openpyxl, excel]\ndescription: Prefer xlsxwriter.\n---\n# B\n"
    )
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    resp = kb_get_fn(idx=idx, id="STD-XL")
    assert resp.applies_when == ["openpyxl", "excel"]
    assert resp.description == "Prefer xlsxwriter."
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_tools_read.py::test_kb_get_fn_returns_applies_when_and_description -q`
Expected: FAIL — `GetResponse` has no `applies_when`.

- [ ] **Step 3: Implement**

- `index.py`: add `applies_when: list[str] = field(default_factory=list)` and `description: str = ""` to `IndexedDoc`. In `get()`, SELECT `applies_when, description` from docs and populate them (split `applies_when` on whitespace back to a list).
- `models.py`: add `applies_when: list[str] = []` and `description: str = ""` to `GetResponse`.
- `tools_read.py` `kb_get_fn`: map `applies_when=doc.applies_when, description=doc.description` into `GetResponse`.

- [ ] **Step 4: Run the suite**

Run: `uv run pytest tests/test_tools_read.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/data_olympus/index.py src/data_olympus/models.py src/data_olympus/tools_read.py tests/test_tools_read.py
git commit -m "feat(mcp): return applies_when and description from kb_get"
```

---

### Task 5: Positioning fix in README and comparison.md

**Files:**
- Modify: `README.md`, `docs/comparison.md`

- [ ] **Step 1: Sharpen the positioning**

In `README.md`, add one sentence to the opening that states data-olympus is a decision/instruction **governance** layer for agents (surfacing the established standard/decision that should govern a coding choice), and explicitly that it is **not** a code-search, reference-finding, or "where is X used" tool (LSP/grep/Sourcegraph own that). Do not overclaim; keep the existing description.

In `docs/comparison.md`, in the "What data-olympus is" section, add the same framing: the retrieval task is **coding-intent → governing rule**, and code search / reference finding is explicitly out of scope (a complementary concern handled by other tools).

- [ ] **Step 2: Add a CHANGELOG entry**

Add under the unreleased section of `CHANGELOG.md`:

```markdown
- Retrieval now indexes `applies_when` trigger metadata and `description` with
  column-weighted ranking, improving coding-intent to governing-rule matching
  (schema v5).
```

- [ ] **Step 3: Final gate**

Run: `uv run pytest -q && uv run ruff check . && uv run mypy src && uv run data-olympus lint example-bundle && uv run data-olympus lint benchmarks/corpus`
Expected: tests pass; ruff clean; mypy shows only the 3 pre-existing errors in `cli/main.py` and `format/frontmatter.py`; both bundles lint clean.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/comparison.md CHANGELOG.md
git commit -m "docs: position data-olympus as governance retrieval, not code search"
```

---

## Self-Review

- **Spec coverage (§4):** `applies_when` (Tasks 2-3), `description` (Tasks 1-2), column weighting (Task 3), parser upgrade (Task 1), positioning (Task 5), toggleable ablation params (Task 3 `columns`/`column_weights`). All map to a task.
- **Regression safety:** Task 1 reuses the tested yaml parser and preserves post-processing; the full suite gates it. The snippet column index moves 3→5 (Task 2) — a real gotcha, called out. The bm25 weight arity against the UNINDEXED column is flagged for empirical verification (Task 3).
- **Injection safety:** `columns` validated against a whitelist; `column_weights` interpolated as floats; the match query is a bound parameter.
- **No placeholders:** complete code for the parser, search(), and schema; precise specs + tests for the rest.
- **Ablation seam for D2:** `search(columns=..., column_weights=...)` lets D2 run FTS-body-only, +description, +applies_when, and reweighted configs without rebuilding the index.
