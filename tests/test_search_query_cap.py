"""Query term cap: an oversized query cannot build an unbounded FTS5 MATCH.

The UserPromptSubmit hook posts whole prompts (review prompts carry patches of
hundreds of KiB) as a consult intent, which reached ``Index.search`` as the
query. Every whitespace token became one OR'd phrase in a single bm25 MATCH:
16k terms took 17.7 s and 308 MB, and the uncancellable worker threads piled
up until the server was OOMKilled. ``Index.search`` now dedupes the terms
(order-preserving) and keeps at most ``MAX_QUERY_TERMS`` of them, for both the
primary MATCH and the trigram fallback.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

import data_olympus.index as index_mod
from data_olympus.index import MAX_QUERY_TERMS, Index

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _kb(tmp_path: Path) -> Path:
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "a.md").write_text(
        "---\nid: DOC-A\n---\nalpha caching library guidance.\n", encoding="utf-8",
    )
    return kb


def _spy_match_terms(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record the terms of every primary MATCH; fail fast on an uncapped list
    so the red run against the old code does not grind through a huge MATCH."""
    seen: list[list[str]] = []
    original = Index._build_match_expr

    def spy(self: Index, terms: list[str], columns: list[str] | None) -> str:
        seen.append(list(terms))
        if len(terms) > MAX_QUERY_TERMS:
            raise AssertionError(f"uncapped MATCH with {len(terms)} terms")
        return original(self, terms, columns)

    monkeypatch.setattr(Index, "_build_match_expr", spy)
    return seen


def test_max_query_terms_is_64() -> None:
    assert MAX_QUERY_TERMS == 64


def test_huge_query_is_capped_and_fast(
    tmp_path: Path, tmp_index_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    idx = Index(tmp_index_path)
    idx.build(_kb(tmp_path), source_commit="x")
    seen = _spy_match_terms(monkeypatch)
    query = "alpha " + " ".join(f"term{i}" for i in range(50_000))
    start = time.monotonic()
    hits = idx.search(query, limit=5)
    elapsed = time.monotonic() - start
    assert seen, "primary MATCH was not built"
    assert all(len(t) <= MAX_QUERY_TERMS for t in seen)
    assert seen[0][0] == "alpha"
    assert [h.id for h in hits] == ["DOC-A"]
    assert elapsed < 2.0, f"capped search took {elapsed:.2f}s"


def test_terms_are_deduped_in_order_before_the_cap(
    tmp_path: Path, tmp_index_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    idx = Index(tmp_index_path)
    idx.build(_kb(tmp_path), source_commit="x")
    seen = _spy_match_terms(monkeypatch)
    # 10k repeats of two tokens must collapse to those two tokens, in order, so
    # a repeated token cannot crowd distinct terms out of the cap.
    idx.search(" ".join(["caching", "alpha"] * 10_000) + " library", limit=5)
    assert seen[0] == ["caching", "alpha", "library"]


def test_trigram_fallback_gets_the_capped_query(
    tmp_path: Path, tmp_index_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    idx = Index(tmp_index_path, trigram_fallback=True)
    idx.build(_kb(tmp_path), source_commit="x")
    seen: list[str] = []
    original = index_mod.build_trigram_match_expr

    def spy(query: str) -> str | None:
        seen.append(query)
        return original(query)

    monkeypatch.setattr(index_mod, "build_trigram_match_expr", spy)
    idx.search(" ".join(f"zz{i}" for i in range(50_000)), limit=5)
    assert seen, "trigram fallback did not run"
    assert all(len(q.split()) <= MAX_QUERY_TERMS for q in seen)
