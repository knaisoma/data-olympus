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

from data_olympus.index import (
    _DEFAULT_STATUS_WEIGHTS,
    Index,
    SearchHit,
    make_status_reranker,
)

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


# --- status-aware reranker (issue #37) ---------------------------------------
#
# bm25 direction: search() does ``ORDER BY score`` ASCENDING where score is a
# bm25 expression and LOWER is better. So the status reranker LOWERS the score of
# an in-force status (boost) and RAISES it for a retired one (penalize). These
# tests pin that direction rather than trusting the sign of the weights.


def _hit(id_: str, score: float, status: str) -> SearchHit:
    return SearchHit(id=id_, path=f"{id_}.md", title=id_, snippet="", score=score, status=status)


def test_status_reranker_boosts_active_over_superseded_at_same_bm25() -> None:
    reranker = make_status_reranker()
    # Same raw bm25 score; only status differs. Active must sort first.
    hits = [_hit("old", -1.0, "superseded"), _hit("new", -1.0, "active")]
    ordered = [h.id for h in reranker("caching", hits)]
    assert ordered == ["new", "old"]


def test_status_reranker_can_overturn_bm25_order() -> None:
    # The superseded doc wins on raw bm25 (more negative = better) but the
    # active doc must still rank first once the status prior is applied.
    reranker = make_status_reranker()
    hits = [_hit("old", -2.0, "superseded"), _hit("new", -1.9, "active")]
    ordered = [h.id for h in reranker("caching", hits)]
    assert ordered == ["new", "old"]


def test_status_reranker_lowers_score_for_boost_raises_for_penalty() -> None:
    reranker = make_status_reranker()
    (adjusted,) = reranker("q", [_hit("a", 0.0, "active")])
    assert adjusted.score < 0.0, "in-force status must LOWER the (ascending) score"
    (adjusted,) = reranker("q", [_hit("d", 0.0, "deprecated")])
    assert adjusted.score > 0.0, "retired status must RAISE the (ascending) score"


def test_status_reranker_neutral_for_unknown_or_empty_status() -> None:
    reranker = make_status_reranker()
    for status in ("", "mystery-status"):
        (adjusted,) = reranker("q", [_hit("x", 3.0, status)])
        assert adjusted.score == 3.0, f"status {status!r} must be neutral"


def test_status_reranker_never_drops_a_hit() -> None:
    reranker = make_status_reranker()
    hits = [_hit("a", 0.0, "active"), _hit("b", 0.0, ""), _hit("c", 0.0, "deprecated")]
    out = reranker("q", hits)
    assert {h.id for h in out} == {"a", "b", "c"}


def test_status_reranker_accepts_weight_override() -> None:
    # A caller-supplied map overrides the default; here "draft" is boosted.
    reranker = make_status_reranker({"draft": -5.0})
    (adjusted,) = reranker("q", [_hit("x", 0.0, "draft")])
    assert adjusted.score == -5.0
    # A status absent from the override map is neutral, not defaulted.
    (adjusted,) = reranker("q", [_hit("y", 0.0, "active")])
    assert adjusted.score == 0.0


def test_default_status_weights_shape() -> None:
    # In-force statuses boost (negative), retired ones penalize (positive).
    assert _DEFAULT_STATUS_WEIGHTS["active"] < 0
    assert _DEFAULT_STATUS_WEIGHTS["accepted"] < 0
    assert _DEFAULT_STATUS_WEIGHTS["superseded"] > 0
    assert _DEFAULT_STATUS_WEIGHTS["deprecated"] > 0
    assert _DEFAULT_STATUS_WEIGHTS["draft"] > 0


def test_default_status_weights_include_approved_as_in_force() -> None:
    # The target KB marks in-force decisions ``approved``; it must boost like
    # ``accepted`` rather than fall through to neutral.
    assert "approved" in _DEFAULT_STATUS_WEIGHTS
    assert _DEFAULT_STATUS_WEIGHTS["approved"] == _DEFAULT_STATUS_WEIGHTS["accepted"]


def test_status_reranker_boosts_approved() -> None:
    reranker = make_status_reranker()
    (adjusted,) = reranker("q", [_hit("x", 0.0, "approved")])
    assert adjusted.score < 0.0, "approved is in-force and must LOWER the score"


def test_status_reranker_status_match_is_case_insensitive() -> None:
    # Mixed-case frontmatter (e.g. ``Active``, ``SUPERSEDED``) must match the
    # lowercase weight keys, not be silently treated as neutral.
    reranker = make_status_reranker()
    (boosted,) = reranker("q", [_hit("a", 0.0, "Active")])
    assert boosted.score == _DEFAULT_STATUS_WEIGHTS["active"]
    (penalized,) = reranker("q", [_hit("s", 0.0, "SUPERSEDED")])
    assert penalized.score == _DEFAULT_STATUS_WEIGHTS["superseded"]


def test_status_reranker_case_insensitive_override_keys() -> None:
    # A caller-supplied map with a mixed-case key still matches a mixed-case
    # (or any-case) document status.
    reranker = make_status_reranker({"Draft": -5.0})
    (adjusted,) = reranker("q", [_hit("x", 0.0, "draft")])
    assert adjusted.score == -5.0
    (adjusted,) = reranker("q", [_hit("y", 0.0, "DRAFT")])
    assert adjusted.score == -5.0


def test_status_reranker_end_to_end_ranks_active_first(
    status_kb: Path, tmp_index_path: Path
) -> None:
    # Acceptance: a query matching both an active and a superseded doc ranks the
    # active one higher once the reranker is wired onto the Index.
    baseline = Index(tmp_index_path)
    baseline.build(status_kb, source_commit="abc")
    plain = [h.id for h in baseline.search("caching", limit=10)]
    assert "STD-OLD" in plain and "STD-NEW" in plain

    ranked = Index(tmp_index_path, reranker=make_status_reranker())
    ranked.build(status_kb, source_commit="abc")
    order = [h.id for h in ranked.search("caching", limit=10)]
    assert order.index("STD-NEW") < order.index("STD-OLD")
