"""SQLite FTS5 index over the KB markdown files."""
from __future__ import annotations

import datetime
import json
import logging
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
    EmbeddingsConfig,
    build_embedder,
    cosine,
    deserialize_vector,
    make_hybrid_reranker,
    serialize_vector,
)
from data_olympus.format.validate import (
    IN_FORCE_STATUSES,
    RESERVED,
    graph_excluded_ids_sql,
    in_force_sql_fragment,
    is_inbox_path,
    not_expired_sql_fragment,
    not_inbox_sql_fragment,
    today_iso,
)
from data_olympus.maintenance import (
    DEFAULT_EXPIRING_SOON_DAYS,
    DEFAULT_LEDGER_PATH,
    DEFAULT_RECENTLY_EXPIRED_DAYS,
    DocAuditRow,
    MaintenanceState,
    compute_maintenance_state,
)
from data_olympus.markdown_parse import ParsedDoc, parse_text_checked
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
    git_remote_url TEXT,
    valid_from TEXT,
    valid_until TEXT,
    last_verified TEXT,
    recheck_by TEXT,
    verification_source TEXT,
    is_inbox INTEGER NOT NULL DEFAULT 0
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
CREATE TABLE IF NOT EXISTS edges (
    source_id TEXT NOT NULL,
    rel TEXT NOT NULL,
    target_id TEXT NOT NULL,
    PRIMARY KEY (source_id, rel, target_id)
);
CREATE INDEX IF NOT EXISTS edges_target_idx ON edges (target_id);
""" + RELATED_TERMS_SCHEMA + EMBEDDINGS_SCHEMA

# The trigram secondary index (issue #41) is a SECOND full copy of every indexed
# column tokenized into 3-char substrings: roughly a 2-3x index-size / build-time
# tax. Finding (c): it was created and populated UNCONDITIONALLY even when the
# trigram fallback is off, so every deployment paid that tax for a feature it was
# not using. It is now created and populated only when the fallback is enabled
# for THIS index (``self.trigram_fallback``), appended to the schema at build
# time rather than baked into the base constant.

# Informational only; recorded in the meta table for observability. Rebuild
# is guaranteed by bootstrap_now=True calling Index.build() unconditionally
# on container start, not by a version-mismatch check.
# v6 adds the related_terms co-occurrence table (issue #40).
# v7 adds the fts_trigram fuzzy-match table (issue #41).
# v8 adds the doc_vectors table (issue #42, populated only when embeddings on).
# v9 adds the edges table (issue #110 slice 1: typed lifecycle relationships
# supersedes/superseded_by/contradicts, parsed from front matter into
# (source_id, rel, target_id) rows). Slice 2 consumes this table for in-force
# graph exclusion and retrieval surfacing; this slice only populates it.
# v10 adds the validity/freshness columns (issue #107): valid_from, valid_until,
# last_verified, recheck_by, verification_source.
# v11 adds the is_inbox column (issue #109): 1 when the doc's path falls under
# the memory-inbox prefix (format.validate.is_inbox_path), computed once at
# build time so the memory-inbox in-force floor is a plain column check
# (format.validate.not_inbox_sql_fragment) rather than a per-query prefix scan.
_SCHEMA_VERSION = "11"

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _ParsedFile:
    """One file read + parsed once during build (finding (i)).

    Carries everything the build needs so a file is neither re-read nor re-parsed:
    the relative path, the resolved doc id, the ParsedDoc, the raw text (stored as
    ``content_markdown``), and whether its front matter was present but malformed
    (finding (j)).
    """

    rel: Path
    doc_id: str
    doc: ParsedDoc
    raw_text: str
    malformed_frontmatter: bool


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

# Dense (semantic) candidate SOURCE defaults (issue #42, reviewer concern 1).
# When embeddings are enabled, search() unions the top-N docs by query-doc cosine
# into the FTS candidate pool so a paraphrase with ZERO lexical overlap can still
# retrieve its doc (bm25 alone never surfaces it). Two knobs, both config-driven:
#   * DEFAULT_DENSE_CANDIDATE_COUNT: how many nearest neighbours to consider.
#   * DEFAULT_DENSE_MIN_COSINE: a minimum cosine a neighbour must clear to be
#     added. This is the abstention guard: a negative / out-of-scope query whose
#     nearest neighbour is only weakly similar pulls in NOTHING, so the dense
#     source does not blow up the false-positive rate on the negative stratum.
DEFAULT_DENSE_CANDIDATE_COUNT = 10
DEFAULT_DENSE_MIN_COSINE = 0.5

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
    # In-force: the guidance that currently applies. Boost. The in-force class
    # (active/accepted/approved, incl. the target KB's ``approved``) is defined
    # once in format.validate.IN_FORCE_STATUSES and expanded here so the soft
    # rerank and the hard ``in_force`` filter share a single source of truth.
    **{s: -1.0 for s in sorted(IN_FORCE_STATUSES)},
    # Retired or superseded: kept for history, must not outrank its replacement.
    "superseded": 2.0,
    "deprecated": 2.0,
    "rejected": 2.0,
    # Not yet in force: a draft should not beat an active doc.
    "draft": 1.0,
    "proposed": 1.0,
}

# Deterministic, sorted materialisation of the in-force class for use as SQL
# ``IN (...)`` parameters by search(in_force=True) and dense_candidates. Sorted
# so the generated SQL/params are stable across runs. Derived from the single
# source (format.validate.IN_FORCE_STATUSES); do not hand-list statuses here.
_IN_FORCE_STATUS_LIST: tuple[str, ...] = tuple(sorted(IN_FORCE_STATUSES))

# Virtual status autofill (issue #147 / KNA-69). The default status written IN
# MEMORY into the SQLite `docs.status` column for a legacy doc that has no
# `status` field, when autofill is enabled for the index. `active` preserves the
# pre-0.4.0 in-force behavior (it is in format.validate.IN_FORCE_STATUSES). The
# markdown source file is untouched by the build; the same default is written to
# disk only by the explicit `data-olympus migrate status --apply` lane
# (data_olympus.status_migrate.DEFAULT_STATUS, kept in sync with this value).
DEFAULT_AUTOFILL_STATUS = "active"

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


# Rank classes. A hit's rank class is the OUTER, score-independent sort key: a
# larger class always sorts after a smaller one, no matter what a reranker does
# to the score. PRIMARY hits (the user's own terms) always outrank BACKFILL hits
# (expansion synonyms, co-occurrence, trigram fuzzy matches). This is what makes
# the "backfill hits are never reordered above primaries" invariant genuinely
# TRUE (finding (d)): the status/hybrid rerankers ADD deltas to and RE-NORMALISE
# the score, so a score-only "strictly worse" floor could be lifted above a
# primary by an active-status boost; a separate rank-class key cannot.
RANK_CLASS_PRIMARY = 0
RANK_CLASS_BACKFILL = 1


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One FTS hit.

    ``rank_class`` (see RANK_CLASS_*) is the outer sort key: a hit from a
    backfill pass (expansion/trigram) carries RANK_CLASS_BACKFILL so it can never
    be reordered above a RANK_CLASS_PRIMARY hit, independent of any score
    arithmetic a reranker applies. Rerankers sort by ``(rank_class, score)``.
    """

    id: str
    path: str
    title: str
    snippet: str
    score: float
    status: str = ""
    doc_type: str = ""
    rank_class: int = RANK_CLASS_PRIMARY
    # validity/freshness (issue #107), ISO YYYY-MM-DD or "" when absent. Carried
    # so callers (kb_search_fn) can compute the deviation-only ``freshness``
    # indicator without a second doc lookup.
    valid_from: str = ""
    valid_until: str = ""
    recheck_by: str = ""
    # Memory-inbox in-force floor (issue #109): 1:1 with the `docs.is_inbox`
    # column, carried so a caller (kb_search_fn) can compute the single-sourced
    # `is_in_force(..., is_inbox=...)` predicate without a second doc lookup.
    is_inbox: bool = False
    # Lifecycle-relationship surfacing (issue #110 slice 2): the SORTED list of
    # ids that supersede this doc, computed as the UNION of the doc's own
    # frontmatter `superseded_by` claim and any reverse `supersedes` edge
    # targeting it (see index._superseded_by_map). Deviation-only: emitted by
    # SearchHitModel.compact_dump only when non-empty, same pattern as
    # `status`/`freshness`. Purely informational -- carries NO ranking or
    # filtering effect on its own (a hit's presence/absence is governed by the
    # `in_force` graph-exclusion filter, not by this field).
    superseded_by: tuple[str, ...] = ()


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
        # Sort by rank_class FIRST (primaries before backfill), then adjusted
        # score. A backfill hit's active-status boost can move it earlier WITHIN
        # its class but never above a primary (finding (d)).
        adjusted.sort(key=lambda h: (h.rank_class, h.score))
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
    # validity/freshness (issue #107), ISO YYYY-MM-DD or "" when absent.
    valid_from: str = ""
    valid_until: str = ""
    last_verified: str = ""
    recheck_by: str = ""
    verification_source: str = ""
    # Memory-inbox in-force floor (issue #109): see SearchHit.is_inbox.
    is_inbox: bool = False
    # Lifecycle-relationship surfacing (issue #110 slice 2), computed at read
    # time from the `edges` table (see _superseded_by_map / _edges_from /
    # _edges_targeting). `superseded_by` is the UNION of this doc's own
    # frontmatter `superseded_by` claim and any reverse `supersedes` edge
    # targeting it -- ONE consistent shape covering both the honest
    # self-declared case and the "forgotten status flip" case where the
    # superseding doc names this one in its `supersedes` list but this doc's
    # own frontmatter never got a matching `superseded_by`. `contradicts` is
    # this doc's own frontmatter list (direct, never affects filtering or
    # ranking); `contradicted_by` is the computed reverse: every other doc
    # whose `contradicts` names this one. All three are dangling-safe: an edge
    # whose other end has no `docs` row is never surfaced.
    superseded_by: tuple[str, ...] = ()
    contradicts: tuple[str, ...] = ()
    contradicted_by: tuple[str, ...] = ()


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


