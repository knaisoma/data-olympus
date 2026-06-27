"""SQLite FTS5 index over the KB markdown files."""
from __future__ import annotations

import datetime
import os
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from data_olympus.markdown_parse import parse_file

if TYPE_CHECKING:
    import builtins
    from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    tier TEXT,
    category TEXT,
    status TEXT,
    type TEXT,
    applies_when TEXT,
    description TEXT,
    title TEXT,
    tags TEXT,
    content_markdown TEXT,
    last_modified TEXT,
    last_modified_source TEXT,
    git_remote_url TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    id UNINDEXED,
    title,
    tags,
    applies_when,
    description,
    body,
    tokenize='porter unicode61'
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# Informational only; recorded in the meta table for observability. Rebuild
# is guaranteed by bootstrap_now=True calling Index.build() unconditionally
# on container start, not by a version-mismatch check.
_SCHEMA_VERSION = "5"


# fts column order (excluding the UNINDEXED id at position 0).
_FTS_MATCH_COLUMNS: tuple[str, ...] = ("title", "tags", "applies_when", "description", "body")
# bm25 weights, one per declared fts column INCLUDING the UNINDEXED id at index 0.
# Title and applies_when are weighted highest; body lowest. Tune in D2.
_DEFAULT_BM25_WEIGHTS: tuple[float, ...] = (0.0, 10.0, 5.0, 10.0, 4.0, 1.0)

# Generic, deployment-neutral default taxonomy. A deployment with a different
# directory layout supplies its own table at runtime via KB_TAXONOMY_PATH (a
# JSON file holding a list of [prefix, tier, category] triples). The two
# prefixes "tech-stacks/" and "projects/" are classified dynamically (see
# _classify_by_path), so any stack or project name is covered without an
# enumerated allow-list.
# NOTE: bin/_kb_fallback.py mirrors this default and the same KB_TAXONOMY_PATH
# loader. If you change one, change the other.
_DEFAULT_PATH_RULES: tuple[tuple[str, str, str], ...] = (
    # T1 Universal, applies to every project, every stack.
    ("universal/foundation/",       "T1", "foundation"),
    ("universal/quality/",          "T1", "quality"),
    ("universal/security/",         "T1", "security"),
    ("universal/infrastructure/",   "T1", "infrastructure"),
    ("universal/database/",         "T1", "database"),
    ("universal/api/",              "T1", "api"),
    ("universal/services/",         "T1", "services"),

    # T2 Stack-specific, classified dynamically: tech-stacks/<stack>/...
    ("tech-stacks/",                 "T2", "stack"),

    # Meta tiers (kept distinct from T1-T4).
    ("decisions/",                   "decisions", "decisions"),
    ("workflows/",                   "workflows", "workflows"),
    ("memory/inbox/",                "memory",    "memory-inbox"),
    ("memory/accepted/",             "memory",    "memory-accepted"),
    ("memory/",                      "memory",    "memory"),
    ("tooling/",                     "tooling",   "tooling"),
    ("templates/",                   "templates", "templates"),

    # T3 / T4 catch-all (project tree). The classifier post-processes
    # this hit; see _classify_by_path for the T3 vs T4 distinction.
    ("projects/",                    "T3", "project"),
)


