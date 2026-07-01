"""Tests for the composable search pipeline (issue #36).

Establishes the staged seams that the search-enhancement features plug into:
- expand-query: an optional ``query_expander`` hook rewrites the term list.
- match: ``_build_match_expr`` turns terms (+ optional column restriction) into
  the FTS5 MATCH expression.
- re-rank: an optional ``reranker`` hook reorders/rescores the hit list.

The existing tests in test_index.py assert that the default pipeline (no hooks)
preserves current search behaviour; these assert the seams exist and are wired.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from data_olympus.index import Index

if TYPE_CHECKING:
    from pathlib import Path


# --- match stage: _build_match_expr ------------------------------------------


def test_build_match_expr_or_joins_quoted_terms(tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    assert idx._build_match_expr(["alpha", "beta"], None) == '"alpha" OR "beta"'


def test_build_match_expr_restricts_to_columns(tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    assert idx._build_match_expr(["alpha"], ["title", "body"]) == '{title body} : ("alpha")'


def test_build_match_expr_rejects_unknown_column(tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    try:
        idx._build_match_expr(["alpha"], ["title", "bogus"])
    except ValueError as e:
        assert "bogus" in str(e)
    else:
        raise AssertionError("expected ValueError for unknown column")


def test_build_match_expr_quotes_embedded_quotes(tmp_index_path: Path) -> None:
    # A term containing a double quote must not break out of its phrase.
    assert idx_quote(tmp_index_path, 'a"b') == '"a""b"'


def idx_quote(path: Path, term: str) -> str:
    return Index(path)._build_match_expr([term], None)


# --- expand-query stage: query_expander hook ---------------------------------


def test_query_expander_hook_rewrites_terms(tmp_kb: Path, tmp_index_path: Path) -> None:
    # A term that does not occur in the corpus, expanded to one that does.
    def expander(terms: list[str]) -> list[str]:
        return ["worktree" if t == "zzznope" else t for t in terms]

    idx = Index(tmp_index_path, query_expander=expander)
    idx.build(tmp_kb, source_commit="abc")
    hits = idx.search("zzznope", limit=10)
    assert hits, "expander should have rewritten the query to a matching term"
    assert any("worktree" in h.snippet.lower() or "worktree" in h.title.lower() for h in hits)


def test_no_expander_leaves_query_unchanged(tmp_kb: Path, tmp_index_path: Path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="abc")
    assert idx.search("zzznope", limit=10) == []  # unknown term, no expansion -> no hits


# --- re-rank stage: reranker hook --------------------------------------------


def test_reranker_hook_reorders_results(tmp_kb: Path, tmp_index_path: Path) -> None:
    base = Index(tmp_index_path)
    base.build(tmp_kb, source_commit="abc")
    default_order = [h.id for h in base.search("STD", limit=10)]
    assert len(default_order) >= 2, "need >=2 hits to prove reordering"

    def reverser(query: str, hits: list) -> list:  # noqa: ARG001
        return list(reversed(hits))

    ranked = Index(tmp_index_path, reranker=reverser)
    reordered = [h.id for h in ranked.search("STD", limit=10)]
    assert reordered == list(reversed(default_order))
