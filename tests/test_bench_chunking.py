from __future__ import annotations

from benchmarks.chunking import chunk_text


def test_chunk_short_text_is_single_chunk() -> None:
    chunks = chunk_text("one two three", size=10, overlap=2)
    assert chunks == ["one two three"]


def test_chunk_splits_on_size() -> None:
    words = " ".join(str(i) for i in range(10))  # 10 whitespace tokens
    chunks = chunk_text(words, size=4, overlap=0)
    assert chunks == ["0 1 2 3", "4 5 6 7", "8 9"]


def test_chunk_overlap_repeats_tail() -> None:
    words = " ".join(str(i) for i in range(6))
    chunks = chunk_text(words, size=4, overlap=2)
    # step = size - overlap = 2
    assert chunks[0] == "0 1 2 3"
    assert chunks[1] == "2 3 4 5"


def test_chunk_empty_returns_empty_list() -> None:
    assert chunk_text("", size=4, overlap=0) == []


def test_chunk_rejects_bad_overlap() -> None:
    import pytest
    with pytest.raises(ValueError):
        chunk_text("a b c", size=2, overlap=2)
