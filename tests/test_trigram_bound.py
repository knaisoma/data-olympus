"""The trigram fallback bounds the phrases it builds from one query.

A single very long token (an encoded blob, a pasted hash list with no spaces) used
to expand into one OR'd phrase per distinct 3-character substring, thousands of
them from one token, in a single MATCH that cannot be cancelled once started. Each
token now contributes at most ``MAX_TRIGRAM_TOKEN_CHARS`` characters and the whole
expression at most ``MAX_TRIGRAM_PHRASES`` phrases, in first-seen order. Ordinary
queries produce exactly the expression they always did.
"""
from __future__ import annotations

import random
import time
from typing import TYPE_CHECKING

import pytest

from data_olympus import index as index_mod
from data_olympus.index import MAX_QUERY_TERMS, Index
from data_olympus.trigram import (
    MAX_TRIGRAM_PHRASES,
    MAX_TRIGRAM_TOKEN_CHARS,
    build_trigram_match_expr,
)

if TYPE_CHECKING:
    from pathlib import Path


def _phrases(expr: str | None) -> list[str]:
    return [] if expr is None else expr.split(" OR ")


def _reference(query: str) -> str | None:
    """The expression the builder produced before any bound existed."""
    grams: list[str] = []
    seen: set[str] = set()
    for word in query.lower().split():
        for i in range(len(word) - 2):
            g = word[i : i + 3]
            if g not in seen:
                seen.add(g)
                grams.append(g)
    if not grams:
        return None
    return " OR ".join('"' + g.replace('"', '""') + '"' for g in grams)


def _long_token(n: int) -> str:
    # Seeded, so nearly every 3-character window is distinct and the test is deterministic.
    rng = random.Random(1234)
    return "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(n))


def test_the_bounds_are_named_constants() -> None:
    assert MAX_TRIGRAM_TOKEN_CHARS == 64
    assert MAX_TRIGRAM_PHRASES == 256


def test_one_very_long_token_stays_within_the_bounds() -> None:
    expr = build_trigram_match_expr(_long_token(30_000))
    phrases = _phrases(expr)
    assert 0 < len(phrases) <= MAX_TRIGRAM_PHRASES
    # One token contributes at most its first 64 characters: 62 windows of 3.
    assert len(phrases) <= MAX_TRIGRAM_TOKEN_CHARS - 2


def test_only_the_first_characters_of_a_token_count() -> None:
    token = _long_token(500)
    assert build_trigram_match_expr(token) == build_trigram_match_expr(
        token[:MAX_TRIGRAM_TOKEN_CHARS],
    )


def test_many_tokens_are_capped_in_first_seen_order() -> None:
    source = _long_token(4_000)
    query = " ".join(source[i * 40 : (i + 1) * 40] for i in range(64))
    assert len(_phrases(_reference(query))) > MAX_TRIGRAM_PHRASES
    capped = _phrases(build_trigram_match_expr(query))
    assert len(capped) == MAX_TRIGRAM_PHRASES
    assert capped == _phrases(_reference(query))[:MAX_TRIGRAM_PHRASES]


@pytest.mark.parametrize(
    "query",
    [
        "kube",
        "observabilty",
        "kubernet",
        "ku",
        "",
        'a"bc',
        "Kubernetes helm chart deployment rollout strategy",
        "  spaced   out\tquery ",
        "x" * MAX_TRIGRAM_TOKEN_CHARS,
        " ".join(["token", "another", "terms"] * 5),
    ],
)
def test_ordinary_queries_build_the_expression_they_always_did(query: str) -> None:
    assert build_trigram_match_expr(query) == _reference(query)


def test_search_never_builds_more_than_the_bound(
    tmp_path: Path, tmp_index_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "a.md").write_text(
        "---\nid: DOC-A\n---\nalpha caching library guidance.\n", encoding="utf-8",
    )
    idx = Index(tmp_index_path, trigram_fallback=True)
    idx.build(kb, source_commit="x")
    original = index_mod.build_trigram_match_expr
    sizes: list[int] = []

    def spy(query: str) -> str | None:
        expr = original(query)
        sizes.append(len(_phrases(expr)))
        if sizes[-1] > MAX_TRIGRAM_PHRASES:
            raise AssertionError(f"unbounded expression with {sizes[-1]} phrases")
        return expr

    monkeypatch.setattr(index_mod, "build_trigram_match_expr", spy)
    start = time.monotonic()
    idx.search(_long_token(30_000), limit=5)
    assert sizes, "trigram fallback did not run"
    assert max(sizes) <= MAX_TRIGRAM_PHRASES
    assert time.monotonic() - start < 2.0


def test_the_term_cap_is_unchanged() -> None:
    assert MAX_QUERY_TERMS == 64