def _load_path_rules() -> tuple[tuple[str, str, str], ...]:
    """Return the active taxonomy: KB_TAXONOMY_PATH JSON if set, else default.

    The JSON must be a list of ``[prefix, tier, category]`` triples. A malformed
    file raises ValueError rather than silently misclassifying every document.
    """
    path = os.environ.get("KB_TAXONOMY_PATH", "").strip()
    if not path:
        return _DEFAULT_PATH_RULES
    import json
    from pathlib import Path as _Path
    data = json.loads(_Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(
        isinstance(r, (list, tuple)) and len(r) == 3 for r in data
    ):
        raise ValueError(
            f"KB_TAXONOMY_PATH={path!r} must be a JSON list of "
            f"[prefix, tier, category] triples"
        )
    return tuple((str(r[0]), str(r[1]), str(r[2])) for r in data)


_EXCLUDED_DIR_NAMES: frozenset[str] = frozenset({
    ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv",
    "__pycache__", "node_modules", ".worktrees", "to-delete",
    "archive", "_archive",
    # Exclude the data-olympus-mcp source itself so the indexer doesn't eat
    # its own .py files (only relevant when run against the live KB).
    "data-olympus-mcp",
    # Test fixtures contain duplicate filename-stem markdown (e.g. multiple
    # CLAUDE.md / AGENTS.md across good/ bad-fat/ bad-no-loader/ etc.).
    # These are test data, not KB content; indexing them produces hard
    # duplicate-id failures that bootstrap the container in a degraded state.
    "test-fixtures", "cli-fixtures",
})


def _is_excluded(rel_path: Path) -> bool:
    return any(part in _EXCLUDED_DIR_NAMES for part in rel_path.parts)


def _classify_by_path(
    rel_path: str,
    rules: tuple[tuple[str, str, str], ...] | None = None,
) -> tuple[str, str]:
    """Return (tier, category) inferred from the relative path.

    Returns ('meta', 'meta') if no rule matches. ``rules`` defaults to the
    active taxonomy (KB_TAXONOMY_PATH or the built-in default); the indexer
    loads it once and passes it in to avoid re-reading per document.
    Within projects/, distinguishes T4 component paths from T3 project paths
    by looking for the literal 'components' segment after the project name.
    tech-stacks/<stack>/... is classified dynamically as stack:<stack>.
    """
    if rules is None:
        rules = _load_path_rules()
    norm = rel_path.replace("\\", "/")
    for prefix, tier, category in rules:
        if norm.startswith(prefix):
            if prefix == "projects/":
                parts = norm.split("/")
                # projects/<name>/components/<component>/<file>... -> T4
                # Requires len >= 5 so parts[3] is a real component DIRECTORY
                # (not a loose file directly inside components/).
                if len(parts) >= 5 and parts[2] == "components":
                    return "T4", f"component:{parts[1]}/{parts[3]}"
                # projects/<name>/... (incl. components/ with no component yet) -> T3
                if len(parts) >= 2:
                    # Strip .md so projects/index.md -> project:index
                    # (rather than project:index.md). For real project dirs
                    # like projects/example-project/ this is a no-op.
                    name = parts[1].removesuffix(".md")
                    return "T3", f"project:{name}"
            if prefix == "tech-stacks/":
                parts = norm.split("/")
                # tech-stacks/<stack>/<file>... -> stack:<stack>
                if len(parts) >= 2 and parts[1]:
                    return tier, f"{category}:{parts[1].removesuffix('.md')}"
            return tier, category
    return "meta", "meta"


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


@dataclass(frozen=True, slots=True)
class IndexBuildResult:
    """Outcome of an index build."""

    docs_indexed: int
    source_commit: str
    built_at: float  # epoch seconds


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
    applies_when: list[str] = field(default_factory=list)
    description: str = ""


class DuplicateIdError(ValueError):
    """Two or more files resolve to the same indexed id."""

    def __init__(self, conflicts: dict[str, list[str]]) -> None:
        self.conflicts = conflicts
        msg = "duplicate ids detected: " + ", ".join(
            f"{id_} -> {paths}" for id_, paths in conflicts.items()
        )
        super().__init__(msg)


def _derive_id_from_path(rel: Path) -> str:
    """Derive a unique id from a relative path when front matter has no `id`.

    Joins the path parts (without the .md extension) with `-` so:
      AGENTS.md                                 -> AGENTS
      tooling/AGENTS.md                         -> tooling-AGENTS
      decisions/index.md                        -> decisions-index
      projects/example-project/coding-agents-preferences/README.md
        -> projects-example-project-coding-agents-preferences-README

    Front-matter `id:` always wins; this is only the fallback.
    """
    parts = list(rel.with_suffix("").parts)
    return "-".join(parts) if parts else rel.stem


class Index:
    """SQLite FTS5 index. Single-writer; safe for concurrent reads."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _git_last_modified(kb_root: Path, rel: Path) -> tuple[str, str]:
        """Return (iso_timestamp, source). source is 'git' or 'mtime'."""
        try:
            result = subprocess.run(
                ["git", "-C", str(kb_root), "log", "-1", "--format=%cI", "--", str(rel)],
                capture_output=True, text=True, check=False, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip(), "git"
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        # Fallback: filesystem mtime
        mtime = (kb_root / rel).stat().st_mtime
        return datetime.datetime.fromtimestamp(mtime, tz=datetime.UTC).isoformat(), "mtime"

    def build(self, kb_root: Path, *, source_commit: str) -> IndexBuildResult:
        """Walk kb_root for .md files and (re)build the FTS index using atomic swap."""
        if not kb_root.is_dir():
            raise NotADirectoryError(f"KB root not a directory: {kb_root}")

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._db_path.with_name(
            f"{self._db_path.name}.tmp.{os.getpid()}.{source_commit}.db"
        )
        if tmp_path.exists():
            tmp_path.unlink()  # stale tmp from previous failed build

        # First pass: collect (id, path) without writing, so we can detect duplicates
        # before mutating anything.
        seen: dict[str, list[str]] = {}
        files_to_index: list[Path] = []
        for md in sorted(kb_root.rglob("*.md")):
            rel = md.relative_to(kb_root)
            if _is_excluded(rel):
                continue
            doc = parse_file(md)
            doc_id = doc.id or _derive_id_from_path(rel)
            seen.setdefault(doc_id, []).append(str(rel))
            files_to_index.append(md)
        conflicts = {id_: paths for id_, paths in seen.items() if len(paths) > 1}
        if conflicts:
            raise DuplicateIdError(conflicts)

        # Build the new index into the tmp file
        conn = sqlite3.connect(tmp_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(_SCHEMA)
            count = 0
            path_rules = _load_path_rules()
            for md in files_to_index:
                rel = md.relative_to(kb_root)
                doc = parse_file(md)
                doc_id = doc.id or _derive_id_from_path(rel)
                path_tier, path_category = _classify_by_path(str(rel), path_rules)
                final_tier = doc.tier or path_tier
                final_category = doc.category or path_category
                tags_str = " ".join(doc.tags)
                applies_when_str = " ".join(doc.applies_when)
                last_modified, lm_source = self._git_last_modified(kb_root, rel)
                content_markdown = md.read_text(encoding="utf-8")
                conn.execute(
                    "INSERT INTO docs (id, path, tier, category, status, type, "
                    "applies_when, description, title, tags, "
                    "content_markdown, last_modified, last_modified_source, git_remote_url) "
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
                count += 1
            now = time.time()
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('source_commit', ?)",
                (source_commit,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('built_at', ?)",
                (str(now),),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('total_docs', ?)",
                (str(count),),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (_SCHEMA_VERSION,),
            )
            # Integrity check before swap
            check = conn.execute("PRAGMA integrity_check").fetchone()
            if check[0] != "ok":
                raise sqlite3.IntegrityError(f"new index failed integrity_check: {check}")
            smoke = conn.execute("SELECT COUNT(*) FROM docs").fetchone()
            if smoke[0] != count:
                raise sqlite3.IntegrityError(
                    f"smoke check failed: docs count {smoke[0]} != built count {count}"
                )
            conn.commit()
        except Exception:
            conn.close()
            if tmp_path.exists():
                tmp_path.unlink()
            raise
        finally:
            conn.close()

        # Atomic swap: the previous index file (if any) is replaced. Open fds on the
        # old inode keep reading from it until they close.
        os.replace(tmp_path, self._db_path)
        return IndexBuildResult(docs_indexed=count, source_commit=source_commit, built_at=now)

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

        Quote each whitespace term individually and OR them together. Quoting
        per-term keeps FTS5 from treating dashes/dots/colons inside a term
        (e.g. "STD-U-002") as operators, while OR lets multi-word natural-
        language queries match on any term (ranked by bm25) instead of
        requiring a verbatim phrase. Doubling embedded quotes prevents a term
        from breaking out of its phrase, so user input cannot inject FTS
        operators.

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

    def get(self, id: str) -> IndexedDoc | None:
        """Retrieve a single document by id. Returns None if not found."""
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT id, path, title, tier, category, status, type, tags,
                       applies_when, description, content_markdown,
                       last_modified, last_modified_source, git_remote_url
                FROM docs WHERE id = ?
                """,
                (id,),
            ).fetchone()
            if row is None:
                return None
            commit_row = conn.execute(
                "SELECT value FROM meta WHERE key='source_commit'"
            ).fetchone()
            source_commit = commit_row[0] if commit_row else ""
        finally:
            conn.close()
        tags = [t for t in (row["tags"] or "").split() if t]
        applies_when = [t for t in (row["applies_when"] or "").split() if t]
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
            applies_when=applies_when,
            description=row["description"] or "",
        )

    def list(self, *, tier: str, category: str | None = None) -> list[dict[str, str]]:
        """List docs in the given tier (and optional category), ordered by id ascending."""
        conn = self._connect()
        try:
            if category is None:
                rows = conn.execute(
                    "SELECT id, COALESCE(title, '') AS title, path FROM docs "
                    "WHERE tier = ? ORDER BY id ASC",
                    (tier,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, COALESCE(title, '') AS title, path FROM docs "
                    "WHERE tier = ? AND category = ? ORDER BY id ASC",
                    (tier, category),
                ).fetchall()
        finally:
            conn.close()
        return [{"id": r["id"], "title": r["title"], "path": r["path"]} for r in rows]

    def outline(self) -> builtins.list[dict[str, object]]:
        """Return list of {name, categories: [{name, count}]} for tiers present."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT tier, category, COUNT(*) AS n "
                "FROM docs GROUP BY tier, category ORDER BY tier, category"
            ).fetchall()
        finally:
            conn.close()

        tiers: dict[str, builtins.list[dict[str, object]]] = {}
        for r in rows:
            tier = r["tier"] or "(untiered)"
            category = r["category"] or "(uncategorized)"
            tiers.setdefault(tier, []).append({"name": category, "count": r["n"]})
        return [{"name": k, "categories": v} for k, v in tiers.items()]

    def health(self) -> dict[str, object]:
        """Return commit, built_at, total_docs, db_size_bytes."""
        if not self._db_path.exists():
            return {
                "source_commit": "",
                "index_built_at": None,
                "total_docs": 0,
                "db_size_bytes": 0,
            }
        conn = self._connect()
        try:
            rows = conn.execute("SELECT key, value FROM meta").fetchall()
        finally:
            conn.close()
        meta = {r["key"]: r["value"] for r in rows}
        return {
            "source_commit": meta.get("source_commit", ""),
            "index_built_at": float(meta["built_at"]) if "built_at" in meta else None,
            "total_docs": int(meta.get("total_docs", "0")),
            "db_size_bytes": self._db_path.stat().st_size,
        }

    def list_by_prefix(
        self, prefix: str, *, exclude_under: str | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """Return docs whose path starts with prefix; optionally exclude
        children under prefix + exclude_under."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, path, tier, git_remote_url FROM docs WHERE path LIKE ?",
                (f"{prefix}%",),
            ).fetchall()
        finally:
            conn.close()
        out: list[dict[str, Any]] = []
        for r in rows:
            path = r["path"]
            if exclude_under is not None:
                rest = path[len(prefix):]
                if rest.startswith(exclude_under):
                    continue
            out.append({
                "id": r["id"], "path": path, "tier": r["tier"] or "",
                "git_remote_url": r["git_remote_url"],
            })
        return out

    def list_with_remote_url(self) -> builtins.list[dict[str, Any]]:
        """Return all docs that have a non-null git_remote_url."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, path, tier, git_remote_url FROM docs "
                "WHERE git_remote_url IS NOT NULL AND git_remote_url != ''"
            ).fetchall()
        finally:
            conn.close()
        return [{
            "id": r["id"], "path": r["path"], "tier": r["tier"] or "",
            "git_remote_url": r["git_remote_url"],
        } for r in rows]
