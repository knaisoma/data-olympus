# Status/Type Retrieval Filtering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the documented "filter by status / tier / type" capability real, so retrieval can return only the current (`status: active`) concept and filter by concept `type`.

**Architecture:** Thread two frontmatter fields (`status`, `type`) through the existing pipeline: parser → index schema → build → search filter → get → MCP tool → response models → server wrapper. No new modules; every change extends an existing function. The `type` field is carried in Python as `doc_type` to avoid shadowing the builtin, and stored in a SQL column named `type`.

**Tech Stack:** Python 3.13, SQLite FTS5, Pydantic v2, pytest. Lint: ruff. Types: mypy strict.

**Part of:** [docs/specs/2026-06-25-retrieval-benchmark-design.md](../specs/2026-06-25-retrieval-benchmark-design.md) §4a (Part A, prerequisite to the benchmark).

---

## File Structure

- `src/data_olympus/markdown_parse.py` — add `status`, `doc_type` to `ParsedDoc` + populate in `parse_file`.
- `src/data_olympus/index.py` — schema columns, `build()` insert, `search()` filters, `SearchHit`/`IndexedDoc` fields, `get()` select, `_SCHEMA_VERSION`.
- `src/data_olympus/tools_read.py` — `kb_search_fn`/`kb_get_fn` pass-through and response mapping.
- `src/data_olympus/models.py` — `SearchHitModel`, `GetResponse` new fields.
- `src/data_olympus/server.py` — `kb_search` MCP wrapper params.
- `tests/conftest.py` — `status_kb` fixture (concepts with status/type, incl. a supersession pair).
- `tests/test_markdown_parse.py`, `tests/test_index.py`, `tests/test_tools_read.py` — new tests; one existing assertion updated.

Run the whole suite at any point with: `uv run pytest -q`

---

### Task 1: Parser extracts `status` and `type`

**Files:**
- Modify: `src/data_olympus/markdown_parse.py`
- Test: `tests/test_markdown_parse.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_markdown_parse.py`:

```python
def test_parse_file_extracts_status_and_type(tmp_path):
    from data_olympus.markdown_parse import parse_file
    p = tmp_path / "x.md"
    p.write_text(
        "---\nid: STD-1\ntier: T1\ntype: standard\nstatus: active\n---\n# Body\n"
    )
    doc = parse_file(p)
    assert doc.status == "active"
    assert doc.doc_type == "standard"


def test_parse_file_status_and_type_default_empty(tmp_path):
    from data_olympus.markdown_parse import parse_file
    p = tmp_path / "y.md"
    p.write_text("# No front matter\n")
    doc = parse_file(p)
    assert doc.status == ""
    assert doc.doc_type == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_markdown_parse.py::test_parse_file_extracts_status_and_type -q`
Expected: FAIL — `AttributeError: 'ParsedDoc' object has no attribute 'status'`

- [ ] **Step 3: Write minimal implementation**

In `src/data_olympus/markdown_parse.py`, add two trailing fields to `ParsedDoc` (after `git_remote_url`, all defaulted so positional construction stays valid):

```python
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
```

In `parse_file`, populate them in the returned `ParsedDoc` (mirror the lenient `str(fm.get(...))` style already used for `tier`):

```python
    return ParsedDoc(
        path=path,
        id=id_value,
        tier=str(fm.get("tier", "")),
        category=str(fm.get("category", "")),
        tags=[str(t) for t in tags],
        title=str(fm.get("title", "")),
        body=body,
        git_remote_url=git_remote_url,
        status=str(fm.get("status", "")),
        doc_type=str(fm.get("type", "")),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_markdown_parse.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/data_olympus/markdown_parse.py tests/test_markdown_parse.py
git commit -m "feat(index): parse status and type frontmatter into ParsedDoc"
```

---

### Task 2: Index schema columns + build populates them + schema version bump

