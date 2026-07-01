"""SQLite FTS5 index over the KB markdown files."""
from __future__ import annotations

import datetime
import os
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from data_olympus.cooccurrence import (
    DEFAULT_K,
    DEFAULT_MAX_TERMS,
    RELATED_TERMS_SCHEMA,
    build_cooccurrence_table,
    cooccurrence_build_params,
    cooccurrence_enabled,
    lookup_related_terms,
    make_cooccurrence_expander,
    tokenize_doc,
    write_cooccurrence_table,
)
from data_olympus.embeddings import (
    EMBEDDINGS_SCHEMA,
    Embedder,
    deserialize_vector,
    embeddings_config,
    embeddings_enabled,
    make_hybrid_reranker,
    serialize_vector,
)
from data_olympus.markdown_parse import parse_file
from data_olympus.trigram import (
    DEFAULT_FALLBACK_THRESHOLD as DEFAULT_TRIGRAM_FALLBACK_THRESHOLD,
)
from data_olympus.trigram import (
    TRIGRAM_FTS_SCHEMA,
    build_trigram_match_expr,
)

if TYPE_CHECKING:
    import builtins
    from collections.abc import Callable
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
""" + RELATED_TERMS_SCHEMA + TRIGRAM_FTS_SCHEMA + EMBEDDINGS_SCHEMA

# Informational only; recorded in the meta table for observability. Rebuild
# is guaranteed by bootstrap_now=True calling Index.build() unconditionally
# on container start, not by a version-mismatch check.
# v6 adds the related_terms co-occurrence table (issue #40).
# v7 adds the fts_trigram fuzzy-match table (issue #41).
# v8 adds the doc_vectors table (issue #42, populated only when embeddings on).
_SCHEMA_VERSION = "8"


# fts column order (excluding the UNINDEXED id at position 0).
_FTS_MATCH_COLUMNS: tuple[str, ...] = ("title", "tags", "applies_when", "description", "body")
# bm25 weights, one per declared fts column INCLUDING the UNINDEXED id at index 0.
# Title and applies_when are weighted highest; body lowest. Tune in D2.
_DEFAULT_BM25_WEIGHTS: tuple[float, ...] = (0.0, 10.0, 5.0, 10.0, 4.0, 1.0)

# When a reranker is installed, fetch a wider BM25 candidate pool than the caller
# asked for so the reranker can promote a relevant doc sitting just outside the
# top-N window, then truncate back to the requested limit. Bounded so a small
# limit still gets a useful pool and a large one cannot drag in the whole corpus.
_RERANK_OVERFETCH_FACTOR = 5
_RERANK_MIN_POOL = 50
_RERANK_MAX_POOL = 200

# Cap on the characters embedded per doc at build time (issue #42). The head of
# a doc carries its topical signal; bounding it keeps a very long doc from
# overrunning the model's context window and keeps build cost predictable.
_EMBED_TEXT_MAX_CHARS = 4000

# Status priors for the status-aware reranker (issue #37). search() orders by
# bm25 ASCENDING (lower = better), so these deltas are ADDED to the raw score:
# a NEGATIVE delta boosts (moves the hit earlier), a POSITIVE delta penalizes.
# In-force statuses boost; retired/not-yet-in-force statuses penalize; anything
# not in this map (incl. the empty string) is neutral and is never dropped. A
# deployment overrides the whole map via KB_STATUS_WEIGHTS (see config.py).
#
# Keys are matched case-insensitively against the document's status (both are
# casefolded before lookup), so mixed-case frontmatter (e.g. ``Active``) is not
# silently treated as neutral. Keep the keys here lowercase.
_DEFAULT_STATUS_WEIGHTS: dict[str, float] = {
    # In-force: the guidance that currently applies. Boost. ``approved`` is the
    # in-force status the target KB uses for accepted decisions, alongside the
    # spec's ``accepted``; both carry the same boost.
    "active": -1.0,
    "accepted": -1.0,
    "approved": -1.0,
    # Retired or superseded: kept for history, must not outrank its replacement.
    "superseded": 2.0,
    "deprecated": 2.0,
    "rejected": 2.0,
    # Not yet in force: a draft should not beat an active doc.
    "draft": 1.0,
    "proposed": 1.0,
}

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


def make_status_reranker(
    weights: dict[str, float] | None = None,
) -> Callable[[str, list[SearchHit]], list[SearchHit]]:
    """Build a reranker that nudges each hit's score by its ``status`` (issue #37).

    Governance intent: a ``superseded``/``deprecated`` doc must not outrank the
    ``active`` one that replaced it. The returned callable matches the ``reranker``
    hook signature ``(query, hits) -> hits``.

    ``score`` is a bm25 value ordered ASCENDING in search() (lower = better), so
    the status delta is ADDED to the raw score: an in-force status carries a
    NEGATIVE delta (boost, moves earlier) and a retired one a POSITIVE delta
    (penalize, moves later). A status not present in ``weights`` (including the
    empty string) is neutral (delta 0.0) and is never dropped. ``weights``
    defaults to ``_DEFAULT_STATUS_WEIGHTS``; a caller-supplied map REPLACES it
    rather than merging, so an override is fully explicit.

    Status matching is case-insensitive: both the configured keys and each hit's
    status are casefolded before lookup, so mixed-case frontmatter (e.g.
    ``Active``) is boosted like ``active`` rather than silently treated as
    neutral.

    The re-sort is stable: hits with equal adjusted score keep their incoming
    (bm25) order.
    """
    source = _DEFAULT_STATUS_WEIGHTS if weights is None else weights
    # Casefold the keys once so per-hit lookup is a plain dict.get on the
    # casefolded status. A caller-supplied map with two keys that collide under
    # casefold (e.g. "Draft" and "draft") keeps the last one, matching normal
    # dict semantics; deployments should supply distinct statuses.
    table = {status.casefold(): weight for status, weight in source.items()}

    def reranker(query: str, hits: list[SearchHit]) -> list[SearchHit]:  # noqa: ARG001
        adjusted = [
            replace(h, score=h.score + table.get(h.status.casefold(), 0.0))
            for h in hits
        ]
        adjusted.sort(key=lambda h: h.score)
        return adjusted

    return reranker


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

    def __init__(
        self,
        db_path: Path,
        *,
        clock: Callable[[], float] = time.monotonic,
        health_ttl_sec: float = 5.0,
        query_expander: Callable[[list[str]], list[str]] | None = None,
        reranker: Callable[[str, list[SearchHit]], list[SearchHit]] | None = None,
        trigram_fallback: bool = False,
        trigram_fallback_threshold: int | None = None,
    ) -> None:
        self._db_path = db_path
        # Trigram fuzzy-match fallback (issue #41). When enabled, a primary FTS
        # query that returns at or below ``trigram_fallback_threshold`` hits is
        # backfilled from the secondary trigram-tokenized table so a typo or a
        # partial identifier still reaches its document. Off by default so the
        # base behaviour is unchanged; the server sets these from config. Trigram
        # hits are only ever APPENDED after primary hits, never reordering them.
        self.trigram_fallback = trigram_fallback
        self.trigram_fallback_threshold = (
            trigram_fallback_threshold
            if trigram_fallback_threshold is not None
            else DEFAULT_TRIGRAM_FALLBACK_THRESHOLD
        )
        # Search pipeline seams (issue #36). search() runs three stages:
        #   expand-query -> match -> re-rank
        # ``query_expander`` rewrites the term list before the FTS MATCH is built
        # (synonym / co-occurrence expansion plug in here); ``reranker`` reorders
        # or rescores the BM25-ordered hits (status/tier priors, hybrid blending
        # plug in here). Both default to identity so the base behaviour is
        # unchanged. Features set them without touching the other stages.
        self.query_expander = query_expander
        self.reranker = reranker
        # health() is on the readiness-probe path and the degraded-precheck of
        # every read route, so its SQLite read is cached for a short TTL to keep
        # that path memory-only in steady state (the underlying values only
        # change on build(), which invalidates the cache). ``clock`` is injected
        # so the TTL is deterministically testable; monotonic avoids wall-clock
        # jumps. The cache is guarded by a lock because reads may run in the
        # anyio threadpool concurrently.
        self._clock = clock
        self._health_ttl = health_ttl_sec
        self._health_lock = threading.Lock()
        self._health_cache: tuple[float, dict[str, object]] | None = None
        # Test/observability counter: how many times the DB was actually read
        # (cache misses). Not part of the public contract.
        self._health_uncached_calls = 0

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
            # Per-document token sets for the co-occurrence table (issue #40).
            # Collected during the single indexing pass so the build stays one
            # walk; only populated when co-occurrence expansion is enabled.
            build_cooccurrence = cooccurrence_enabled()
            doc_token_sets: list[set[str]] = []
            # Embedding vectors (issue #42). Only when the feature is enabled do
            # we load the (optional) model and collect per-doc embedding text; a
            # default build never touches the embedding dependency. Each entry is
            # (doc_id, text_to_embed), embedded in one batch after the walk and
            # written into the SAME tmp DB so vectors swap atomically with the
            # rest of the index. build_embedder raises loudly if enabled but the
            # dep/model is unavailable, so a misconfigured build fails visibly.
            build_embeddings = embeddings_enabled()
            embedder: Embedder | None = None
            embed_inputs: list[tuple[str, str]] = []
            if build_embeddings:
                from data_olympus.embeddings import build_embedder
                embedder = build_embedder(embeddings_config())
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
                # Secondary trigram index (issue #41), built into the SAME tmp DB
                # so it is swapped atomically with the primary fts table below.
                # A query never sees a half-built trigram table.
                conn.execute(
                    "INSERT INTO fts_trigram "
                    "(id, title, tags, applies_when, description, body) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (doc_id, doc.title, tags_str, applies_when_str, doc.description, doc.body),
                )
                if build_cooccurrence:
                    doc_token_sets.append(
                        tokenize_doc(doc.title, tags_str, doc.description, doc.body)
                    )
                if build_embeddings:
                    # Embed title + description + body: the retrievable semantic
                    # content of the doc. Bounded to keep a huge doc from blowing
                    # the model's context; the head carries the topical signal.
                    embed_text = "\n".join(
                        p for p in (doc.title, doc.description, doc.body) if p
                    )[:_EMBED_TEXT_MAX_CHARS]
                    embed_inputs.append((doc_id, embed_text))
                count += 1
            # Embedding vectors (issue #42): one batched embed of all docs, then
            # persist into the tmp DB. Kept inside the build so vectors are part
            # of the same atomic swap and a query never sees a half-built table.
            if build_embeddings and embedder is not None and embed_inputs:
                vectors = embedder.embed_many([t for _id, t in embed_inputs])
                conn.executemany(
                    "INSERT OR REPLACE INTO doc_vectors (id, vector) VALUES (?, ?)",
                    [
                        (doc_id, serialize_vector(vec))
                        for (doc_id, _t), vec in zip(embed_inputs, vectors, strict=True)
                    ],
                )
            # Co-occurrence / PMI table (issue #40). Built into the SAME tmp DB
            # and swapped atomically with the rest of the index below, so a query
            # never sees a half-built related_terms table.
            if build_cooccurrence:
                params = cooccurrence_build_params()
                table = build_cooccurrence_table(
                    doc_token_sets,
                    k=int(params["k"]),
                    min_count=int(params["min_count"]),
                    min_pmi=float(params["min_pmi"]),
                )
                write_cooccurrence_table(conn, table)
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
        # Invalidate the health cache so the next health() reflects the rebuild
        # immediately rather than serving the pre-swap commit for up to the TTL.
        with self._health_lock:
            self._health_cache = None
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
        # Stage 1 (expand-query): term extraction + optional expansion hook.
        terms = query.split()
        if not terms:
            return []
        if self.query_expander is not None:
            terms = list(self.query_expander(terms))
            if not terms:
                return []
        match_query = self._build_match_expr(terms, columns)
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
            # Over-fetch a wider candidate pool when a reranker will reorder the
            # hits (see stage 3); never fewer than `limit`.
            candidate_limit = limit
            if self.reranker is not None:
                candidate_limit = max(
                    limit,
                    min(
                        max(limit * _RERANK_OVERFETCH_FACTOR, _RERANK_MIN_POOL),
                        _RERANK_MAX_POOL,
                    ),
                )
            params.append(candidate_limit)
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
            # Stage 2 (match): rows come back BM25-ordered from SQLite.
            rows = conn.execute(sql, params).fetchall()
            hits = [
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
            # Stage 2b (fuzzy fallback, issue #41): only when enabled AND the
            # primary query returned few/no hits. Trigram hits are APPENDED after
            # the primary hits and given scores strictly worse than any primary
            # hit, so an exact/primary match is never diluted or reordered by a
            # fuzzy hit (even if a later reranker re-sorts by score). The fallback
            # backfills up to the same candidate_limit.
            if (
                self.trigram_fallback
                and len(hits) <= self.trigram_fallback_threshold
            ):
                hits = self._trigram_backfill(
                    conn,
                    query,
                    primary_hits=hits,
                    base_where=where[1:],
                    base_params=params[1:-1],
                    candidate_limit=candidate_limit,
                )
        finally:
            conn.close()
        # Stage 3 (re-rank): optional hook reorders/rescores (default identity),
        # then truncate the (possibly wider / prepended) pool back to `limit`.
        if self.reranker is not None:
            hits = list(self.reranker(query, hits))[:limit]
        return hits

    def _build_match_expr(self, terms: list[str], columns: list[str] | None) -> str:
        """Match stage: build the FTS5 MATCH expression from query terms.

        Each term is quoted individually (doubling embedded quotes so user input
        cannot break out of its phrase or inject FTS operators) and OR-joined, so
        a natural-language query matches on any term, ranked by bm25. ``columns``
        restricts the match to the given fts columns (ablation); an unknown column
        is a ValueError rather than a silent no-op.
        """
        quoted = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
        if columns:
            unknown = [c for c in columns if c not in _FTS_MATCH_COLUMNS]
            if unknown:
                raise ValueError(f"unknown fts column(s): {unknown}")
            return "{" + " ".join(columns) + "} : (" + quoted + ")"
        return quoted

    def _trigram_backfill(
        self,
        conn: sqlite3.Connection,
        query: str,
        *,
        primary_hits: list[SearchHit],
        base_where: list[str],
        base_params: list[object],
        candidate_limit: int,
    ) -> list[SearchHit]:
        """Backfill few/no primary hits with trigram fuzzy matches (issue #41).

        Runs the trigram (substring) MATCH built from the query's own trigrams,
        excluding ids already returned by the primary query, and appends the
        results AFTER the primary hits. Each appended hit is given a score
        strictly worse (larger, since bm25 is ordered ascending) than any primary
        hit, so a downstream reranker that re-sorts by score cannot lift a fuzzy
        hit above an exact/primary one. The same tier/category/status/doc_type
        filters (``base_where`` / ``base_params``) are applied. A query with no
        trigram (shorter than 3 chars) no-ops and the primary hits are returned
        unchanged.
        """
        trigram_expr = build_trigram_match_expr(query)
        if trigram_expr is None:
            return primary_hits
        seen_ids = {h.id for h in primary_hits}
        # Worst (largest) primary score, so appended trigram scores sort strictly
        # after every primary hit. bm25 scores are <= 0 here; use 0.0 as the floor
        # when there are no primary hits so appended scores stay finite/ordered.
        worst_primary = max((h.score for h in primary_hits), default=0.0)
        where = ["fts_trigram MATCH ?", *base_where]
        params: list[object] = [trigram_expr, *base_params, candidate_limit]
        sql = f"""
            SELECT
                fts_trigram.id AS id,
                docs.path AS path,
                COALESCE(docs.title, '') AS title,
                COALESCE(docs.status, '') AS status,
                COALESCE(docs.type, '') AS doc_type,
                COALESCE(docs.description, '') AS description,
                bm25(fts_trigram) AS tscore
            FROM fts_trigram
            JOIN docs ON docs.id = fts_trigram.id
            WHERE {' AND '.join(where)}
            ORDER BY tscore
            LIMIT ?
        """
        rows = conn.execute(sql, params).fetchall()
        appended: list[SearchHit] = []
        # Assign monotonically increasing scores strictly above worst_primary so
        # the trigram hits keep their own relative (bm25) order but always sort
        # after the primaries.
        offset = 1.0
        for r in rows:
            if r["id"] in seen_ids:
                continue
            seen_ids.add(r["id"])
            appended.append(
                SearchHit(
                    id=r["id"],
                    path=r["path"],
                    title=r["title"],
                    snippet=r["description"],
                    score=worst_primary + offset,
                    status=r["status"],
                    doc_type=r["doc_type"],
                )
            )
            offset += 1.0
        return [*primary_hits, *appended]

    def related_terms(self, term: str, *, limit: int) -> builtins.list[str]:
        """Return up to ``limit`` corpus co-occurring terms for ``term``.

        Reads the ``related_terms`` table populated at build time (issue #40),
        strongest (highest-PMI) first. Opens a per-call connection so it always
        reads the atomically-swapped current index. An index built before the
        table existed (no ``related_terms`` table) or an unknown term yields the
        empty list rather than raising. This is the query-time backing for the
        co-occurrence expander wired via ``make_cooccurrence_expander``.
        """
        if limit <= 0 or not self._db_path.exists():
            return []
        conn = self._connect()
        try:
            return lookup_related_terms(conn, term, limit=limit)
        except sqlite3.OperationalError:
            # Table absent (index predates the schema); degrade to no expansion.
            return []
        finally:
            conn.close()

    def cooccurrence_expander(
        self, *, k: int = DEFAULT_K, max_terms: int = DEFAULT_MAX_TERMS,
    ) -> Callable[[list[str]], list[str]]:
        """Return a ``query_expander`` backed by this index's related-terms table.

        The expander appends, for each query term, up to ``k`` co-occurring terms
        (down-weighted by appending after the originals), bounded overall by
        ``max_terms``. Bound to ``self.related_terms`` so it always reads the
        currently-swapped index. Compose it AFTER the synonym expander via
        ``cooccurrence.compose_expanders``.
        """
        return make_cooccurrence_expander(
            lambda term, kk: self.related_terms(term, limit=kk),
            k=k,
            max_terms=max_terms,
        )

    def get_vector(self, id: str) -> builtins.list[float] | None:
        """Return the stored embedding vector for ``id``, or None (issue #42).

        Reads the ``doc_vectors`` table populated at build time only when the
        embeddings feature was enabled. A build that predates the table, a doc
        with no vector, or the feature being off all yield None (the hybrid
        reranker treats a missing vector as a neutral cosine, never a drop).
        Opens a per-call connection so it always reads the atomically-swapped
        current index.
        """
        if not self._db_path.exists():
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT vector FROM doc_vectors WHERE id = ?", (id,)
            ).fetchone()
        except sqlite3.OperationalError:
            # Table absent (index predates the schema); degrade to no vector.
            return None
        finally:
            conn.close()
        if row is None:
            return None
        return deserialize_vector(row["vector"])

    def make_hybrid_reranker(
        self, embedder: Embedder, *, weight: float,
    ) -> Callable[[str, list[SearchHit]], list[SearchHit]]:
        """Return a hybrid reranker bound to this index's stored vectors.

        Blends normalised bm25 with query-doc cosine (issue #42). ``embedder``
        embeds the query once per search; ``weight`` in [0, 1] is the cosine
        fraction of the blended score. The query is embedded lazily (only when a
        search actually runs), and a query the model cannot embed degrades to
        bm25 ordering. Compose this as the ``inner`` reranker under the id/tag
        short-circuit so an exact id/tag still wins (see server.build_app).
        """
        return make_hybrid_reranker(
            embed_query=lambda q: embedder.embed_one(q) if q.strip() else None,
            get_vector=self.get_vector,
            weight=weight,
        )

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

    def ids_with_exact_tag(self, tag: str) -> set[str]:
        """Return the ids of docs carrying ``tag`` as an EXACT (whole) tag.

        Tags are stored space-joined in ``docs.tags``. A LIKE query would match
        substrings ('style' inside 'styleguide'), so candidates are pre-filtered
        with LIKE for index use, then confirmed by splitting the stored value and
        checking membership. ``%`` and ``_`` in the tag are escaped so they are
        matched literally rather than as LIKE wildcards, keeping the candidate
        pre-filter tight. An empty/blank tag yields the empty set.
        """
        tag = tag.strip()
        if not tag:
            return set()
        # Escape LIKE metacharacters so the pre-filter matches ``tag`` literally.
        escaped = tag.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, tags FROM docs WHERE tags LIKE ? ESCAPE '\\'",
                (f"%{escaped}%",),
            ).fetchall()
        finally:
            conn.close()
        return {r["id"] for r in rows if tag in (r["tags"] or "").split()}

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
        """Return commit, built_at, total_docs, db_size_bytes.

        Cached for ``health_ttl_sec`` (see __init__): within the window the
        cached snapshot is returned without touching SQLite, so the readiness
        probe and per-route degraded-precheck stay off the DB under load. The
        cache is invalidated by build(); the underlying values change nowhere
        else.
        """
        now = self._clock()
        with self._health_lock:
            cached = self._health_cache
            if cached is not None and (now - cached[0]) < self._health_ttl:
                return dict(cached[1])
        # Cache miss: read the DB outside the lock (the connection is per-call,
        # so a concurrent miss is harmless — both compute the same snapshot).
        snapshot = self._health_uncached()
        with self._health_lock:
            self._health_cache = (now, dict(snapshot))
        return snapshot

    def _health_uncached(self) -> dict[str, object]:
        """The raw health read, bypassing the cache. See health()."""
        self._health_uncached_calls += 1
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
