"""Token counting for the benchmark.

Default is a dependency-free deterministic splitter (words + punctuation runs).
`tiktoken` is an optional precision upgrade selected by name. All methods in a
given run use the SAME tokenizer, so token RATIOS are comparable regardless of
which tokenizer is chosen; only absolute counts differ.
"""
from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

_TOKEN_RE = re.compile(r"\w+|[^\w\s]")


@runtime_checkable
class Tokenizer(Protocol):
    name: str

    def count(self, text: str) -> int: ...


class SimpleTokenizer:
    """Dependency-free: count word runs and individual punctuation marks."""

    name = "simple"

    def count(self, text: str) -> int:
        return len(_TOKEN_RE.findall(text))


class TiktokenTokenizer:
    """Optional cl100k_base counts. Requires `pip install -e '.[bench]'`."""

    name = "tiktoken-cl100k"

    def __init__(self) -> None:
        import tiktoken  # lazy: only imported when explicitly requested

        self._enc = tiktoken.get_encoding("cl100k_base")

    def count(self, text: str) -> int:
        return len(self._enc.encode(text))


def get_tokenizer(name: str = "simple") -> Tokenizer:
    if name == "simple":
        return SimpleTokenizer()
    if name in ("tiktoken", "tiktoken-cl100k"):
        return TiktokenTokenizer()
    raise ValueError(f"unknown tokenizer: {name!r}")