**Files:**
- Modify: `src/data_olympus/index.py`
- Test: `tests/test_index.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_index.py`:

```python
def test_docs_table_has_status_and_type_columns(tmp_kb: Path, tmp_index_path: Path) -> None:
    import sqlite3
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    conn = sqlite3.connect(tmp_index_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(docs)").fetchall()}
    conn.close()
    assert {"status", "type"} <= cols, f"missing status/type columns; got {cols}"


def test_build_populates_status_and_type(tmp_path: Path, tmp_index_path: Path) -> None:
    import sqlite3
    kb = tmp_path / "kb"
    (kb / "universal" / "foundation").mkdir(parents=True)
    (kb / "universal" / "foundation" / "STD-S.md").write_text(
        "---\nid: STD-S\ntier: T1\ntype: standard\nstatus: active\n---\n# Body about caching\n"
    )
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    conn = sqlite3.connect(tmp_index_path)
    row = conn.execute("SELECT status, type FROM docs WHERE id='STD-S'").fetchone()
    conn.close()
    assert row == ("active", "standard"), f"status/type not populated; got {row}"
```

Update the existing `test_index_records_schema_version` assertion in the same file from `"3"` to `"4"`:

```python
    assert row[0] == "4", f"schema_version must be '4' after status/type columns; got {row[0]!r}"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_index.py::test_docs_table_has_status_and_type_columns tests/test_index.py::test_build_populates_status_and_type -q`
Expected: FAIL — `sqlite3.OperationalError: no such column: status`

- [ ] **Step 3: Write minimal implementation**

In `src/data_olympus/index.py`, add the two columns to the `docs` table in `_SCHEMA` (between `category` and `title`):

```python
CREATE TABLE IF NOT EXISTS docs (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    tier TEXT,
    category TEXT,
    status TEXT,
    type TEXT,
    title TEXT,
    tags TEXT,
    content_markdown TEXT,
    last_modified TEXT,
    last_modified_source TEXT,
    git_remote_url TEXT
);
```

Bump the version constant:

```python
_SCHEMA_VERSION = "4"
```

In `build()`, change the `docs` INSERT to include the two columns and values (`doc.status`, `doc.doc_type`):

```python
                conn.execute(
                    "INSERT INTO docs (id, path, tier, category, status, type, title, tags, "
                    "content_markdown, last_modified, last_modified_source, git_remote_url) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (doc_id, str(rel), final_tier, final_category, doc.status, doc.doc_type,
                     doc.title, tags_str, content_markdown, last_modified, lm_source,
                     doc.git_remote_url),
                )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_index.py -q`
Expected: PASS (including the updated schema-version test)

- [ ] **Step 5: Commit**

```bash
git add src/data_olympus/index.py tests/test_index.py
git commit -m "feat(index): store status and type columns; bump schema_version to 4"
```

---

### Task 3: `search()` filters by `status` and `doc_type`

**Files:**
- Modify: `src/data_olympus/index.py`
- Modify: `tests/conftest.py` (add `status_kb` fixture)
- Test: `tests/test_index.py`

- [ ] **Step 1: Add the fixture and write the failing tests**

Add to `tests/conftest.py`:

```python
@pytest.fixture
def status_kb(tmp_path: Path) -> Path:
    """A KB with status/type frontmatter and a supersession pair, all matching
    the word 'caching', so status/type filters can be exercised."""
    kb = tmp_path / "status-kb"
    d = kb / "universal" / "foundation"
    d.mkdir(parents=True)
    (d / "STD-OLD.md").write_text(
        "---\nid: STD-OLD\ntier: T1\ntype: standard\nstatus: superseded\n"
        "superseded_by: STD-NEW\n---\n# Caching rule\n\nOld caching guidance.\n"
    )
    (d / "STD-NEW.md").write_text(
        "---\nid: STD-NEW\ntier: T1\ntype: standard\nstatus: active\n"
        "supersedes: STD-OLD\n---\n# Caching rule\n\nCurrent caching guidance.\n"
    )
    decisions = kb / "decisions"
    decisions.mkdir(parents=True)
    (decisions / "DEC-1.md").write_text(
        "---\nid: DEC-1\ntier: decisions\ntype: decision\nstatus: accepted\n"
        "---\n# Caching decision\n\nWe chose caching.\n"
    )
    return kb
```

