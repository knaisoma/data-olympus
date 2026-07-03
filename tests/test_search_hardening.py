"""Search pipeline hardening (WP2b, 0.3.0).

One test per audit finding letter that is behaviour-observable at the Index level:

- (a) penalized expansion backfill: an expansion-only hit never outranks a
  primary-term hit, and the rank_class invariant survives the status reranker.
- (d) rank-class invariant: a backfill hit stays below primaries even when its
  status would otherwise boost it above them.
- (f) dense text includes applies_when + tags.
- (g) vectors are batch-fetched once per build (no per-hit connection).
- (h) search() never returns more than ``limit`` hits, incl. the embeddings-on /
  no-reranker path that used to over-return.
- (i) single git-log pass produces identical per-file commit metadata.
- (j) malformed front matter is counted and health-visible.

Findings (b) and (c) are covered in test_cooccurrence.py / test_trigram.py.
"""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from data_olympus.index import (
    RANK_CLASS_BACKFILL,
    RANK_CLASS_PRIMARY,
    Index,
    SearchHit,
    make_status_reranker,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write(kb: Path, name: str, body: str, *, front: str = "") -> None:
    (kb / name).write_text(f"{front}{body}\n", encoding="utf-8")


# --- (a) + (d) penalized expansion backfill ----------------------------------


def _expansion_kb(kb: Path) -> None:
    """A doc matching only the user's term and a doc matching only an expansion
    term, so we can prove the expansion-only doc never outranks the primary."""
    # PRIMARY doc: matches "alpha" (the user's term).
    _write(kb, "primary.md", "alpha appears here as the primary term.",
           front="---\nid: DOC-PRIMARY\n---\n")
    # EXPANSION-ONLY doc: matches only "beta" (a synonym the expander adds), and
    # matches it MANY times so its raw bm25 would beat the single-mention primary
    # if the terms were folded into one MATCH.
    _write(kb, "expansion.md",
           "beta beta beta beta beta beta beta beta beta beta beta beta.",
           front="---\nid: DOC-EXPANSION\n---\n")


def test_expansion_only_hit_never_outranks_primary(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    """Finding (a): a doc matching ONLY an expansion (synonym) term must rank
    strictly below every doc matching the user's actual term, even though the
    expansion doc's raw bm25 (many mentions) would win a single OR-MATCH."""
    kb = tmp_path / "kb"
    kb.mkdir()
    _expansion_kb(kb)

    def expander(terms: list[str]) -> list[str]:
        # Emulate the real expander contract: originals first, then a derived
        # synonym "beta" for "alpha".
        return [*terms, "beta"] if "alpha" in terms else terms

    idx = Index(tmp_index_path, query_expander=expander)
    idx.build(kb, source_commit="x")
    hits = idx.search("alpha", limit=20)
    ids = [h.id for h in hits]
    # Both docs are reachable (recall broadened)...
    assert "DOC-PRIMARY" in ids
    assert "DOC-EXPANSION" in ids
    # ...but the expansion-only doc is strictly after the primary one.
    assert ids.index("DOC-PRIMARY") < ids.index("DOC-EXPANSION"), (
        "expansion-only hit must not outrank the primary-term hit (finding a)"
    )
    # The expansion doc carries the backfill rank class; the primary does not.
    by_id = {h.id: h for h in hits}
    assert by_id["DOC-PRIMARY"].rank_class == RANK_CLASS_PRIMARY
    assert by_id["DOC-EXPANSION"].rank_class == RANK_CLASS_BACKFILL


def test_expansion_backfill_survives_status_reranker(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    """Finding (d): even when the expansion-only doc is ``active`` (status boost)
    and the primary doc is ``superseded`` (status penalty), the rank_class outer
    key keeps the expansion hit below the primary. A score-only floor would fail
    here because the status deltas span more than the +1.0 backfill spacing."""
    kb = tmp_path / "kb"
    kb.mkdir()
    _write(kb, "primary.md", "alpha primary content here.",
           front="---\nid: DOC-PRIMARY\nstatus: superseded\n---\n")
    _write(kb, "expansion.md", "beta beta beta beta beta beta.",
           front="---\nid: DOC-EXPANSION\nstatus: active\n---\n")

    def expander(terms: list[str]) -> list[str]:
        return [*terms, "beta"] if "alpha" in terms else terms

    idx = Index(
        tmp_index_path,
        query_expander=expander,
        reranker=make_status_reranker(),
    )
    idx.build(kb, source_commit="x")
    ids = [h.id for h in idx.search("alpha", limit=20)]
    assert ids.index("DOC-PRIMARY") < ids.index("DOC-EXPANSION"), (
        "an active-status expansion hit must still rank below a superseded "
        "primary hit (rank-class invariant, finding d)"
    )


def test_status_reranker_orders_by_rank_class_first() -> None:
    """Finding (d) unit: the status reranker sorts by (rank_class, score), so a
    backfill hit cannot be lifted above a primary regardless of its status."""
    reranker = make_status_reranker()
    hits = [
        SearchHit(id="prim", path="p", title="", snippet="", score=-1.0,
                  status="superseded", rank_class=RANK_CLASS_PRIMARY),
        SearchHit(id="back", path="b", title="", snippet="", score=1.0,
                  status="active", rank_class=RANK_CLASS_BACKFILL),
    ]
    ordered = [h.id for h in reranker("q", hits)]
    assert ordered == ["prim", "back"]


def test_hybrid_reranker_orders_by_rank_class_first() -> None:
    """Finding (d) unit for the HYBRID path: a backfill hit with a PERFECT cosine
    match must still rank below a primary hit with a WEAK cosine, even at
    weight=1.0 (pure semantic). Without the rank_class outer sort key the cosine
    blend would lift the backfill hit to the top."""
    from data_olympus.embeddings import make_hybrid_reranker

    qvec = [1.0, 0.0]
    vectors = {
        "prim": [0.0, 1.0],  # orthogonal to the query -> weak cosine
        "back": [1.0, 0.0],  # identical to the query -> perfect cosine
    }
    hits = [
        SearchHit(id="prim", path="p", title="", snippet="", score=-1.0,
                  rank_class=RANK_CLASS_PRIMARY),
        SearchHit(id="back", path="b", title="", snippet="", score=1.0,
                  rank_class=RANK_CLASS_BACKFILL),
    ]
    reranker = make_hybrid_reranker(
        embed_query=lambda _q: qvec,
        get_vector=vectors.get,
        weight=1.0,  # pure cosine: back would win without the rank_class key
    )
    ordered = [h.id for h in reranker("q", hits)]
    assert ordered == ["prim", "back"], (
        "a perfect-cosine backfill hit must not outrank a weak-cosine primary "
        "(hybrid rank-class invariant, finding d)"
    )


# --- (f) dense text includes applies_when + tags -----------------------------


def test_embed_text_includes_applies_when_and_tags(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    """Finding (f): the text embedded at build time must include applies_when and
    tags (the curated intent vocabulary), not just title/description/body."""
    kb = tmp_path / "kb"
    kb.mkdir()
    _write(
        kb, "doc.md", "Body about caching.",
        front=(
            "---\nid: DOC-1\ntitle: Cache Policy\n"
            "tags: [uniquetagtoken]\n"
            "applies_when: [uniqueappliestoken]\n"
            "description: A description.\n---\n"
        ),
    )

    captured: list[str] = []

    class _RecordingEmbedder:
        model_name = "recording"

        def embed_many(self, texts: list[str]) -> list[list[float]]:
            captured.extend(texts)
            return [[1.0, 0.0, 0.0] for _ in texts]

        def embed_one(self, text: str) -> list[float]:  # noqa: ARG002
            return [1.0, 0.0, 0.0]

    from data_olympus.embeddings import EmbeddingsConfig

    idx = Index(
        tmp_index_path,
        embeddings=EmbeddingsConfig(model_name="recording", weight=0.5),
        embedder=_RecordingEmbedder(),  # type: ignore[arg-type]
    )
    idx.build(kb, source_commit="x")
    assert captured, "embedder.embed_many must have been called at build time"
    embedded = captured[0]
    assert "uniqueappliestoken" in embedded, (
        "embedded text must include applies_when (finding f)"
    )
    assert "uniquetagtoken" in embedded, (
        "embedded text must include tags (finding f)"
    )


# --- (g) vectors batch-fetched once per build --------------------------------


class _CountingEmbedder:
    model_name = "counting"

    def __init__(self) -> None:
        self.embed_one_calls = 0

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        # Distinct unit vectors so cosine ordering is well-defined.
        return [[1.0, 0.0] for _ in texts]

    def embed_one(self, text: str) -> list[float]:  # noqa: ARG002
        self.embed_one_calls += 1
        return [1.0, 0.0]


def test_vectors_loaded_once_per_build_not_per_hit(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    """Finding (g): the id->vector matrix is loaded from SQLite ONCE per build,
    shared across every candidate hit, instead of one connection per get_vector.
    Asserted via the ``_vector_loads`` counter across a multi-hit hybrid search."""
    from data_olympus.embeddings import EmbeddingsConfig

    kb = tmp_path / "kb"
    kb.mkdir()
    for i in range(6):
        _write(kb, f"d{i}.md", f"shared caching topic document number {i}.",
               front=f"---\nid: DOC-{i}\n---\n")

    embedder = _CountingEmbedder()
    idx = Index(
        tmp_index_path,
        embeddings=EmbeddingsConfig(model_name="counting", weight=0.5),
        embedder=embedder,  # type: ignore[arg-type]
    )
    idx.build(kb, source_commit="x")
    idx.reranker = idx.make_hybrid_reranker(embedder, weight=0.5)  # type: ignore[arg-type]

    idx._vector_loads = 0  # reset after any build-time access
    hits = idx.search("caching", limit=20)
    assert len(hits) >= 3, "need several candidate hits to exercise get_vector"
    # One query -> at most ONE matrix load, no matter how many candidate hits the
    # hybrid reranker scored. The old per-hit get_vector opened one connection
    # each; this proves that is gone.
    assert idx._vector_loads <= 1, (
        f"vector matrix must load at most once per query; "
        f"loaded {idx._vector_loads} times"
    )


def test_vector_cache_invalidated_on_rebuild(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    """Finding (g): the (size, mtime)-keyed cache invalidates on the atomic swap,
    so a rebuilt index is not served stale vectors."""
    from data_olympus.embeddings import EmbeddingsConfig

    kb = tmp_path / "kb"
    kb.mkdir()
    _write(kb, "a.md", "alpha document.", front="---\nid: DOC-A\n---\n")

    embedder = _CountingEmbedder()
    idx = Index(
        tmp_index_path,
        embeddings=EmbeddingsConfig(model_name="counting", weight=0.5),
        embedder=embedder,  # type: ignore[arg-type]
    )
    idx.build(kb, source_commit="first")
    assert idx.get_vector("DOC-A") is not None
    loads_after_first = idx._vector_loads

    # Add a doc and rebuild; the new file has a fresh mtime so the cache key
    # changes and the matrix reloads.
    _write(kb, "b.md", "beta document.", front="---\nid: DOC-B\n---\n")
    idx.build(kb, source_commit="second")
    assert idx.get_vector("DOC-B") is not None, "new doc's vector must be visible"
    assert idx._vector_loads > loads_after_first, (
        "the rebuild must invalidate the vector cache (finding g)"
    )


# --- (h) >limit regression ---------------------------------------------------


def test_search_never_exceeds_limit_embeddings_on_no_reranker(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    """Finding (h): with embeddings configured but NO reranker set, the dense
    union could push the pool past ``limit`` and search() returned all of them,
    because truncation lived only inside the reranker branch. It must always
    truncate to ``limit``."""
    from data_olympus.embeddings import EmbeddingsConfig

    kb = tmp_path / "kb"
    kb.mkdir()
    for i in range(8):
        _write(kb, f"d{i}.md", f"caching topic document {i}.",
               front=f"---\nid: DOC-{i}\n---\n")

    embedder = _CountingEmbedder()
    idx = Index(
        tmp_index_path,
        embeddings=EmbeddingsConfig(model_name="counting", weight=0.5),
        embedder=embedder,  # type: ignore[arg-type]
        dense_min_cosine=-1.0,  # accept every neighbour so the union is large
        dense_candidate_count=8,
    )
    # No reranker set on purpose.
    assert idx.reranker is None
    idx.build(kb, source_commit="x")
    hits = idx.search("caching", limit=3)
    assert len(hits) <= 3, (
        f"search() must never return more than limit hits; got {len(hits)}"
    )


# --- (i) single git-log pass preserves per-file metadata ---------------------


def test_single_git_pass_matches_per_file_metadata(
    tmp_git_kb: Path, tmp_path: Path,
) -> None:
    """Finding (i): the batched ``git log --name-only`` pass yields the same
    (last_modified, source) per file as the old one-subprocess-per-file path."""
    kb = tmp_git_kb
    # Reference: per-file git query for each indexed file.
    idx = Index(tmp_path / "ref.db")
    idx.build(kb, source_commit="x")

    conn = sqlite3.connect(tmp_path / "ref.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT path, last_modified, last_modified_source FROM docs ORDER BY path"
    ).fetchall()
    conn.close()
    assert rows, "fixture must index at least one git-tracked file"

    from pathlib import Path as _Path

    for r in rows:
        rel = _Path(r["path"])
        expected_iso, expected_src = Index._git_last_modified(kb, rel)
        assert r["last_modified_source"] == "git", (
            f"{r['path']} should be sourced from git"
        )
        assert expected_src == "git"
        assert r["last_modified"] == expected_iso, (
            f"batched git timestamp for {r['path']} must equal the per-file "
            f"query result: {r['last_modified']!r} != {expected_iso!r}"
        )


def test_build_falls_back_to_mtime_without_git(
    tmp_kb: Path, tmp_index_path: Path,
) -> None:
    """Finding (i): a non-git KB (empty batched map) still records 'mtime' as the
    source, unchanged from the per-file fallback."""
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")  # tmp_kb is NOT a git repo
    conn = sqlite3.connect(tmp_index_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT last_modified_source FROM docs").fetchall()
    conn.close()
    assert rows
    assert all(r["last_modified_source"] == "mtime" for r in rows), (
        "a non-git KB must fall back to filesystem mtime (finding i)"
    )


def test_git_last_modified_map_empty_for_non_repo(tmp_kb: Path) -> None:
    """The batched git map is empty when the directory is not a git repo."""
    assert Index._git_last_modified_map(tmp_kb) == {}


def test_single_git_pass_handles_special_char_paths(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    """Finding (i) exactness: a filename with a space (and a unicode char) must
    round-trip through the NUL-delimited / quotePath=false batched git pass and
    still resolve to 'git', not fall back to mtime."""
    import subprocess

    kb = tmp_path / "kb"
    kb.mkdir()
    _write(kb, "plain.md", "plain body.", front="---\nid: DOC-PLAIN\n---\n")
    # A space and a non-ASCII char in the filename: the exact cases git would
    # octal-quote (and a naive splitlines/strip parser would mangle or drop).
    _write(kb, "with spacé.md", "spaced body.",
           front="---\nid: DOC-SPACED\n---\n")
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    }
    subprocess.run(["git", "init", "-q", "--initial-branch=main"],
                   cwd=kb, check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=kb, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=kb, check=True, env=env)

    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    conn = sqlite3.connect(tmp_index_path)
    conn.row_factory = sqlite3.Row
    rows = {
        r["path"]: r["last_modified_source"]
        for r in conn.execute(
            "SELECT path, last_modified_source FROM docs"
        ).fetchall()
    }
    conn.close()
    assert rows.get("with spacé.md") == "git", (
        "a space/unicode filename must resolve via git, not fall back to mtime "
        f"(finding i exactness); got {rows.get('with spacé.md')!r}"
    )
    assert rows.get("plain.md") == "git"


# --- (j) malformed front matter counter --------------------------------------


def test_malformed_frontmatter_counted_and_health_visible(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    """Finding (j): a doc whose front-matter block is present but is invalid YAML
    (so status/supersedes are silently dropped) is counted and surfaced on the
    Index and in health()."""
    kb = tmp_path / "kb"
    kb.mkdir()
    # Valid doc.
    _write(kb, "good.md", "Body.", front="---\nid: DOC-GOOD\nstatus: active\n---\n")
    # Malformed: a tab in YAML indentation is invalid, and the ':' with no space
    # plus broken structure makes safe_load raise -> lenient fallback drops status.
    (kb / "bad.md").write_text(
        "---\nid: DOC-BAD\nstatus: active\n\tbroken:\t: [unclosed\n---\nBody.\n",
        encoding="utf-8",
    )
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    assert idx.malformed_frontmatter_count == 1, (
        f"exactly one malformed doc expected; got {idx.malformed_frontmatter_count}"
    )
    health = idx.health()
    assert health["malformed_frontmatter"] == 1, (
        "health() must surface the malformed-frontmatter count (finding j)"
    )


def test_no_frontmatter_is_not_counted_malformed(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    """A doc with NO front matter at all (does not open with ---) is a normal,
    valid case and must NOT be counted as malformed."""
    kb = tmp_path / "kb"
    kb.mkdir()
    _write(kb, "plain.md", "# Just a heading\n\nNo front matter here.")
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    assert idx.malformed_frontmatter_count == 0