# ``validity_state`` facet (issue #107): an audit-query filter over the
# validity/freshness columns, orthogonal to ``in_force``. Three forms:
#   "expired"            -> valid_until strictly before today.
#   "stale"               -> recheck_by strictly before today (advisory only).
#   "expiring_within:N"   -> valid_until in [today, today + N days] inclusive.
_VALIDITY_STATE_KINDS = frozenset({"expired", "stale", "expiring_within"})


def _parse_validity_state(value: str) -> tuple[str, int | None]:
    """Parse a ``validity_state`` facet value into ``(kind, days)``.

    ``days`` is only meaningful for ``"expiring_within"`` (None otherwise).
    Raises ValueError for an unrecognized kind or a non-integer/negative day
    count, so a malformed facet value fails loudly rather than silently
    matching nothing.
    """
    if ":" in value:
        kind, _, rest = value.partition(":")
        if kind != "expiring_within":
            raise ValueError(f"unknown validity_state {value!r}")
        try:
            days = int(rest)
        except ValueError as exc:
            raise ValueError(f"validity_state {value!r}: N must be an integer") from exc
        if days < 0:
            raise ValueError(f"validity_state {value!r}: N must not be negative")
        return kind, days
    if value not in _VALIDITY_STATE_KINDS:
        raise ValueError(f"unknown validity_state {value!r}")
    return value, None


