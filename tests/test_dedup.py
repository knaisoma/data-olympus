"""Tests for the deterministic dedup heuristics."""
from __future__ import annotations

from data_olympus.dedup import (
    classify_overlap,
    content_hash,
    extract_headings,
    jaccard,
    normalize_markdown,
)


def test_normalize_strips_frontmatter_and_collapses_whitespace() -> None:
    text = "---\nid: x\n---\n# Title\n\nHello   World\n"
    norm = normalize_markdown(text)
    assert "id: x" not in norm
    assert "hello world" in norm


def test_content_hash_ignores_frontmatter_and_whitespace_differences() -> None:
    a = "---\nid: a\n---\n# T\n\nsame body here\n"
    b = "---\nid: b\n---\n# T\nsame   body   here"
    assert content_hash(a) == content_hash(b)


def test_extract_headings_lowercased() -> None:
    assert extract_headings("# Alpha\n## Beta Gamma\ntext") == {"alpha", "beta gamma"}


def test_jaccard_bounds() -> None:
    assert jaccard(set(), set()) == 1.0
    assert jaccard({"a"}, set()) == 0.0
    assert jaccard({"a", "b"}, {"a", "b"}) == 1.0


def test_classify_exact_duplicate() -> None:
    local = "# Purpose\n\nThe billing service charges customers monthly.\n"
    kb = "---\nid: p\n---\n# Purpose\nThe billing   service charges customers monthly."
    cls, headings = classify_overlap(local, kb)
    assert cls == "imported_duplicate"
    assert headings == []


def test_classify_partial_overlap_returns_shared_headings() -> None:
    local = (
        "# Architecture\n\n" + " ".join(f"word{i}" for i in range(40))
        + "\n# Extra Local\n\nlocal only text\n"
    )
    kb = (
        "# Architecture\n\n" + " ".join(f"word{i}" for i in range(40))
        + "\n# Extra Kb\n\ndifferent tail entirely here now\n"
    )
    cls, headings = classify_overlap(local, kb, jaccard_threshold=0.5)
    assert cls == "partial_overlap"
    assert "architecture" in headings


def test_classify_unique_when_no_overlap() -> None:
    local = "# Deploy\n\nkubernetes rollout steps for the gateway\n"
    kb = "# Purpose\n\nunrelated billing invariants and money math\n"
    cls, headings = classify_overlap(local, kb)
    assert cls == "unique"
    assert headings == []
