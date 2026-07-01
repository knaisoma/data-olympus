"""Tests for the exact-id / exact-tag short-circuit reranker (issue #39).

Covers:
- detection helpers (looks_like_id / looks_like_tag) are conservative.
- Index.ids_with_exact_tag matches whole tags, not substrings.
- an exact-id query returns that doc as the top hit, even when it is absent
  from the FTS hit list.
- an exact-tag query favours docs carrying that tag.
- ordinary multi-term queries are unaffected.
- the reranker composes with an inner reranker.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from data_olympus.index import Index, SearchHit
from data_olympus.search_shortcut import (
    looks_like_id,
    looks_like_tag,
    make_id_tag_reranker,
)

if TYPE_CHECKING:
    from pathlib import Path


# --- detection helpers -------------------------------------------------------


def test_looks_like_id_accepts_kb_and_path_ids() -> None:
    assert looks_like_id("STD-U-002") == "STD-U-002"
    assert looks_like_id("DEC-001") == "DEC-001"
    assert looks_like_id("ADR-014") == "ADR-014"
    assert looks_like_id("tooling-AGENTS") == "tooling-AGENTS"
    assert looks_like_id("projects-example-project-README") == (
        "projects-example-project-README"
    )
    assert looks_like_id("  STD-U-002  ") == "STD-U-002"  # trims


def test_looks_like_id_rejects_words_and_phrases() -> None:
    assert looks_like_id("caching") is None  # no separator
    assert looks_like_id("worktree") is None
    assert looks_like_id("code review guide") is None  # multi-token
    assert looks_like_id("STD-U-002 style") is None  # id + word is not a bare id
    assert looks_like_id("") is None


def test_looks_like_tag_accepts_single_token_only() -> None:
    assert looks_like_tag("style") == "style"
    assert looks_like_tag("backend-nestjs") == "backend-nestjs"
    assert looks_like_tag("two words") is None
    assert looks_like_tag("") is None


# --- Index.ids_with_exact_tag ------------------------------------------------


def test_ids_with_exact_tag_matches_whole_tag(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    # STD-U-001 has tags [policy, test]; STD-U-002 has [style].
    assert idx.ids_with_exact_tag("style") == {"STD-U-002"}
    assert idx.ids_with_exact_tag("policy") == {"STD-U-001"}
    # 'test' is a whole tag on STD-U-001; must not also match via substring of
    # some other value.
    assert idx.ids_with_exact_tag("test") == {"STD-U-001"}


def test_ids_with_exact_tag_no_substring_match(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")
    # 'styl' is a substring of the 'style' tag but not a whole tag.
    assert idx.ids_with_exact_tag("styl") == set()
    assert idx.ids_with_exact_tag("") == set()


def test_ids_with_exact_tag_escapes_like_wildcards(
    tmp_kb: Path, tmp_index_path: Path
) -> None:
    """Tags carrying LIKE metacharacters ('_', '%') resolve to exact matches.

    The stored ``tags`` column is space-joined, so a tag may legitimately carry
    '_' or '%'. Final correctness comes from the split-recheck, but the LIKE
    pre-filter now escapes '_'/'%' (ESCAPE '\\') so it stays a literal-substring
    filter instead of treating '_' as 'any single char' and '%' as 'any run of
    chars' (which would pre-select 'beXend' for 'be_end', or every doc for '%').
    This pins the observable contract: (a) a genuine exact tag with '_'/'%' still
    matches, and (b) a near-miss differing only at the metacharacter position
    does NOT appear in the result.
    """
    foundation = tmp_kb / "universal" / "foundation"
    # Target doc: exact tag 'be_end'.
    (foundation / "STD-U-100-underscore.md").write_text(
        "---\nid: STD-U-100\ntier: T1\ncategory: foundation\ntags: [be_end]\n"
        "title: Underscore Tag\n---\n# STD-U-100\n\nBody.\n"
    )
    # Near-miss doc: tag 'beXend' would match '%be_end%' only if '_' were a
    # wildcard. Also carries a '100%' tag to exercise the '%' escape.
    (foundation / "STD-U-101-nearmiss.md").write_text(
        "---\nid: STD-U-101\ntier: T1\ncategory: foundation\ntags: [beXend, 100%]\n"
        "title: Near Miss Tag\n---\n# STD-U-101\n\nBody.\n"
    )
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="x")

    # (a) genuine exact tags containing metacharacters still match.
    assert idx.ids_with_exact_tag("be_end") == {"STD-U-100"}
    assert idx.ids_with_exact_tag("100%") == {"STD-U-101"}
    # (b) '_' is literal: 'beXend' is not the queried tag 'be_end'.
    assert "STD-U-101" not in idx.ids_with_exact_tag("be_end")
    # (c) a bare '%' must not act as a wildcard selecting every doc.
    assert idx.ids_with_exact_tag("%") == set()


# --- exact-id short-circuit --------------------------------------------------


def test_exact_id_query_returns_doc_first(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.reranker = make_id_tag_reranker(idx)
    idx.build(tmp_kb, source_commit="x")
    hits = idx.search("STD-U-002", limit=10)
    assert hits, "expected at least the id doc"
    assert hits[0].id == "STD-U-002"


def test_exact_id_first_even_when_absent_from_fts(
    tmp_index_path: Path, tmp_path: Path
) -> None:
    # Construct a KB where the id doc does NOT contain its own id in any indexed
    # column, so a plain FTS search for the id returns other docs (or nothing),
    # yet the reranker must still surface it via Index.get.
    kb = tmp_path / "kb"
    d = kb / "universal" / "foundation"
    d.mkdir(parents=True)
    # Target doc: id in front matter only; body/title never mention "STD-Z-999".
    (d / "target.md").write_text(
        "---\nid: STD-Z-999\ntier: T1\ntitle: Quiet Doc\n---\n# Quiet Doc\n\n"
        "Nothing here repeats the identifier.\n"
    )
    # A decoy doc whose BODY mentions the id string, so FTS ranks it for the query.
    (d / "decoy.md").write_text(
        "---\nid: DECOY-1\ntier: T1\ntitle: Decoy\n---\n# Decoy\n\n"
        "See STD-Z-999 STD-Z-999 STD-Z-999 for details.\n"
    )
    idx = Index(tmp_index_path)
    idx.reranker = make_id_tag_reranker(idx)
    idx.build(kb, source_commit="x")

    # Precondition: on the same built index, a plain FTS search (no reranker)
    # does NOT already put the id doc first, so the assertion below is proving
    # the reranker's effect rather than incidental ordering.
    plain_ids = [h.id for h in Index(tmp_index_path).search("STD-Z-999", limit=10)]
    assert not plain_ids or plain_ids[0] != "STD-Z-999", (
        "precondition: plain FTS should not already put the id doc first"
    )

    hits = idx.search("STD-Z-999", limit=10)
    assert hits[0].id == "STD-Z-999", "reranker must surface the exact-id doc first"


def test_unknown_id_shaped_query_unchanged(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.reranker = make_id_tag_reranker(idx)
    idx.build(tmp_kb, source_commit="x")
    base = Index(tmp_index_path)  # no reranker
    # An id-shaped token that is not a real doc must not alter results.
    q = "STD-U-001"  # this IS a doc, so use a non-existent one:
    q = "STD-QQ-404"
    assert idx.search(q, limit=10) == base.search(q, limit=10)


# --- exact-tag short-circuit -------------------------------------------------


def test_exact_tag_query_favours_tagged_docs(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.reranker = make_id_tag_reranker(idx)
    idx.build(tmp_kb, source_commit="x")
    hits = idx.search("style", limit=10)
    # STD-U-002 carries the 'style' tag; if any hits come back it must lead.
    assert hits, "expected hits for the 'style' query"
    tagged = idx.ids_with_exact_tag("style")
    assert hits[0].id in tagged


def test_exact_tag_moves_tagged_ahead_of_untagged(tmp_index_path: Path, tmp_path: Path) -> None:
    kb = tmp_path / "kb"
    d = kb / "universal" / "foundation"
    d.mkdir(parents=True)
    # Both docs mention 'alpha' in the body so both are FTS hits; only doc B
    # carries 'alpha' as a tag, so the reranker must lift B above A.
    (d / "a.md").write_text(
        "---\nid: DOC-A\ntier: T1\ntitle: A\n---\n# A\n\nalpha alpha alpha keyword.\n"
    )
    (d / "b.md").write_text(
        "---\nid: DOC-B\ntier: T1\ntags: [alpha]\ntitle: B\n---\n# B\n\nalpha here.\n"
    )
    base = Index(tmp_index_path)
    base.build(kb, source_commit="x")
    base_ids = [h.id for h in base.search("alpha", limit=10)]
    assert set(base_ids) == {"DOC-A", "DOC-B"}

    idx = Index(tmp_index_path)
    idx.reranker = make_id_tag_reranker(idx)
    ranked = [h.id for h in idx.search("alpha", limit=10)]
    assert ranked[0] == "DOC-B", "the tagged doc must lead"
    assert set(ranked) == {"DOC-A", "DOC-B"}, "no hits dropped or duplicated"


# --- ordinary queries unaffected ---------------------------------------------


def test_ordinary_multiterm_query_unchanged(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.reranker = make_id_tag_reranker(idx)
    idx.build(tmp_kb, source_commit="x")
    base = Index(tmp_index_path)  # no reranker
    q = "review structure"  # multi-term, not an id, not a single tag token
    assert [h.id for h in idx.search(q, limit=10)] == [
        h.id for h in base.search(q, limit=10)
    ]


def test_single_word_non_tag_unchanged(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.reranker = make_id_tag_reranker(idx)
    idx.build(tmp_kb, source_commit="x")
    base = Index(tmp_index_path)
    # 'worktree' is a plain content word, not a tag and not an id.
    assert [h.id for h in idx.search("worktree", limit=10)] == [
        h.id for h in base.search("worktree", limit=10)
    ]


# --- composability -----------------------------------------------------------


def test_composes_with_inner_reranker(tmp_kb: Path, tmp_index_path: Path) -> None:
    calls: list[str] = []

    def inner(query: str, hits: list[SearchHit]) -> list[SearchHit]:
        calls.append(query)
        return list(reversed(hits))

    idx = Index(tmp_index_path)
    idx.reranker = make_id_tag_reranker(idx, inner=inner)
    idx.build(tmp_kb, source_commit="x")

    # Exact-id query: inner runs (recorded), but the id short-circuit still wins.
    hits = idx.search("STD-U-002", limit=10)
    assert calls == ["STD-U-002"], "inner reranker must be invoked"
    assert hits[0].id == "STD-U-002"

    # Ordinary query: inner ordering is preserved end-to-end (reversed of base).
    calls.clear()
    base_ids = [h.id for h in Index(tmp_index_path).search("review structure", limit=10)]
    composed_ids = [h.id for h in idx.search("review structure", limit=10)]
    assert composed_ids == list(reversed(base_ids))