def _add_days_iso(today: str, days: int) -> str:
    """Return ``today`` (ISO YYYY-MM-DD) plus ``days`` calendar days, as ISO."""
    return (datetime.date.fromisoformat(today) + datetime.timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# Lifecycle-relationship surfacing helpers (issue #110 slice 2)
# ---------------------------------------------------------------------------
#
# Shared by Index.get() (single id) and Index.search() (batched over the
# current hit pool). Both directions JOIN edges to docs on the OTHER end (the
# end NOT already constrained by the caller's id filter) so a dangling edge
# (either end missing a `docs` row) is never surfaced -- the same
# never-trust-edges-verbatim discipline the graph-exclusion SQL uses.


def _edges_from(
    conn: sqlite3.Connection, rel: str, source_ids: builtins.list[str],
) -> dict[str, builtins.list[str]]:
    """``{source_id: [target_id, ...]}`` for ``rel`` edges FROM each of
    ``source_ids``, restricted to targets that exist as real docs."""
    if not source_ids:
        return {}
    placeholders = ", ".join("?" for _ in source_ids)
    rows = conn.execute(
        "SELECT DISTINCT edges.source_id AS src, edges.target_id AS tgt "
        "FROM edges JOIN docs ON docs.id = edges.target_id "
        f"WHERE edges.rel = ? AND edges.source_id IN ({placeholders})",
        [rel, *source_ids],
    ).fetchall()
    out: dict[str, builtins.list[str]] = {}
    for r in rows:
        out.setdefault(r["src"], []).append(r["tgt"])
    return out


def _edges_targeting(
    conn: sqlite3.Connection, rel: str, target_ids: builtins.list[str],
) -> dict[str, builtins.list[str]]:
    """``{target_id: [source_id, ...]}`` for ``rel`` edges TARGETING each of
    ``target_ids``, restricted to sources that exist as real docs."""
    if not target_ids:
        return {}
    placeholders = ", ".join("?" for _ in target_ids)
    rows = conn.execute(
        "SELECT DISTINCT edges.target_id AS tgt, edges.source_id AS src "
        "FROM edges JOIN docs ON docs.id = edges.source_id "
        f"WHERE edges.rel = ? AND edges.target_id IN ({placeholders})",
        [rel, *target_ids],
    ).fetchall()
    out: dict[str, builtins.list[str]] = {}
    for r in rows:
        out.setdefault(r["tgt"], []).append(r["src"])
    return out


def _superseded_by_map(
    conn: sqlite3.Connection, ids: builtins.list[str],
) -> dict[str, builtins.list[str]]:
    """``{doc_id: [superseder_id, ...]}`` (sorted, deduped) for every id in
    ``ids`` that is superseded, by ONE consistent rule (issue #110 slice 2):
    the UNION of the doc's own frontmatter ``superseded_by`` claim (the
    ``superseded_by`` rel, edge FROM the doc) and any reverse ``supersedes``
    edge targeting it (the ``supersedes`` rel, edge TO the doc). The union
    covers both the honest self-declared case and the "forgotten status flip"
    case where only the superseding doc's own ``supersedes`` list names this
    one. An id with neither is omitted from the returned dict entirely (never
    an empty-list entry), so callers can use a plain ``.get(id, [])``.
    """
    if not ids:
        return {}
    own = _edges_from(conn, "superseded_by", ids)
    reverse = _edges_targeting(conn, "supersedes", ids)
    out: dict[str, builtins.list[str]] = {}
    for doc_id in ids:
        combined = sorted(set(own.get(doc_id, [])) | set(reverse.get(doc_id, [])))
        if combined:
            out[doc_id] = combined
    return out


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
        embeddings: EmbeddingsConfig | None = None,
        embedder: Embedder | None = None,
        dense_candidate_count: int = DEFAULT_DENSE_CANDIDATE_COUNT,
        dense_min_cosine: float = DEFAULT_DENSE_MIN_COSINE,
        maintenance_ledger_path: str = DEFAULT_LEDGER_PATH,
        maintenance_recently_expired_days: int = DEFAULT_RECENTLY_EXPIRED_DAYS,
        maintenance_expiring_soon_days: int = DEFAULT_EXPIRING_SOON_DAYS,
        status_autofill: bool = True,
    ) -> None:
        self._db_path = db_path
        # Virtual status autofill (issue #147 / KNA-69). When True (default), a
        # doc missing `status` is indexed with `docs.status` = active IN MEMORY,
        # so a pre-0.4.0 corpus keeps its in-force docs after upgrade without the
        # build ever touching the markdown source. The physical missing-status
        # gap is still reported by the maintenance ledger (DocAuditRow carries the
        # PHYSICAL status), so the ledger keeps nagging until an operator runs
        # `data-olympus migrate status --apply`. Off restores the conservative
        # pre-#147 behavior (a status-less doc is served but never in-force).
        self._status_autofill = status_autofill
        # Embeddings (issue #42) are threaded in from Config, NOT re-read from env
        # here (reviewer concern 2): Config (via load_config) is the single source
        # of truth for KB_EMBEDDINGS_MODE/MODEL/WEIGHT. ``embeddings`` being non-
        # None is what turns the feature on for THIS index; ``embedder`` is the
        # (lazily loadable) model shared between build() and the query-time dense
        # source. When ``embeddings`` is None the embedding path is fully inert:
        # build() stores no vectors and search() is byte-for-byte pure FTS.
        self._embeddings = embeddings
        self._embedder = embedder
        # Dense candidate SOURCE knobs (reviewer concern 1). Only consulted when
        # both an embeddings config and an embedder are present.
        self._dense_candidate_count = dense_candidate_count
        self._dense_min_cosine = dense_min_cosine
        # Trigram fuzzy-match fallback (issue #41). When enabled, a primary FTS
        # query that returns at or below ``trigram_fallback_threshold`` hits is
        # backfilled from the secondary trigram-tokenized table so a typo or a
        # partial identifier still reaches its document. Off by default so the
        # base behaviour is unchanged; the server sets these from config. Trigram
        # hits are APPENDED after primary hits and stamped RANK_CLASS_BACKFILL,
        # which is the outer sort key in every reranker, so they are never
        # reordered above a primary hit (finding (d)).
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
        # Per-build id->vector matrix cache (finding (g)). The hybrid reranker
        # asked get_vector() once PER candidate hit, and each call opened a fresh
        # SQLite connection; dense_candidates re-read + deserialized the whole
        # doc_vectors table on every query. Both now share a single in-memory
        # matrix loaded once per build. The cache is keyed by the db file's
        # (size, mtime_ns) so the atomic os.replace() swap in build() transparently
        # invalidates it (the new inode has a different mtime), with no explicit
        # invalidation call needed even for a rebuild by a DIFFERENT Index object.
        # Guarded by a lock because query reads run concurrently in the threadpool.
        self._vector_lock = threading.Lock()
        self._vector_cache: tuple[
            tuple[int, int], dict[str, builtins.list[float]]
        ] | None = None
        # Test/observability counter: how many times the vector matrix was
        # actually loaded from the DB (cache misses). Not part of the public
        # contract; asserted by the batch-fetch test to prove one load per build.
        self._vector_loads = 0
        # Health-visible count of docs whose front-matter block was present but
        # malformed at the last build, so their status/supersedes were silently
        # dropped (finding (j)). Updated by build(); surfaced via health() and the
        # ``malformed_frontmatter_count`` property. Exposing it on the Index (with
        # a WARN log per doc) is the minimal, non-invasive plumbing; wiring it into
        # the /health degraded signal is a tracked follow-up.
        self._malformed_frontmatter_count = 0
        # Health-visible count of docs whose ``validity`` block was present but
        # malformed at the last build (issue #107). Updated by build(); surfaced
        # via health() and the ``malformed_validity_count`` property.
        self._malformed_validity_count = 0
        # Maintenance ledger (issue #113): the corpus-state audit computed at
        # the LAST build, cached here so kb_health/kb_consult and the
        # ledger-commit hook can read it without touching SQLite. None before
        # the first build() call. ``_maintenance_ledger_path`` is excluded from
        # its own audit (see maintenance.compute_maintenance_state).
        self._maintenance_ledger_path = maintenance_ledger_path
        self._maintenance_recently_expired_days = maintenance_recently_expired_days
        self._maintenance_expiring_soon_days = maintenance_expiring_soon_days
        self._maintenance_state: MaintenanceState | None = None
        # In-process memo of the last MaintenanceState the maintenance-ledger
        # commit hook successfully committed (set by
        # maintenance.maybe_update_ledger, never by build()). This is the loop
        # guard for the window between a ledger commit and its publication
        # (push -> merge to main -> re-index): during that window the INDEX
        # still holds the pre-commit ledger copy, so without this memo every
        # pull-loop tick would look like "state changed" and commit a
        # duplicate. Not persisted: after a restart the system worktree's HEAD
        # (which survives restarts; unpushed commits block its GC) covers the
        # same window (see maybe_update_ledger's worktree check).
        self.maintenance_last_committed_state: MaintenanceState | None = None

    @property
    def maintenance_state(self) -> MaintenanceState | None:
        """The corpus-state audit computed at the last build(), or None before
        the first build (see data_olympus.maintenance.MaintenanceState)."""
        return self._maintenance_state

    @property
    def malformed_frontmatter_count(self) -> int:
        """Docs with a present-but-malformed front-matter block at last build (j).

        A YAML typo in a doc's front matter makes the parser fall back to "no
        front matter", silently dropping its ``status`` / ``supersedes`` and
        disabling that doc's staleness protection. This counter (also logged at
        WARN per doc during build) makes the condition observable.
        """
        return self._malformed_frontmatter_count

    @property
    def malformed_validity_count(self) -> int:
        """Docs with a present-but-malformed ``validity`` block at last build.

        A malformed date anywhere in ``validity`` fails the whole block open
        (treated as absent, fail open for visibility); this counter (also
        logged at WARN per doc during build) makes that condition observable.
        """
        return self._malformed_validity_count

    def graph_excluded_count(self, *, today: str | None = None) -> int:
        """Count of docs CURRENTLY excluded from in-force retrieval by the
        supersession-graph rule (issue #110 slice 2): the TARGET of a
        `supersedes` edge whose SOURCE is itself in-force.

        Computed LIVE against ``today`` (default: the real wall clock via
        :func:`today_iso`; injectable for deterministic tests) from the same
        SQL definition the retrieval-time filter uses
        (:func:`data_olympus.format.validate.graph_excluded_ids_sql`), so the
        counter can never drift from the filter it reports on. Live (not
        build-time) evaluation matters because the in-force-source guard is
        wall-clock-relative: a source whose validity window opens or closes
        between rebuilds changes retrieval behavior per query, and a counter
        frozen at build time would keep reporting the stale value (codex
        review blocker). Returns 0 when the index file or the ``edges`` table
        does not exist (an index predating schema v9).
        """
        if not self._db_path.exists():
            return 0
        resolved_today = today if today is not None else today_iso()
        placeholders = ", ".join("?" for _ in _IN_FORCE_STATUS_LIST)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(DISTINCT target_id) FROM "
                f"({graph_excluded_ids_sql(placeholders)})",
                [*_IN_FORCE_STATUS_LIST, resolved_today, resolved_today],
            ).fetchone()
        except sqlite3.OperationalError:
            # edges table absent (index predates schema v9): nothing excluded.
            return 0
        finally:
            conn.close()
        return int(row[0]) if row else 0

    def graph_excluded_ids(self, *, today: str | None = None) -> set[str]:
        """The doc ids CURRENTLY graph-excluded from in-force retrieval.

        Same live SQL definition as :meth:`graph_excluded_count` (single
        source: :func:`data_olympus.format.validate.graph_excluded_ids_sql`),
        returned as a set so a response-shaping caller (the computed per-doc
        ``in_force`` boolean on kb_get / kb_search hits, issue #109) can
        compose the graph rule into the full in-force predicate -- status
        class AND validity window AND not-inbox AND not-graph-excluded --
        with ONE query per request instead of one per hit. Returns the empty
        set when the index file or the ``edges`` table does not exist.
        """
        if not self._db_path.exists():
            return set()
        resolved_today = today if today is not None else today_iso()
        placeholders = ", ".join("?" for _ in _IN_FORCE_STATUS_LIST)
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT DISTINCT target_id FROM "
                f"({graph_excluded_ids_sql(placeholders)})",
                [*_IN_FORCE_STATUS_LIST, resolved_today, resolved_today],
            ).fetchall()
        except sqlite3.OperationalError:
            # edges table absent (index predates schema v9): nothing excluded.
            return set()
        finally:
            conn.close()
        return {r["target_id"] for r in rows}

    def _resolve_embedder(self) -> Embedder | None:
        """Return the embedder to use, loading it from the threaded config once.

        Returns None when embeddings are not configured for this index (feature
        off). When configured, an explicitly-injected ``embedder`` (e.g. shared
        with the query-time reranker, or a test double) is preferred; otherwise
        the model is loaded from ``self._embeddings`` on first use and cached, so
        build() and the query-time dense source share a single loaded model.
        ``build_embedder`` raises loudly if the dep/model is unavailable, so a
        misconfigured build fails visibly rather than silently shipping lexical.
        """
        if self._embeddings is None:
            return None
        if self._embedder is None:
            self._embedder = build_embedder(self._embeddings)
        return self._embedder

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _git_last_modified(kb_root: Path, rel: Path) -> tuple[str, str]:
        """Return (iso_timestamp, source). source is 'git' or 'mtime'.

        Per-file git query. Retained for callers that need a single file's
        timestamp; the index build uses :func:`_git_last_modified_map` to fetch
        every file's last-commit time in ONE ``git log`` pass (finding (i)).
        """
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

    # Sentinel prefixing each commit's timestamp record in the batched git log,
    # so the parser distinguishes a timestamp header from a changed-path record. A
    # control char that cannot appear in a git path keeps the split unambiguous.
    _GIT_LOG_MARK = "\x01"

    @classmethod
    def _git_last_modified_map(cls, kb_root: Path) -> dict[str, str]:
        """Map every tracked path to its last-commit ISO timestamp in ONE pass (i).

        Runs a single ``git log -z --name-only`` over the whole history and
        records, for each path, the FIRST (most recent, since git log is
        newest-first) commit timestamp that touched it. Replaces the previous
        one-subprocess-per-file loop (N git invocations -> 1). Returns an empty
        map when the directory is not a git repo or git is unavailable; the build
        then falls back to filesystem mtime per file exactly as before, so the
        ``source`` recorded per doc ('git' vs 'mtime') is unchanged.

        Exactness (reviewer concern): the output is NUL-delimited (``-z``) and
        ``core.quotePath=false`` disables git's octal-quoting of non-ASCII paths,
        so a path with spaces, unicode, or other special characters round-trips
        byte-for-byte and is NOT lost to the mtime fallback. Each record is either
        ``<MARK><iso>`` (a commit header) or a changed path; the first path of a
        commit carries a leading ``\\n`` (git's format-block-to-name-list
        separator) which is stripped, but the path body is otherwise untouched (no
        ``.strip()``, so trailing/leading whitespace in a filename survives). Paths
        are relative to ``kb_root`` with forward slashes, matching ``str(rel)``.
        Renames are not requested (no ``-M``), so paths are literal.
        """
        try:
            result = subprocess.run(
                ["git", "-C", str(kb_root), "-c", "core.quotePath=false", "log",
                 "-z", f"--format={cls._GIT_LOG_MARK}%cI", "--name-only", "HEAD"],
                capture_output=True, text=True, check=False, timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return {}
        if result.returncode != 0 or not result.stdout:
            return {}
        out: dict[str, str] = {}
        current_iso = ""
        for record in result.stdout.split("\x00"):
            if not record:
                continue
            if record.startswith(cls._GIT_LOG_MARK):
                # Commit header: MARK + iso. ISO-8601 timestamps carry no
                # surrounding whitespace, so stripping the iso is safe/exact.
                current_iso = record[len(cls._GIT_LOG_MARK):].strip()
                continue
            # A changed-path record. The first path of a commit is prefixed with a
            # single '\n' (git's separator between the format block and the name
            # list); remove exactly that, but do NOT strip the path body so a
            # filename with leading/trailing whitespace round-trips exactly.
            path = record[1:] if record.startswith("\n") else record
            # git log is newest-first, so the first time we see a path is its most
            # recent commit; keep that and ignore older ones.
            if path and current_iso and path not in out:
                out[path] = current_iso
        return out

    @staticmethod
    def _resolve_last_modified(
        kb_root: Path, rel: Path, git_mtimes: dict[str, str],
    ) -> tuple[str, str]:
        """Return (iso_timestamp, source) for ``rel`` using the batched git map (i).

        A hit in ``git_mtimes`` (the single ``git log`` pass) yields ('git', iso),
        byte-for-byte the same value the per-file ``git log`` produced. A miss
        (untracked file, or not a git repo at all) falls back to filesystem mtime,
        exactly as the per-file path did, so the recorded ``source`` is unchanged.
        """
        iso = git_mtimes.get(str(rel))
        if iso:
            return iso, "git"
        mtime = (kb_root / rel).stat().st_mtime
        return (
            datetime.datetime.fromtimestamp(mtime, tz=datetime.UTC).isoformat(),
            "mtime",
        )

    def build(
        self, kb_root: Path, *, source_commit: str, today: str | None = None,
    ) -> IndexBuildResult:
        """Walk kb_root for .md files and (re)build the FTS index using atomic swap.

        ``today`` (ISO ``YYYY-MM-DD``) drives the maintenance-ledger expiry
        window computation (issue #113); defaults to :func:`today_iso` (the
        real wall clock) but is injectable so tests are deterministic.
        """
        if not kb_root.is_dir():
            raise NotADirectoryError(f"KB root not a directory: {kb_root}")

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._db_path.with_name(
            f"{self._db_path.name}.tmp.{os.getpid()}.{source_commit}.db"
        )
        if tmp_path.exists():
            tmp_path.unlink()  # stale tmp from previous failed build

        # Single pass over the corpus (finding (i)): read + parse each file EXACTLY
        # ONCE and reuse the result for duplicate detection, the docs/fts inserts,
        # and (via the raw text) the stored content_markdown. The previous build
        # read/parsed each file three times (a first-pass parse, a second-pass
        # parse, and a separate content read). Each ``_ParsedFile`` carries the
        # raw text, the ParsedDoc, and the malformed-frontmatter flag (finding
        # (j)).
        parsed: list[_ParsedFile] = []
        seen: dict[str, list[str]] = {}
        for md in sorted(kb_root.rglob("*.md")):
            rel = md.relative_to(kb_root)
            if _is_excluded(rel):
                continue
            raw_text = md.read_text(encoding="utf-8")
            doc, malformed = parse_text_checked(md, raw_text)
            doc_id = doc.id or _derive_id_from_path(rel)
            seen.setdefault(doc_id, []).append(str(rel))
            parsed.append(
                _ParsedFile(
                    rel=rel, doc_id=doc_id, doc=doc,
                    raw_text=raw_text, malformed_frontmatter=malformed,
                )
            )
        conflicts = {id_: paths for id_, paths in seen.items() if len(paths) > 1}
        if conflicts:
            raise DuplicateIdError(conflicts)
        # One git pass for every file's last-commit time (finding (i)); empty when
        # not a git repo, in which case each file falls back to mtime below.
        git_mtimes = self._git_last_modified_map(kb_root)

        # Build the new index into the tmp file
        conn = sqlite3.connect(tmp_path)
        conn.row_factory = sqlite3.Row
        try:
            # Finding (c): the trigram table is created only when the trigram
            # fallback is enabled for this index. With it off the schema (and the
            # per-doc insert below) skip it, so a default deployment no longer
            # pays the 2-3x index-size / build-time cost of a second tokenized
            # copy of the corpus.
            schema = _SCHEMA
            if self.trigram_fallback:
                schema = schema + TRIGRAM_FTS_SCHEMA
            conn.executescript(schema)
            count = 0
            path_rules = _load_path_rules()
            # Per-document token sets for the co-occurrence table (issue #40).
            # Collected during the single indexing pass so the build stays one
            # walk; only populated when co-occurrence expansion is enabled.
            build_cooccurrence = cooccurrence_enabled()
            doc_token_sets: list[set[str]] = []
            # Lifecycle-relationship edges (issue #110 slice 1). Collected during
            # the single indexing pass and written into the edges table in one
            # batch after the walk, same pattern as embed_inputs below, so the
            # edges table is part of the same atomic tmp-DB swap. Deduplicated via
            # a set: a doc listing the same target twice in one field (e.g. a
            # repeated id in `supersedes`) must not raise on the edges table's
            # composite primary key.
            edge_rows: set[tuple[str, str, str]] = set()
            # Embedding vectors (issue #42). Whether/what to embed is decided from
            # the threaded ``self._embeddings`` config (reviewer concern 2), NOT
            # from an env re-read: Config is the single source of truth. Only when
            # a config is present do we load the (optional) model and collect
            # per-doc embedding text; a default build never touches the embedding
            # dependency. Each entry is (doc_id, text_to_embed), embedded in one
            # batch after the walk and written into the SAME tmp DB so vectors
            # swap atomically with the rest of the index. _resolve_embedder raises
            # loudly if enabled but the dep/model is unavailable.
            build_embeddings = self._embeddings is not None
            embedder: Embedder | None = None
            embed_inputs: list[tuple[str, str]] = []
            if build_embeddings:
                embedder = self._resolve_embedder()
            # Count of docs whose front-matter block was present but malformed, so
            # status/supersedes were silently dropped (finding (j)). Surfaced on
            # the Index and logged at WARN so a YAML typo that disables a doc's
            # staleness protection is visible rather than silent.
            malformed_frontmatter = 0
            # Docs whose ``validity`` block was present but malformed (issue
            # #107): the whole block failed open (absent), so the doc's
            # expiry/staleness protection is silently disabled. Surfaced the
            # same way as malformed_frontmatter above (WARN log + health
            # counter), but tracked separately since a doc can have valid
            # front matter overall yet a malformed ``validity`` sub-block.
            malformed_validity = 0
            # Maintenance-ledger audit rows (issue #113), gathered during this
            # SAME single pass over the corpus so the audit is nearly free (no
            # extra walk/query). Computed into a MaintenanceState after the loop.
            maintenance_rows: list[DocAuditRow] = []
            for pf in parsed:
                rel = pf.rel
                doc = pf.doc
                doc_id = pf.doc_id
                if pf.malformed_frontmatter:
                    malformed_frontmatter += 1
                    logger.warning(
                        "malformed front matter in %s: front-matter block present "
                        "but not valid YAML; status/supersedes/other fields were "
                        "dropped (staleness protection disabled for this doc)",
                        rel,
                    )
                if doc.validity_malformed:
                    malformed_validity += 1
                    logger.warning(
                        "malformed validity block in %s: 'validity' present but "
                        "one or more of its date fields did not parse; the whole "
                        "block was treated as absent for this doc",
                        rel,
                    )
                # The maintenance ledger audits the PHYSICAL corpus, so it records
                # the doc's real (pre-autofill) status: a legacy doc missing
                # `status` still counts as missing here, so the ledger keeps
                # nagging until an operator runs `migrate status --apply`, even
                # while virtual autofill (below) serves it as in-force.
                maintenance_rows.append(
                    DocAuditRow(
                        path=str(rel), id=doc_id, status=doc.status,
                        valid_until=doc.valid_until,
                        is_reserved=rel.name in RESERVED,
                    )
                )
                # Virtual status autofill (issue #147 / KNA-69): a legacy doc
                # missing `status` is indexed as `active` IN MEMORY only (this
                # SQLite column, never the markdown source), so a pre-0.4.0 corpus
                # keeps its in-force docs after upgrade. Gated on the per-index
                # ``_status_autofill`` flag; off restores the conservative
                # served-but-never-in-force behavior. A reserved filename
                # (index.md/log.md/template.md) is exempt from the status schema
                # requirement, so it is NOT autofilled (it never needed a status).
                indexed_status = doc.status
                if (
                    self._status_autofill
                    and not doc.status
                    and rel.name not in RESERVED
                ):
                    indexed_status = DEFAULT_AUTOFILL_STATUS
                path_tier, path_category = _classify_by_path(str(rel), path_rules)
                final_tier = doc.tier or path_tier
                final_category = doc.category or path_category
                tags_str = " ".join(doc.tags)
                applies_when_str = " ".join(doc.applies_when)
                last_modified, lm_source = self._resolve_last_modified(
                    kb_root, rel, git_mtimes,
                )
                content_markdown = pf.raw_text
                # Memory-inbox in-force floor (issue #109): derived from the
                # RELATIVE PATH via the single-sourced is_inbox_path, not from
                # final_category (a taxonomy override could reclassify the same
                # path under a different category and the floor must still hold).
                is_inbox = is_inbox_path(str(rel))
                conn.execute(
                    "INSERT INTO docs (id, path, tier, category, status, type, "
                    "applies_when, description, title, tags, "
                    "content_markdown, last_modified, last_modified_source, git_remote_url, "
                    "valid_from, valid_until, last_verified, recheck_by, verification_source, "
                    "is_inbox) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (doc_id, str(rel), final_tier, final_category, indexed_status, doc.doc_type,
                     applies_when_str, doc.description, doc.title, tags_str, content_markdown,
                     last_modified, lm_source, doc.git_remote_url,
                     doc.valid_from or None, doc.valid_until or None,
                     doc.last_verified or None, doc.recheck_by or None,
                     doc.verification_source or None, int(is_inbox)),
                )
                conn.execute(
                    "INSERT INTO fts (id, title, tags, applies_when, description, body) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (doc_id, doc.title, tags_str, applies_when_str, doc.description, doc.body),
                )
                # Secondary trigram index (issue #41), built into the SAME tmp DB
                # so it is swapped atomically with the primary fts table below.
                # A query never sees a half-built trigram table. Only populated
                # when the trigram fallback is enabled for this index (finding
                # (c)); the table itself was created above only in that case.
                if self.trigram_fallback:
                    conn.execute(
                        "INSERT INTO fts_trigram "
                        "(id, title, tags, applies_when, description, body) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (doc_id, doc.title, tags_str, applies_when_str,
                         doc.description, doc.body),
                    )
                if build_cooccurrence:
                    doc_token_sets.append(
                        tokenize_doc(doc.title, tags_str, doc.description, doc.body)
                    )
                # Lifecycle-relationship edges (issue #110 slice 1): extract both
                # directions of the supersession field pair plus `contradicts`
                # into (source_id, rel, target_id) rows, stored verbatim (a
                # dangling or malformed target is a `kb lint` concern, not an
                # index-build concern; the index just records what the front
                # matter declared).
                for target in doc.supersedes:
                    edge_rows.add((doc_id, "supersedes", target))
                if doc.superseded_by:
                    edge_rows.add((doc_id, "superseded_by", doc.superseded_by))
                for target in doc.contradicts:
                    edge_rows.add((doc_id, "contradicts", target))
                if build_embeddings:
                    # Embed title + applies_when + tags + description + body: the
                    # retrievable semantic content PLUS the curated intent
                    # vocabulary (finding (f)). applies_when and tags are exactly
                    # the hand-authored phrases a paraphrase query embeds near
                    # ("when do I ...", topic labels), so excluding them left the
                    # dense channel blind to the intent signal it most needs.
                    # Bounded to keep a huge doc from blowing the model's context;
                    # the head carries the topical signal.
                    embed_text = "\n".join(
                        p
                        for p in (
                            doc.title,
                            applies_when_str,
                            tags_str,
                            doc.description,
                            doc.body,
                        )
                        if p
                    )[:_EMBED_TEXT_MAX_CHARS]
                    embed_inputs.append((doc_id, embed_text))
                count += 1
            # Lifecycle-relationship edges (issue #110 slice 1): one batched
            # insert into the SAME tmp DB, so the edges table swaps atomically
            # with the rest of the index below.
            if edge_rows:
                conn.executemany(
                    "INSERT OR IGNORE INTO edges (source_id, rel, target_id) "
                    "VALUES (?, ?, ?)",
                    sorted(edge_rows),
                )
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
                    min_docs=int(params["min_docs"]),
                    max_doc_tokens=int(params["max_doc_tokens"]),
                )
                write_cooccurrence_table(conn, table)
            now = time.time()
            # Maintenance ledger (issue #113): computed from the SAME corpus
            # walk above, so the audit is nearly free. ``today`` is injectable
            # for deterministic tests; defaults to the real wall clock.
            maintenance_state = compute_maintenance_state(
                maintenance_rows,
                today=today if today is not None else today_iso(),
                ledger_path=self._maintenance_ledger_path,
                recently_expired_days=self._maintenance_recently_expired_days,
                expiring_soon_days=self._maintenance_expiring_soon_days,
            )
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
            # Malformed-frontmatter count (finding (j)): persisted in meta so
            # health() surfaces it without an extra scan and it survives a process
            # restart that reads the swapped index.
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) "
                "VALUES ('malformed_frontmatter', ?)",
                (str(malformed_frontmatter),),
            )
            # Malformed-validity count (issue #107): same rationale as above.
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) "
                "VALUES ('malformed_validity', ?)",
                (str(malformed_validity),),
            )
            # Maintenance-ledger state (issue #113): persisted as JSON so it
            # survives a process restart that reads the swapped index, same
            # rationale as the counters above.
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) "
                "VALUES ('maintenance_state', ?)",
                (json.dumps(maintenance_state.to_dict()),),
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
        # Publish the malformed-frontmatter count for the in-process health view
        # (finding (j)); the persisted meta row covers a fresh process.
        self._malformed_frontmatter_count = malformed_frontmatter
        self._malformed_validity_count = malformed_validity
        self._maintenance_state = maintenance_state
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
        in_force: bool = False,
        doc_type: str | None = None,
        include_expired: bool = False,
        validity_state: str | None = None,
        today: str | None = None,
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

        `status` filters to a single exact status. `in_force` is a HARD
        pre-ranking filter restricting results to the in-force status class
        (format.validate.IN_FORCE_STATUSES: active/accepted/approved) AND the
        validity window (not expired, not upcoming; see
        format.validate.is_in_force), so a superseded/deprecated/expired/
        upcoming doc is EXCLUDED before ranking rather than merely
        soft-downranked by the status reranker. The two compose: passing both a
        single `status` and `in_force=True` requires the doc match `status` AND
        be in-force (an empty result if `status` is not itself in-force). The
        filter is applied to both the FTS candidate pool and the dense
        (embedding) candidate source so neither leaks an out-of-force doc.

        `in_force=True` ALSO excludes any doc that is the TARGET of a
        `supersedes` edge whose SOURCE doc is itself in-force (issue #110
        slice 2; see format.validate.graph_excluded_ids_sql, the single
        definition shared with the `graph_excluded_docs` health counter). This
        closes the "forgotten status flip" gap: a doc superseded only via an
        edge, whose own `status` was never updated, is excluded from
        `in_force=True` results but stays visible in a default search (same as
        an upcoming doc). A mutually-supersessive in-force cycle is not
        special-cased: every member independently satisfies the rule and all
        are excluded. A dangling edge excludes nothing.

        `include_expired` (default False, issue #107): by default a doc past
        its `valid_until` is EXCLUDED from every result, not just from
        `in_force=True` queries (an expired doc has no named successor to
        outrank it, so left visible it could be the top hit and would govern).
        Set True to include expired docs anyway; each carries
        `freshness=expired` once shaped by `kb_search_fn`. A doc with a future
        `valid_from` ("upcoming") is NOT excluded by default search, only by
        `in_force=True`.

        `validity_state` (issue #107) is an audit-query facet, one of
        `"expired"`, `"stale"`, or `"expiring_within:N"` (N days). Filtering
        for `"expired"` implies including expired docs regardless of
        `include_expired`. Composes with `tier`/`category`/`status`/`doc_type`
        but not with `in_force` (an in-force query has its own window).

        `today` (ISO `YYYY-MM-DD`) drives every validity/freshness comparison
        above; defaults to `format.validate.today_iso()` (the real wall clock)
        but is injectable for deterministic tests.
        """
        today = today if today is not None else today_iso()
        # Stage 1 (expand-query): term extraction + optional expansion hook.
        # The user's ACTUAL terms (post-split, pre-expansion) drive the PRIMARY
        # match; any expansion terms are matched separately and can only backfill
        # BELOW the worst primary hit (see stage 2b'). FTS5 bm25 gives an
        # expansion synonym full idf weight in an OR-MATCH, so folding expansion
        # terms into the primary MATCH would let a doc matching only a derived
        # term outrank a doc matching the term the user typed. Splitting the
        # passes makes the "expansion is down-weighted" contract actually true.
        primary_terms = query.split()
        if not primary_terms:
            return []
        expansion_terms: list[str] = []
        if self.query_expander is not None:
            expanded = list(self.query_expander(primary_terms))
            if not expanded:
                return []
            # The expander keeps the originals first (order-stable), then appends
            # derived terms. Everything not in the original set is an expansion
            # term matched only in the penalized backfill pass.
            primary_lower = {t.lower() for t in primary_terms}
            seen_exp: set[str] = set()
            for t in expanded:
                low = t.lower()
                if low in primary_lower or low in seen_exp:
                    continue
                seen_exp.add(low)
                expansion_terms.append(t)
        weights = column_weights if column_weights is not None else _DEFAULT_BM25_WEIGHTS
        # Over-fetch a wider candidate pool when a reranker will reorder the
        # hits (see stage 3); never fewer than `limit`. Computed once and used to
        # bound both the primary pool and the backfill passes.
        candidate_limit = limit
        if self.reranker is not None:
            candidate_limit = max(
                limit,
                min(
                    max(limit * _RERANK_OVERFETCH_FACTOR, _RERANK_MIN_POOL),
                    _RERANK_MAX_POOL,
                ),
            )
        conn = self._connect()
        try:
            base_where, base_params = self._facet_filters(
                tier=tier, category=category, status=status,
                in_force=in_force, doc_type=doc_type, today=today,
                include_expired=include_expired, validity_state=validity_state,
            )
            # Stage 2 (match): PRIMARY pool from the user's own terms only.
            hits = self._fts_match(
                conn, primary_terms, columns=columns, weights=weights,
                base_where=base_where, base_params=base_params,
                candidate_limit=candidate_limit,
            )
            # Stage 2b' (penalized expansion backfill, finding (a)): match the
            # expansion terms separately and append ONLY docs not already found
            # by the primary pass, each scored strictly worse than the worst
            # primary hit. So an expansion-only hit can never outrank a doc that
            # matched a term the user actually typed, even after the reranker
            # re-sorts by score. This is the same rank-class backfill discipline
            # the trigram fallback uses.
            if expansion_terms:
                hits = self._expansion_backfill(
                    conn, expansion_terms, primary_hits=hits, columns=columns,
                    weights=weights, base_where=base_where,
                    base_params=base_params, candidate_limit=candidate_limit,
                )
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
                    base_where=base_where,
                    base_params=base_params,
                    candidate_limit=candidate_limit,
                )
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        # Stage 2c (dense candidate SOURCE, issue #42, reviewer concern 1). Only
        # when embeddings are configured for this index (guarded on the embedder
        # being present) do we ADD semantic neighbours to the pool; when off this
        # whole block is skipped and search() is byte-for-byte pure FTS. A
        # paraphrase with zero lexical overlap never entered the FTS pool above, so
        # without this the hybrid reranker could not help it. The dense hits are
        # UNIONED (dedup by id) into the pool; a dense-only hit (no bm25 score)
        # gets a neutral floor score (the worst bm25 in the pool, or 0.0 for an
        # empty pool) so the hybrid blend ranks it by its cosine component. The
        # reranker below then blends and truncates to ``limit``.
        if self._embeddings is not None:
            hits = self._union_dense_candidates(
                query,
                hits,
                dense_limit=self._dense_candidate_count,
                tier=tier,
                category=category,
                status=status,
                in_force=in_force,
                doc_type=doc_type,
                include_expired=include_expired,
                validity_state=validity_state,
                today=today,
            )
        # Stage 3 (re-rank): optional hook reorders/rescores (default identity).
        if self.reranker is not None:
            hits = list(self.reranker(query, hits))
        # Truncate to the caller's ``limit`` UNCONDITIONALLY (finding (h)). The
        # dense union and the backfill passes can push the pool past ``limit``,
        # and that truncation must happen whether or not a reranker is installed:
        # doing it only inside the reranker branch let search() return more than
        # ``limit`` hits when embeddings were configured but no reranker was set.
        final_hits = hits[:limit]
        # Stage 4 (surface, issue #110 slice 2): decorate the FINAL (already
        # truncated) hits with `superseded_by`, purely informational and
        # computed AFTER ranking/truncation since it has no ranking or
        # filtering effect (unconditional -- applied regardless of
        # ``in_force``, same as `freshness`, so a plain search still explains
        # why a hit is historically superseded).
        return self._attach_superseded_by(final_hits)

    def _attach_superseded_by(self, hits: list[SearchHit]) -> list[SearchHit]:
        if not hits:
            return hits
        conn = self._connect()
        try:
            by_id = _superseded_by_map(conn, [h.id for h in hits])
        except sqlite3.Error:
            return hits
        finally:
            conn.close()
        if not by_id:
            return hits
        return [
            replace(h, superseded_by=tuple(by_id[h.id])) if h.id in by_id else h
            for h in hits
        ]

    def _union_dense_candidates(
        self,
        query: str,
        fts_hits: builtins.list[SearchHit],
        *,
        dense_limit: int,
        tier: str | None,
        category: str | None,
        status: str | None,
        in_force: bool,
        doc_type: str | None,
        include_expired: bool = False,
        validity_state: str | None = None,
        today: str | None = None,
    ) -> builtins.list[SearchHit]:
        """Union dense (cosine) candidates into the FTS pool (reviewer concern 1).

        Dense hits already present in ``fts_hits`` (matched lexically too) are
        dropped so an FTS hit keeps its real bm25 score. A dense-ONLY hit has no
        bm25 signal, so it is given the worst (largest, since bm25 is ordered
        ascending) score in the current pool as a neutral floor; the hybrid
        reranker then decides its rank purely by its cosine component. With an
        empty FTS pool the floor is 0.0 (a finite, ordered anchor). Dense-only
        hits are APPENDED after the FTS hits; the reranker re-sorts the whole pool.
        """
        dense = self.dense_candidates(
            query,
            limit=dense_limit,
            min_cosine=self._dense_min_cosine,
            tier=tier,
            category=category,
            status=status,
            in_force=in_force,
            doc_type=doc_type,
            include_expired=include_expired,
            validity_state=validity_state,
            today=today,
        )
        if not dense:
            return fts_hits
        seen = {h.id for h in fts_hits}
        # Worst (largest) bm25 in the pool as the neutral floor for dense-only
        # hits; 0.0 when the FTS pool is empty so scores stay finite and ordered.
        floor = max((h.score for h in fts_hits), default=0.0)
        appended = [
            replace(h, score=floor) for h in dense if h.id not in seen
        ]
        return [*fts_hits, *appended]

    def _facet_filters(
        self,
        *,
        tier: str | None,
        category: str | None,
        status: str | None,
        in_force: bool,
        doc_type: str | None,
        today: str,
        include_expired: bool = False,
        validity_state: str | None = None,
    ) -> tuple[list[str], list[object]]:
        """Build the shared docs.* facet WHERE fragments (no MATCH clause).

        Returns (where_fragments, params) covering tier/category/status/
        in_force/doc_type/validity. The MATCH clause is prepended per-pass by
        the caller so the primary, expansion-backfill, trigram, and dense
        passes all apply the same facet filters from a single source.

        Validity/freshness (issue #107): ``in_force`` ANDs the status-class
        filter with the validity-WINDOW fragment (:func:`in_force_sql_fragment`
        from format.validate), so an in-force query also excludes expired and
        upcoming docs. It ALSO ANDs the memory-inbox floor
        (:func:`not_inbox_sql_fragment`, issue #109): a doc under the memory
        inbox is never in force regardless of claimed status, so it is excluded
        here rather than only downstream at response-shaping time (this is the
        hard filter every ``in_force=True`` caller -- kb_search, kb_consult,
        the dense candidate source -- shares).

        Independent of ``in_force``, ``validity_state`` (an
        explicit audit-query facet: ``"expired"``, ``"stale"``, or
        ``"expiring_within:N"``) takes over the validity filtering entirely and
        DISABLES the default not-expired exclusion below (an audit query for
        "what's expired" must not itself be filtered out by the very
        not-expired guard it's asking about). With no ``validity_state``, the
        default (``include_expired=False``) applies
        :func:`not_expired_sql_fragment` so a doc past ``valid_until`` never
        appears in an ordinary search; ``include_expired=True`` lifts that.

        Supersession-graph exclusion (issue #110 slice 2): ``in_force`` ALSO
        excludes any doc that is the TARGET of a `supersedes` edge whose
        SOURCE is itself in-force (:func:`graph_excluded_ids_sql` from
        format.validate is the single source of this rule, shared with the
        live `graph_excluded_docs` health counter, see
        :meth:`graph_excluded_count`). This is scoped to ``in_force`` only,
        exactly like the ``upcoming`` half of the validity window above: a
        graph-excluded doc stays visible in a plain (non-``in_force``) search,
        same as an upcoming doc does.
        """
        where: list[str] = []
        params: list[object] = []
        if tier:
            where.append("docs.tier = ?")
            params.append(tier)
        if category:
            where.append("docs.category = ?")
            params.append(category)
        if status:
            where.append("docs.status = ?")
            params.append(status)
        if in_force:
            placeholders = ", ".join("?" for _ in _IN_FORCE_STATUS_LIST)
            where.append(f"docs.status IN ({placeholders})")
            params.extend(_IN_FORCE_STATUS_LIST)
            where.append(in_force_sql_fragment())
            params.extend([today, today])
            # Memory-inbox floor (issue #109): no bind params, see docstring.
            where.append(not_inbox_sql_fragment())
            # Supersession-graph exclusion (issue #110 slice 2): composes with
            # the fragments above -- the full in-force predicate is status
            # class AND validity window AND not-inbox AND not-graph-excluded.
            where.append(f"docs.id NOT IN ({graph_excluded_ids_sql(placeholders)})")
            params.extend(_IN_FORCE_STATUS_LIST)
            params.extend([today, today])
        if doc_type:
            where.append("docs.type = ?")
            params.append(doc_type)
        if validity_state:
            kind, days = _parse_validity_state(validity_state)
            if kind == "expired":
                where.append("(docs.valid_until IS NOT NULL AND docs.valid_until < ?)")
                params.append(today)
            elif kind == "stale":
                where.append("(docs.recheck_by IS NOT NULL AND docs.recheck_by < ?)")
                params.append(today)
            elif kind == "expiring_within":
                cutoff = _add_days_iso(today, days or 0)
                where.append(
                    "(docs.valid_until IS NOT NULL AND docs.valid_until >= ? "
                    "AND docs.valid_until <= ?)"
                )
                params.extend([today, cutoff])
        elif not include_expired:
            where.append(not_expired_sql_fragment())
            params.append(today)
        return where, params

    def _fts_match(
        self,
        conn: sqlite3.Connection,
        terms: list[str],
        *,
        columns: list[str] | None,
        weights: tuple[float, ...],
        base_where: list[str],
        base_params: list[object],
        candidate_limit: int,
    ) -> list[SearchHit]:
        """Run one bm25-ordered FTS MATCH over ``terms`` and return the hits.

        The single primary-pool query, factored out so the primary pass and the
        penalized expansion-backfill pass share exactly one code path. Rows come
        back bm25-ordered (ascending; lower is better). Applies the shared facet
        filters (``base_where`` / ``base_params``).
        """
        match_query = self._build_match_expr(terms, columns)
        bm25_expr = "bm25(fts, " + ", ".join(repr(float(w)) for w in weights) + ")"
        where = ["fts MATCH ?", *base_where]
        params: list[object] = [match_query, *base_params, candidate_limit]
        sql = f"""
            SELECT
                fts.id AS id,
                docs.path AS path,
                COALESCE(docs.title, '') AS title,
                COALESCE(docs.status, '') AS status,
                COALESCE(docs.type, '') AS doc_type,
                COALESCE(docs.valid_from, '') AS valid_from,
                COALESCE(docs.valid_until, '') AS valid_until,
                COALESCE(docs.recheck_by, '') AS recheck_by,
                COALESCE(docs.is_inbox, 0) AS is_inbox,
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
                valid_from=r["valid_from"],
                valid_until=r["valid_until"],
                recheck_by=r["recheck_by"],
                is_inbox=bool(r["is_inbox"]),
            )
            for r in rows
        ]

    def _expansion_backfill(
        self,
        conn: sqlite3.Connection,
        expansion_terms: list[str],
        *,
        primary_hits: list[SearchHit],
        columns: list[str] | None,
        weights: tuple[float, ...],
        base_where: list[str],
        base_params: list[object],
        candidate_limit: int,
    ) -> list[SearchHit]:
        """Backfill expansion-term matches strictly below the primary hits (a).

        Runs a second FTS MATCH over the expansion terms only, drops any doc
        already returned by the primary pass, and appends the rest with scores
        strictly worse (larger, since bm25 is ordered ascending) than the worst
        primary hit. So a doc that matches ONLY a synonym / co-occurrence term can
        never outrank a doc that matched a term the user actually typed, even
        after the reranker re-sorts by score. This is the same rank-class discipline
        the trigram backfill uses. The expansion pass preserves its own internal
        bm25 order via a monotonically increasing offset.
        """
        expanded_hits = self._fts_match(
            conn, expansion_terms, columns=columns, weights=weights,
            base_where=base_where, base_params=base_params,
            candidate_limit=candidate_limit,
        )
        if not expanded_hits:
            return primary_hits
        seen_ids = {h.id for h in primary_hits}
        # Worst (largest) primary score; bm25 scores are <= 0, so 0.0 is a safe
        # finite floor when there are no primary hits.
        worst_primary = max((h.score for h in primary_hits), default=0.0)
        appended: list[SearchHit] = []
        offset = 1.0
        for h in expanded_hits:
            if h.id in seen_ids:
                continue
            seen_ids.add(h.id)
            appended.append(
                replace(
                    h, score=worst_primary + offset,
                    rank_class=RANK_CLASS_BACKFILL,
                )
            )
            offset += 1.0
        return [*primary_hits, *appended]

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
        results AFTER the primary hits. Each appended hit carries
        ``rank_class=RANK_CLASS_BACKFILL`` AND a score strictly worse (larger,
        since bm25 is ordered ascending) than any primary hit. The rank_class is
        the OUTER sort key in every reranker, so a fuzzy hit can never be lifted
        above an exact/primary one even by a reranker that ADDS status deltas or
        RE-NORMALISES scores (finding (d)); the worse score just orders the fuzzy
        hits among themselves. The same tier/category/status/doc_type filters
        (``base_where`` / ``base_params``) are applied. A query with no trigram
        (shorter than 3 chars) no-ops and the primary hits are returned unchanged.
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
                COALESCE(docs.valid_from, '') AS valid_from,
                COALESCE(docs.valid_until, '') AS valid_until,
                COALESCE(docs.recheck_by, '') AS recheck_by,
                COALESCE(docs.is_inbox, 0) AS is_inbox,
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
                    rank_class=RANK_CLASS_BACKFILL,
                    valid_from=r["valid_from"],
                    valid_until=r["valid_until"],
                    recheck_by=r["recheck_by"],
                    is_inbox=bool(r["is_inbox"]),
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
        (down-weighted by search()'s separate penalized backfill pass, not by
        appended position), bounded overall by ``max_terms``. Bound to
        ``self.related_terms`` so it always reads the currently-swapped index.
        Compose it AFTER the synonym expander via ``cooccurrence.compose_expanders``.
        """
        return make_cooccurrence_expander(
            lambda term, kk: self.related_terms(term, limit=kk),
            k=k,
            max_terms=max_terms,
        )

    def _load_vectors(self) -> dict[str, builtins.list[float]]:
        """Return the id->vector matrix for the current build, loaded once (g).

        Batch-fetches and deserializes the whole ``doc_vectors`` table in ONE
        query and caches it, keyed by the db file's (size, mtime_ns). The atomic
        ``os.replace`` swap in build() gives the new file a different mtime, so a
        rebuild (even by a different Index object) transparently invalidates the
        cache with no explicit call. Replaces the old per-hit ``get_vector`` SQL
        (one connection per candidate) and the per-query full-table re-read in
        ``dense_candidates``. Returns an empty dict when the db or the table is
        absent (index predates schema v8) or the feature is off.
        """
        if not self._db_path.exists():
            return {}
        try:
            st = self._db_path.stat()
            key = (st.st_size, st.st_mtime_ns)
        except OSError:
            return {}
        with self._vector_lock:
            cached = self._vector_cache
            if cached is not None and cached[0] == key:
                return cached[1]
        # Cache miss: load the whole table once, outside the lock (a concurrent
        # miss is harmless; both compute the same matrix from the same file).
        conn = self._connect()
        try:
            rows = conn.execute("SELECT id, vector FROM doc_vectors").fetchall()
        except sqlite3.OperationalError:
            # Table absent (index predates schema v8); no vectors.
            rows = []
        finally:
            conn.close()
        matrix = {r["id"]: deserialize_vector(r["vector"]) for r in rows}
        self._vector_loads += 1
        with self._vector_lock:
            self._vector_cache = (key, matrix)
        return matrix

    def get_vector(self, id: str) -> builtins.list[float] | None:
        """Return the stored embedding vector for ``id``, or None (issue #42).

        Backed by the per-build id->vector matrix (finding (g)): the matrix is
        loaded from ``doc_vectors`` ONCE per build and cached, so the hybrid
        reranker's per-candidate ``get_vector`` calls are memory-only lookups
        instead of one SQLite connection each. A build that predates the table, a
        doc with no vector, or the feature being off all yield None (the hybrid
        reranker treats a missing vector as a neutral cosine, never a drop).
        """
        return self._load_vectors().get(id)

    def dense_candidates(
        self,
        query: str,
        *,
        limit: int,
        min_cosine: float,
        tier: str | None = None,
        category: str | None = None,
        status: str | None = None,
        in_force: bool = False,
        doc_type: str | None = None,
        include_expired: bool = False,
        validity_state: str | None = None,
        today: str | None = None,
    ) -> builtins.list[SearchHit]:
        """Return up to ``limit`` docs most similar to ``query`` by cosine (issue #42).

        This is the semantic candidate SOURCE that reviewer concern 1 requires: a
        paraphrase with zero lexical overlap never enters the FTS pool, so search()
        unions these dense hits in before the reranker runs. Reuses ``cosine`` over
        the stored ``doc_vectors`` (read via the same deserialize path as
        ``get_vector``). Only neighbours clearing ``min_cosine`` are returned, so a
        negative / out-of-scope query whose nearest doc is only weakly similar pulls
        in nothing (abstention guard). The same tier/category/status/doc_type/
        validity filters are applied (via the SAME :meth:`_facet_filters` the FTS
        pool uses, issue #107) so a filtered search does not leak an off-facet or
        expired doc through the dense channel.

        Returns the empty list when embeddings are not configured, the query cannot
        be embedded, or the index predates the ``doc_vectors`` table. Each hit
        carries its cosine in ``score`` (higher = better here); search() re-scores
        dense-only hits onto the bm25 convention before the blend.

        Perf (finding (g)): the vectors come from the per-build id->vector matrix
        (loaded once, cached) instead of re-reading and re-deserializing the whole
        ``doc_vectors`` table on every query. Only the facet-eligible doc metadata
        is queried here; the vector lookup is an in-memory dict hit.
        """
        embedder = self._resolve_embedder()
        if embedder is None or limit <= 0 or not self._db_path.exists():
            return []
        qvec = embedder.embed_one(query) if query.strip() else None
        if not qvec:
            return []
        matrix = self._load_vectors()
        if not matrix:
            return []
        today = today if today is not None else today_iso()
        # Fetch only the facet-eligible doc metadata (no vector blob); the vector
        # is pulled from the cached matrix by id. Shares the exact same
        # tier/category/status/in_force/doc_type/validity WHERE-building as the
        # FTS pool (single-sourced, issue #107) so the two filter sites cannot
        # drift apart.
        facet_where, facet_params = self._facet_filters(
            tier=tier, category=category, status=status,
            in_force=in_force, doc_type=doc_type, today=today,
            include_expired=include_expired, validity_state=validity_state,
        )
        where = ["dv.id IS NOT NULL", *facet_where]
        params: list[object] = list(facet_params)
        sql = f"""
            SELECT
                dv.id AS id,
                docs.path AS path,
                COALESCE(docs.title, '') AS title,
                COALESCE(docs.status, '') AS status,
                COALESCE(docs.type, '') AS doc_type,
                COALESCE(docs.description, '') AS description,
                COALESCE(docs.valid_from, '') AS valid_from,
                COALESCE(docs.valid_until, '') AS valid_until,
                COALESCE(docs.recheck_by, '') AS recheck_by,
                COALESCE(docs.is_inbox, 0) AS is_inbox
            FROM doc_vectors dv
            JOIN docs ON docs.id = dv.id
            WHERE {' AND '.join(where)}
        """
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # doc_vectors table absent (index predates schema v8): no dense source.
            return []
        finally:
            conn.close()
        scored: list[tuple[float, SearchHit]] = []
        for r in rows:
            dvec = matrix.get(r["id"])
            if dvec is None or len(dvec) != len(qvec):
                continue
            sim = cosine(qvec, dvec)
            if sim < min_cosine:
                continue
            scored.append(
                (
                    sim,
                    SearchHit(
                        id=r["id"],
                        path=r["path"],
                        title=r["title"],
                        snippet=r["description"],
                        score=sim,
                        status=r["status"],
                        doc_type=r["doc_type"],
                        valid_from=r["valid_from"],
                        valid_until=r["valid_until"],
                        recheck_by=r["recheck_by"],
                        is_inbox=bool(r["is_inbox"]),
                    ),
                )
            )
        # Strongest cosine first, then truncate to ``limit``.
        scored.sort(key=lambda t: -t[0])
        return [hit for _sim, hit in scored[:limit]]

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
                       last_modified, last_modified_source, git_remote_url,
                       valid_from, valid_until, last_verified, recheck_by,
                       verification_source, is_inbox
                FROM docs WHERE id = ?
                """,
                (id,),
            ).fetchone()
            if row is None:
                return None
            try:
                commit_row = conn.execute(
                    "SELECT value FROM meta WHERE key='source_commit'"
                ).fetchone()
                source_commit = commit_row[0] if commit_row else ""
            except sqlite3.Error:
                source_commit = ""
            # Lifecycle-relationship surfacing (issue #110 slice 2): computed
            # from the `edges` table so "retirement is explainable" -- see
            # _superseded_by_map / _edges_from / _edges_targeting.
            try:
                superseded_by = _superseded_by_map(conn, [id]).get(id, [])
                contradicts = _edges_from(conn, "contradicts", [id]).get(id, [])
                contradicted_by = _edges_targeting(conn, "contradicts", [id]).get(
                    id, []
                )
            except sqlite3.Error:
                superseded_by, contradicts, contradicted_by = [], [], []
        except sqlite3.Error:
            return None
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
            valid_from=row["valid_from"] or "",
            valid_until=row["valid_until"] or "",
            last_verified=row["last_verified"] or "",
            recheck_by=row["recheck_by"] or "",
            verification_source=row["verification_source"] or "",
            is_inbox=bool(row["is_inbox"]),
            superseded_by=tuple(superseded_by),
            contradicts=tuple(contradicts),
            contradicted_by=tuple(contradicted_by),
        )

    def id_to_path_map(self) -> dict[str, str]:
        """Return ``{doc_id: path}`` for every indexed document.

        Used by the write-path validation gate to detect a forged/duplicate id
        (an id already used by a DIFFERENT path corrupts the next rebuild). This
        includes reserved files (index.md/log.md), which the indexer still assigns
        an id (explicit or path-derived), so a reserved file carrying a colliding
        id is caught too."""
        conn = self._connect()
        try:
            rows = conn.execute("SELECT id, path FROM docs").fetchall()
        except sqlite3.Error:
            return {}
        finally:
            conn.close()
        return {row["id"]: row["path"] for row in rows}

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
        except sqlite3.Error:
            return set()
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
        except sqlite3.Error:
            return []
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
        except sqlite3.Error:
            return []
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
        except sqlite3.Error:
            return {
                "source_commit": "",
                "index_built_at": None,
                "total_docs": 0,
                "db_size_bytes": self._db_path.stat().st_size,
            }
        finally:
            conn.close()
        meta = {r["key"]: r["value"] for r in rows}
        return {
            "source_commit": meta.get("source_commit", ""),
            "index_built_at": float(meta["built_at"]) if "built_at" in meta else None,
            "total_docs": int(meta.get("total_docs", "0")),
            "db_size_bytes": self._db_path.stat().st_size,
            # Finding (j): present-but-malformed front-matter count from the last
            # build. Read from the persisted meta row so a fresh process (that did
            # not run build() itself) still sees it.
            "malformed_frontmatter": int(meta.get("malformed_frontmatter", "0")),
            # Issue #107: present-but-malformed ``validity`` block count, same
            # persisted-meta rationale as malformed_frontmatter above.
            "malformed_validity": int(meta.get("malformed_validity", "0")),
            # Issue #110 slice 2: docs CURRENTLY excluded from in-force
            # retrieval by the supersession-graph rule. Computed LIVE (not
            # from a persisted build-time meta row) because the in-force-
            # source guard is wall-clock-relative: a source's validity window
            # opening/closing between rebuilds changes retrieval per query,
            # and a frozen counter would drift from the filter it reports on
            # (codex review blocker). The health cache bounds staleness to
            # ``health_ttl_sec`` (default 5s).
            "graph_excluded_docs": self.graph_excluded_count(),
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
        except sqlite3.Error:
            return []
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
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        return [{
            "id": r["id"], "path": r["path"], "tier": r["tier"] or "",
            "git_remote_url": r["git_remote_url"],
        } for r in rows]