Add to `tests/test_index.py`:

```python
def test_search_filters_by_status(status_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(status_kb, source_commit="x")
    hits = idx.search("caching", limit=10, status="active")
    assert {h.id for h in hits} == {"STD-NEW"}, (
        f"status=active must exclude superseded/accepted; got {[h.id for h in hits]}"
    )


def test_search_filters_by_type(status_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(status_kb, source_commit="x")
    hits = idx.search("caching", limit=10, doc_type="decision")
    assert {h.id for h in hits} == {"DEC-1"}


def test_search_hit_carries_status_and_type(status_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(status_kb, source_commit="x")
    hits = idx.search("caching", limit=10, doc_type="decision")
    assert hits[0].status == "accepted"
    assert hits[0].doc_type == "decision"


def test_search_no_status_filter_returns_all_matches(status_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(status_kb, source_commit="x")
    hits = idx.search("caching", limit=10)
    assert {h.id for h in hits} == {"STD-OLD", "STD-NEW", "DEC-1"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_index.py::test_search_filters_by_status -q`
Expected: FAIL — `TypeError: search() got an unexpected keyword argument 'status'`

- [ ] **Step 3: Write minimal implementation**

In `src/data_olympus/index.py`, add `status` and `doc_type` fields to `SearchHit` (trailing, defaulted):

```python
@dataclass(frozen=True, slots=True)
class SearchHit:
    """One FTS hit."""

    id: str
    path: str
    title: str
    snippet: str
    score: float
    status: str = ""
    doc_type: str = ""
```

