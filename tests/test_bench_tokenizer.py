from __future__ import annotations

from benchmarks.tokenizer import SimpleTokenizer, get_tokenizer


def test_simple_tokenizer_counts_words_and_punct() -> None:
    tok = SimpleTokenizer()
    # 5 word tokens + 1 period
    assert tok.count("the quick brown fox jumps.") == 6


def test_simple_tokenizer_empty_is_zero() -> None:
    assert SimpleTokenizer().count("") == 0
    assert SimpleTokenizer().count("   \n  ") == 0


def test_simple_tokenizer_is_deterministic() -> None:
    tok = SimpleTokenizer()
    text = "STD-U-002: avoid em-dashes; use commas, please."
    assert tok.count(text) == tok.count(text)


def test_get_tokenizer_default_is_simple() -> None:
    tok = get_tokenizer("simple")
    assert tok.name == "simple"
    assert tok.count("a b c") == 3


def test_get_tokenizer_unknown_raises() -> None:
    import pytest
    with pytest.raises(ValueError, match="unknown tokenizer"):
        get_tokenizer("nope")