Replace `search()` with the filter-aware version:

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
    ) -> list[SearchHit]:
        """FTS5 search across title, tags, body. Optional tier/category/status/type filters."""
        safe_query = '"' + query.replace('"', '""') + '"'
        conn = self._connect()
        try:
            where = ["fts MATCH ?"]
            params: list[object] = [safe_query]
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
                    snippet(fts, 3, '[', ']', '...', 16) AS snippet,
                    bm25(fts) AS score
                FROM fts
                JOIN docs ON docs.id = fts.id
                WHERE {' AND '.join(where)}
                ORDER BY score
                LIMIT ?
            """
            rows = conn.execute(sql, params).fetchall()
            return [
                SearchHit(
                    id=r["id"],
                    path=r["path"],
                    title=r["title"],
                    snippet=r["snippet"],
                    score=float(r["score"]),
                    status=r["status"],
                    doc_type=r["doc_type"],
                )
                for r in rows
            ]
        finally:
            conn.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_index.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/data_olympus/index.py tests/conftest.py tests/test_index.py
git commit -m "feat(index): filter search by status and type"
```

---

### Task 4: `get()` returns `status` and `type`

**Files:**
- Modify: `src/data_olympus/index.py`
- Test: `tests/test_index.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_index.py`:

```python
def test_get_returns_status_and_type(status_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(status_kb, source_commit="x")
    doc = idx.get("STD-NEW")
    assert doc is not None
    assert doc.status == "active"
    assert doc.doc_type == "standard"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_index.py::test_get_returns_status_and_type -q`
Expected: FAIL — `AttributeError: 'IndexedDoc' object has no attribute 'status'`

- [ ] **Step 3: Write minimal implementation**

In `src/data_olympus/index.py`, add `status` and `doc_type` to `IndexedDoc` (trailing, defaulted):

```python
@dataclass(frozen=True, slots=True)
class IndexedDoc:
    """A document served from the indexed snapshot (kb_get response)."""

    id: str
    path: str
    title: str
    tier: str
    category: str
    tags: list[str]
    content_markdown: str
    last_modified: str
    last_modified_source: str
    source_commit: str
    git_remote_url: str | None = None
    status: str = ""
    doc_type: str = ""
```

In `get()`, add `status, type` to the SELECT and to the returned `IndexedDoc`:

```python
            row = conn.execute(
                """
                SELECT id, path, title, tier, category, status, type, tags, content_markdown,
                       last_modified, last_modified_source, git_remote_url
                FROM docs WHERE id = ?
                """,
                (id,),
            ).fetchone()
```

```python
        return IndexedDoc(
            id=row["id"],
            path=row["path"],
            title=row["title"] or "",
            tier=row["tier"] or "",
            category=row["category"] or "",
            tags=tags,
            content_markdown=row["content_markdown"] or "",
            last_modified=row["last_modified"] or "",
            last_modified_source=row["last_modified_source"] or "",
            source_commit=source_commit,
            git_remote_url=row["git_remote_url"],
            status=row["status"] or "",
            doc_type=row["type"] or "",
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_index.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/data_olympus/index.py tests/test_index.py
git commit -m "feat(index): return status and type from get()"
```

---

### Task 5: MCP layer exposes status/type (tools + models + server wrapper)

**Files:**
- Modify: `src/data_olympus/models.py`
- Modify: `src/data_olympus/tools_read.py`
- Modify: `src/data_olympus/server.py`
- Test: `tests/test_tools_read.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_tools_read.py`:

```python
def test_kb_search_fn_filters_by_status(status_kb, tmp_index_path):
    from data_olympus.index import Index
    from data_olympus.tools_read import kb_search_fn
    idx = Index(tmp_index_path)
    idx.build(status_kb, source_commit="x")
    resp = kb_search_fn(idx=idx, query="caching", status="active")
    assert {h.id for h in resp.hits} == {"STD-NEW"}
    assert resp.hits[0].status == "active"
    assert resp.hits[0].type == "standard"


def test_kb_get_fn_returns_status_and_type(status_kb, tmp_index_path):
    from data_olympus.index import Index
    from data_olympus.tools_read import kb_get_fn
    idx = Index(tmp_index_path)
    idx.build(status_kb, source_commit="x")
    resp = kb_get_fn(idx=idx, id="DEC-1")
    assert resp.status == "accepted"
    assert resp.type == "decision"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tools_read.py::test_kb_search_fn_filters_by_status -q`
Expected: FAIL — `TypeError: kb_search_fn() got an unexpected keyword argument 'status'`

- [ ] **Step 3: Write minimal implementation**

In `src/data_olympus/models.py`, add fields to `SearchHitModel`:

```python
class SearchHitModel(BaseModel):
    """One search hit."""

    id: str
    path: str
    title: str
    snippet: str
    score: float
    status: str = ""
    type: str = ""
```

And to `GetResponse` (after `category`):

```python
class GetResponse(BaseModel):
    """kb_get response."""

    id: str
    path: str
    title: str
    tier: str
    category: str
    status: str = ""
    type: str = ""
    tags: list[str]
    content_markdown: str
    last_modified: str
    last_modified_source: str
    source_commit: str
    git_remote_url: str | None = None
```

In `src/data_olympus/tools_read.py`, extend `kb_search_fn`:

```python
def kb_search_fn(
    *,
    idx: Index,
    query: str,
    limit: int = 20,
    tier: str | None = None,
    category: str | None = None,
    status: str | None = None,
    doc_type: str | None = None,
) -> SearchResponse:
    if limit > 100:
        limit = 100
    hits = idx.search(
        query, limit=limit, tier=tier, category=category, status=status, doc_type=doc_type
    )
    health = idx.health()
    return SearchResponse(
        query=query,
        hits=[
            SearchHitModel(
                id=h.id,
                path=h.path,
                title=h.title,
                snippet=h.snippet,
                score=h.score,
                status=h.status,
                type=h.doc_type,
            )
            for h in hits
        ],
        source_commit=str(health["source_commit"]),
        total_returned=len(hits),
    )
```

And map the new fields in `kb_get_fn` (add `status=doc.status, type=doc.doc_type` to the `GetResponse(...)` call):

```python
    return GetResponse(
        id=doc.id,
        path=doc.path,
        title=doc.title,
        tier=doc.tier,
        category=doc.category,
        status=doc.status,
        type=doc.doc_type,
        tags=list(doc.tags),
        content_markdown=doc.content_markdown,
        last_modified=doc.last_modified,
        last_modified_source=doc.last_modified_source,
        source_commit=doc.source_commit,
        git_remote_url=doc.git_remote_url,
    )
```

In `src/data_olympus/server.py`, extend the `kb_search` MCP wrapper so the filters reach agents over MCP:

```python
    @app.tool()
    def kb_search(
        query: str,
        limit: int = 20,
        tier: str | None = None,
        category: str | None = None,
        status: str | None = None,
        doc_type: str | None = None,
    ) -> dict[str, object]:
        """Full-text search across the KB.

        Optional tier/category/status/type filters (status e.g. 'active',
        doc_type e.g. 'decision'). Returns ranked hits with snippets.
        """
        resp = kb_search_fn(
            idx=state.idx, query=query, limit=limit, tier=tier, category=category,
            status=status, doc_type=doc_type,
        )
        return resp.model_dump()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_tools_read.py tests/test_server_smoke.py -q`
Expected: PASS (the thin server wrapper is a pass-through covered by the `kb_search_fn` tests; the smoke test confirms registration still imports cleanly)

- [ ] **Step 5: Commit**

```bash
git add src/data_olympus/models.py src/data_olympus/tools_read.py src/data_olympus/server.py tests/test_tools_read.py
git commit -m "feat(mcp): expose status and type on kb_search and kb_get"
```

---

### Task 6: Make the docs claim true and recorded

**Files:**
- Modify: `CHANGELOG.md`
- Verify (no change needed unless wording drifts): `README.md:13`, `docs/comparison.md:15`

- [ ] **Step 1: Confirm the existing claims are now satisfied**

Run: `grep -n "status" README.md docs/comparison.md`
Expected: the existing "filter by status / tier / type" lines are present. They are now backed by code, so no edit is required beyond recording the change.

- [ ] **Step 2: Add a CHANGELOG entry**

Add under the top/unreleased section of `CHANGELOG.md` (match the file's existing bullet style):

```markdown
- `kb_search` and `kb_get` now support and return `status` and `type`, making the
  documented "filter by status / tier / type" capability real (index schema v4).
```

- [ ] **Step 3: Run the full suite + lint + types as a final gate**

Run: `uv run pytest -q && uv run ruff check src tests && uv run mypy src`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: record status/type filtering in CHANGELOG"
```

---

## Self-Review

- **Spec coverage (§4a):** parser (Task 1), schema columns + build (Task 2), `search()` filter (Task 3), `get()` (Task 4), `kb_search`/responses (Task 5), schema-version test fix (Task 2), docs (Task 6). All §4a bullets map to a task.
- **Type consistency:** `doc_type` is the Python attribute/param name across `ParsedDoc`, `SearchHit`, `IndexedDoc`, `Index.search`, `kb_search_fn`, and the `kb_search` wrapper; the SQL column and the Pydantic response field are named `type`. The mapping (`type=h.doc_type`, `type=doc.doc_type`) is applied consistently in Task 5.
- **No placeholders:** every step has concrete code and an exact command with expected output.
- **Existing-test impact:** only `test_index_records_schema_version` changes (`"3"` → `"4"`), handled in Task 2. New columns add no rows, so doc-count assertions are unaffected.
